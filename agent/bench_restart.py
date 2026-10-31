from __future__ import annotations

import json
import os
from pathlib import Path
from shlex import quote
from tempfile import NamedTemporaryFile

import docker
from filelock import FileLock

from agent.cli_sandbox import POLICY_LABEL, POLICY_VERSION

CHECKPOINT = ".cli-sandbox-migration.json"
SITE_FLAGS = ("maintenance_mode", "pause_scheduler", "allow_reads_during_maintenance")
RUNTIME = Path(__file__).parent / "templates" / "bench" / "cli-sandbox-runtime.py"


def restart_for_cli_sandbox(bench):
    with FileLock(str(Path(bench.directory) / (CHECKPOINT + ".lock")), timeout=1), docker.from_env(
        timeout=60
    ) as client:
        migration = SandboxMigration(bench, client)
        migration.run()


class SandboxMigration:
    def __init__(self, bench, client):
        self.bench = bench
        self.client = client
        self.checkpoint = Path(bench.directory) / CHECKPOINT
        self.state = {}

    def fail(self, message):
        self.bench.execute(f"printf '%s\\n' {quote(message)} >&2; exit 1")

    def runtime(self, action, timeout=30):
        result = self.bench.docker_execute(
            f"timeout {timeout + 15} env/bin/python - {quote(action)} "
            f"{quote(json.dumps(self.state))} {timeout}",
            input=RUNTIME.read_text(),
        )
        return json.loads(result["output"].splitlines()[-1])

    def save(self):
        with NamedTemporaryFile(mode="w", dir=self.checkpoint.parent, delete=False) as temporary:
            json.dump(self.state, temporary)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary.name, self.checkpoint)

    def site_flags(self, restore=False, allow_reads=True):
        sites = self.bench.valid_sites
        for name, original in self.state["sites"].items():
            site = sites[name]
            config = site.get_config(for_update=True)
            if restore:
                for key in SITE_FLAGS:
                    config.pop(key, None)
                config.update(original)
            else:
                config.update(
                    {
                        "maintenance_mode": True,
                        "pause_scheduler": True,
                        "allow_reads_during_maintenance": allow_reads,
                    }
                )
            site.set_config(config)

    def prepare(self):
        old = self.client.containers.get(self.bench.name)
        image = self.client.images.get(self.bench.bench_config["docker_image"])
        if old.status != "running" or old.image.id != image.id:
            self.fail(
                "CLI sandbox migration requires the running bench's current image; no app upgrade is allowed."
            )
        self.preserve_mounts(old)
        sites = self.bench.valid_sites
        expected_sites = {
            path.parent.name for path in Path(self.bench.sites_directory).glob("*/site_config.json")
        }
        if set(sites) != expected_sites:
            self.fail("CLI sandbox migration could not validate every site's configuration.")
        self.state = {
            "phase": "preparing",
            "old_id": old.id,
            "restart_policy": old.attrs["HostConfig"]["RestartPolicy"],
            "backup": f"{self.bench.name}-rollback-{old.short_id}",
            "sites": {
                name: {key: value for key, value in site.config.items() if key in SITE_FLAGS}
                for name, site in sites.items()
            },
        }
        self.state.update(self.runtime("snapshot"))
        self.runtime("health", timeout=120)
        self.bench.verify_cli_sandbox()
        self.save()

    def preserve_mounts(self, old):
        base = {f"/home/frappe/frappe-bench/{folder}" for folder in ("sites", "config", "logs")}
        for folder in ("sites", "config", "logs"):
            expected = Path(getattr(self.bench, folder + "_directory")).resolve()
            if not any(
                mount["Type"] == "bind"
                and mount["Destination"] == f"/home/frappe/frappe-bench/{folder}"
                and Path(mount["Source"]).resolve() == expected
                and mount["RW"]
                and mount.get("Propagation", "rprivate") in ("", "rprivate")
                and mount.get("Mode", "rw") in ("", "rw")
                for mount in old.attrs["Mounts"]
            ):
                self.fail(f"CLI sandbox migration requires the persistent {folder} bind mount.")
        self.bench.mounts = [
            self.extra_mount(mount) for mount in old.attrs["Mounts"] if mount["Destination"] not in base
        ]

    def extra_mount(self, mount):
        if mount["Type"] not in ("bind", "volume"):
            self.fail(
                "CLI sandbox migration cannot preserve this mount type; the original bench is unchanged."
            )
        mode = mount.get("Mode") or ("rw" if mount["RW"] else "ro")
        propagation = mount.get("Propagation")
        if propagation and propagation not in mode.split(","):
            mode += "," + propagation
        return {
            "source": mount["Name"] if mount["Type"] == "volume" else mount["Source"],
            "destination": mount["Destination"] + ":" + mode,
            "is_absolute_path": True,
        }

    def run(self):
        if self.checkpoint.exists():
            self.state = json.loads(self.checkpoint.read_text())
            if self.state["phase"] == "committed":
                self.finish()
                return
            if self.state["phase"] == "rolled-back":
                self.restore_settings()
                self.checkpoint.unlink()
            else:
                self.rollback()
        self.prepare()
        try:
            self.site_flags(allow_reads=False)
            self.runtime("drain", timeout=300)
            self.state["quiesced"] = True
            self.save()
            old = self.client.containers.get(self.state["old_id"])
            old.update(restart_policy={"Name": "no"})
            old.stop(timeout=60)
            old.rename(self.state["backup"])
            self.bench.start()
            if self.client.containers.get(self.bench.name).image.id != old.image.id:
                self.fail("Replacement image changed during migration; restoring the original bench.")
            self.site_flags()
            self.runtime("restore-processes", timeout=60)
            self.runtime("health", timeout=120)
            # Once traffic and jobs resume, recovery must finish forward, never replay work.
            self.state["phase"] = "committed"
            self.state["new_id"] = self.client.containers.get(self.bench.name).id
            self.save()
        except Exception:
            self.rollback()
            self.fail("CLI sandbox migration failed; the original container was restored and checked.")
        self.finish()

    def finish(self):
        current = self.client.containers.get(self.bench.name)
        if current.id != self.state["new_id"]:
            self.fail(
                "Migration recovery stopped: the replacement container has changed. Recovery record retained."
            )
        self.restore_settings()
        try:
            old = self.client.containers.get(self.state["old_id"])
        except docker.errors.NotFound:
            old = None
        if old:
            old.remove()
        self.checkpoint.unlink()

    def rollback(self):
        old = self.client.containers.get(self.state["old_id"])
        try:
            current = self.client.containers.get(self.bench.name)
        except docker.errors.NotFound:
            current = None
        if current and current.id != old.id:
            if current.labels.get(POLICY_LABEL) != POLICY_VERSION:
                self.fail(
                    "Rollback stopped: an unexpected container owns the bench name. Recovery record retained."
                )
            if current.status == "running":
                # Candidate jobs stay suspended until commit; rollback also works when Redis is unhealthy.
                current.stop(timeout=60)
            current.remove()
        old.reload()
        if old.name != self.bench.name:
            old.rename(self.bench.name)
        if old.status != "running":
            old.start()
        old.update(restart_policy=self.state["restart_policy"])
        self.site_flags(restore=True)
        self.runtime("restore-processes", timeout=60)
        self.state.pop("quiesced", None)
        self.runtime("health", timeout=120)
        self.state["phase"] = "rolled-back"
        self.save()
        self.restore_settings()
        self.checkpoint.unlink()

    def restore_settings(self):
        self.site_flags(restore=True)
        self.runtime("restore-queue")
