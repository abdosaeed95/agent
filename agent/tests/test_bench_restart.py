from __future__ import annotations

import runpy
import shlex
import subprocess
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from shlex import quote
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import docker

from agent.base import AgentException, Base
from agent.bench import Bench
from agent.bench_restart import RUNTIME, SandboxMigration
from agent.cli_sandbox import POLICY_LABEL, POLICY_VERSION


class TestSandboxMigration(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.events = []
        self.site_config = {"db_name": "test", "pause_scheduler": False, "unrelated": "keep"}
        self.original = self.site_config.copy()
        self.site = SimpleNamespace(
            config=self.site_config,
            get_config=MagicMock(side_effect=lambda **kwargs: self.site_config.copy()),
            set_config=MagicMock(side_effect=self.write_config),
        )
        self.bench = SimpleNamespace(
            name="bench-test",
            directory=str(self.root),
            bench_config={"docker_image": "test-image"},
            valid_sites={"test.local": self.site},
            execute=MagicMock(side_effect=RuntimeError("migration failed")),
            verify_cli_sandbox=MagicMock(side_effect=lambda: self.events.append("preflight")),
            start=MagicMock(side_effect=self.start_candidate),
        )
        for folder in ("sites", "config", "logs"):
            path = self.root / folder
            path.mkdir()
            self.bench.__dict__[folder + "_directory"] = str(path)
        site = self.root / "sites/test.local"
        site.mkdir()
        (site / "site_config.json").write_text("{}")
        self.old = self.container("old-id")
        self.old.attrs = {
            "HostConfig": {"RestartPolicy": {"Name": "always", "MaximumRetryCount": 0}},
            "Mounts": [
                {
                    "Type": "bind",
                    "Destination": f"/home/frappe/frappe-bench/{folder}",
                    "Source": str(self.root / folder),
                    "RW": True,
                }
                for folder in ("sites", "config", "logs")
            ],
        }
        self.current = self.old
        self.client = MagicMock()
        self.client.containers.get.side_effect = self.get_container
        self.client.images.get.return_value.id = "image-id"
        self.migration = SandboxMigration(self.bench, self.client)
        self.migration.runtime = MagicMock(side_effect=self.runtime)
        self.snapshot = {
            "processes": {"web": "RUNNING", "worker": "RUNNING", "code-server": "STOPPED"},
            "queue": {"value": None, "ttl": -2, "captured_at": 1},
        }

    def write_config(self, config):
        self.site_config.clear()
        self.site_config.update(config)

    def container(self, identifier):
        container = MagicMock(id=identifier, short_id=identifier, status="running")
        container.name = self.bench.name
        container.image.id = "image-id"
        container.labels = {POLICY_LABEL: POLICY_VERSION}
        container.stop.side_effect = lambda **kwargs: self.events.append(f"stop:{identifier}")
        container.remove.side_effect = lambda **kwargs: self.events.append(f"remove:{identifier}")
        container.rename.side_effect = lambda name: self.rename(container, name)
        return container

    def rename(self, container, name):
        container.name = name
        self.events.append(f"rename:{container.id}:{name}")
        self.current = container if name == self.bench.name else None

    def get_container(self, name):
        if name == "old-id":
            return self.old
        if name == self.bench.name and self.current is not None:
            return self.current
        return MagicMock(side_effect=docker.errors.NotFound("absent"))()

    def start_candidate(self):
        self.events.append("start:new")
        self.current = self.container("new-id")

    def runtime(self, action, timeout=30):
        self.events.append(action)
        if action == "snapshot":
            return deepcopy(self.snapshot)
        return {}

    def test_health_passes_before_old_is_removed_and_original_settings_are_restored(self):
        self.migration.run()
        self.assertLess(self.events.index("drain"), self.events.index("stop:old-id"))
        self.assertLess(self.events.index("start:new"), len(self.events) - 1)
        self.assertEqual(self.events[-3:], ["health", "restore-queue", "remove:old-id"])
        self.assertEqual(self.site_config, self.original)
        self.old.update.assert_called_once_with(restart_policy={"Name": "no"})
        self.assertFalse(self.migration.checkpoint.exists())

    def test_unhealthy_candidate_restores_exact_old_container_without_relaunching_it(self):
        def runtime(action, timeout=30):
            if action == "health" and self.current and self.current.id == "new-id":
                return MagicMock(side_effect=RuntimeError("unhealthy"))()
            return self.runtime(action, timeout)

        self.migration.runtime.side_effect = runtime
        with self.assertRaisesRegex(RuntimeError, "migration failed"):
            self.migration.run()
        self.assertIs(self.current, self.old)
        self.bench.start.assert_called_once_with()
        self.assertIn("remove:new-id", self.events)
        self.assertNotIn("remove:old-id", self.events)
        self.assertEqual([c.args[0] for c in self.migration.runtime.call_args_list].count("drain"), 1)
        self.assertEqual(self.site_config, self.original)
        self.assertFalse(self.migration.checkpoint.exists())

    def test_drain_timeout_keeps_old_container_and_does_not_signal_workers_again(self):
        def runtime(action, timeout=30):
            if action == "drain":
                return MagicMock(side_effect=RuntimeError("busy worker"))()
            return self.runtime(action, timeout)

        self.migration.runtime.side_effect = runtime
        with self.assertRaises(RuntimeError):
            self.migration.run()
        self.old.stop.assert_not_called()
        self.old.remove.assert_not_called()
        self.bench.start.assert_not_called()
        self.assertEqual([c.args[0] for c in self.migration.runtime.call_args_list].count("drain"), 1)
        self.assertEqual(self.site_config, self.original)

    def test_recovery_record_survives_failed_rollback(self):
        def runtime(action, timeout=30):
            if action == "restore-processes":
                return MagicMock(side_effect=RuntimeError("supervisor unavailable"))()
            if action == "drain":
                return MagicMock(side_effect=RuntimeError("busy worker"))()
            return self.runtime(action, timeout)

        self.migration.runtime.side_effect = runtime
        with self.assertRaisesRegex(RuntimeError, "supervisor unavailable"):
            self.migration.run()
        self.assertTrue(self.migration.checkpoint.exists())
        self.assertEqual(self.migration.checkpoint.stat().st_mode & 0o777, 0o600)
        self.old.remove.assert_not_called()

    def test_failed_preflight_leaves_sites_and_container_untouched(self):
        self.bench.verify_cli_sandbox.side_effect = RuntimeError("policy rejected")
        with self.assertRaisesRegex(RuntimeError, "policy rejected"):
            self.migration.run()
        self.assertEqual(self.site_config, self.original)
        self.old.stop.assert_not_called()
        self.assertFalse(self.migration.checkpoint.exists())

    def test_extra_mounts_are_recovered_from_docker_with_modes_and_safe_quoting(self):
        self.old.attrs["Mounts"].append(
            {
                "Type": "bind",
                "Source": "/mnt/data/shared assets",
                "Destination": "/assets",
                "Mode": "ro",
                "Propagation": "rslave",
                "RW": False,
            }
        )
        self.migration.prepare()
        command = Bench.prepare_mounts_on_host(self.bench, "/home/frappe/frappe-bench")
        self.assertEqual(shlex.split(command), ["-v", "/mnt/data/shared assets:/assets:ro,rslave"])

    def test_image_upgrade_or_missing_persistent_mount_is_rejected_before_drain(self):
        for invalid in ("image", "mount"):
            with self.subTest(invalid=invalid):
                self.old.image.id = "different" if invalid == "image" else "image-id"
                if invalid == "mount":
                    self.old.attrs["Mounts"] = []
                with self.assertRaises(RuntimeError):
                    self.migration.run()
                self.assertEqual(self.events, [])
                self.assertFalse(self.migration.checkpoint.exists())

    def test_committed_recovery_resumes_cleanup_without_restarting_or_replaying_jobs(self):
        self.migration.prepare()
        self.start_candidate()
        self.migration.state.update(phase="committed", new_id="new-id")
        self.migration.save()
        self.migration.run()
        self.bench.start.assert_not_called()
        self.assertEqual(self.events.count("drain"), 0)
        self.assertFalse(self.migration.checkpoint.exists())

    def test_incomplete_migration_is_recovered_before_retrying(self):
        self.migration.prepare()
        self.migration.site_flags()
        self.migration.run()
        self.assertEqual(self.site_config, self.original)
        self.assertEqual(self.events.count("restore-queue"), 2)
        self.bench.start.assert_called_once_with()


class TestMigrationRuntime(unittest.TestCase):
    def setUp(self):
        self.runtime = runpy.run_path(str(RUNTIME))

    def test_existing_queue_suspension_and_expiry_are_preserved(self):
        connection = MagicMock()
        state = {"queue": {"value": "MQ==", "ttl": -1, "captured_at": 0}}
        self.runtime["restore_queue"](connection, state)
        connection.set.assert_called_once_with("rq:suspended", b"1", px=None)
        connection.save.assert_called_once_with()
        connection.reset_mock()
        state["queue"]["ttl"] = 1
        self.runtime["restore_queue"](connection, state)
        connection.delete.assert_called_once_with("rq:suspended")

    def test_database_probe_uses_frappe_sites_directory_and_restores_cwd(self):
        original = Path.cwd()
        with TemporaryDirectory() as root:
            (Path(root) / "sites").mkdir()
            frappe = SimpleNamespace(
                init=MagicMock(),
                destroy=MagicMock(),
                connect=MagicMock(),
                db=MagicMock(),
                conf=SimpleNamespace(maintenance_mode=False, allow_reads_during_maintenance=False),
            )
            response = SimpleNamespace(status_code=200, json=lambda: {"message": "pong"})
            get = MagicMock(return_value=response)
            with patch.object(Path, "cwd", return_value=Path(root)), patch("os.chdir") as chdir, patch.dict(
                "sys.modules", frappe=frappe
            ), patch.dict(self.runtime["site_health"].__globals__, requests=SimpleNamespace(get=get)):
                self.assertTrue(self.runtime["site_health"]("site.local", True))
                self.assertEqual(
                    [call.args[0] for call in chdir.call_args_list], [Path(root) / "sites", Path(root)]
                )
                frappe.db.sql.assert_called_once_with("SELECT 1")
                frappe.destroy.assert_called_once_with()
        self.assertEqual(Path.cwd(), original)

    def test_busy_or_not_yet_suspended_workers_prevent_draining(self):
        connection = MagicMock()
        worker = MagicMock()
        worker.get_current_job_id.return_value = None
        with patch.dict(self.runtime["workers_idle"].__globals__, Queue=MagicMock(), Worker=MagicMock()):
            globals_ = self.runtime["workers_idle"].__globals__
            globals_["Queue"].all.return_value = []
            globals_["Worker"].all.return_value = [worker]
            worker.get_state.return_value = "busy"
            self.assertFalse(self.runtime["workers_idle"](connection))
            worker.get_state.return_value = "suspended"
            self.assertTrue(self.runtime["workers_idle"](connection))
            worker.get_current_job_id.return_value = "job"
            self.assertFalse(self.runtime["workers_idle"](connection))

    def test_retry_does_not_send_a_second_shutdown_signal_to_a_busy_worker(self):
        worker = SimpleNamespace(shutdown_requested_date="already requested")
        control = MagicMock()
        states = {"frappe-bench-workers:frappe-bench-frappe-worker-0": "RUNNING"}
        with patch.dict(
            self.runtime["stop_producers"].__globals__,
            processes=lambda: states,
            control=control,
            Worker=SimpleNamespace(all=lambda **kwargs: [worker]),
        ):
            self.runtime["stop_producers"](MagicMock())
        control.assert_not_called()

    def test_worker_pool_scheduler_thread_counts_as_healthy_only_while_lock_is_held(self):
        name = "frappe-bench-workers:frappe-bench-frappe-schedule"
        with patch.dict(
            self.runtime["processes_ready"].__globals__,
            processes=lambda: {name: "EXITED"},
            scheduler_alive=lambda: True,
        ):
            self.assertTrue(self.runtime["processes_ready"]({name: "RUNNING"}))
            self.assertFalse(self.runtime["needs_start"](name, "EXITED", "RUNNING"))
        with patch.dict(
            self.runtime["processes_ready"].__globals__,
            processes=lambda: {name: "EXITED"},
            scheduler_alive=lambda: False,
        ):
            self.assertFalse(self.runtime["processes_ready"]({name: "RUNNING"}))
            self.assertTrue(self.runtime["needs_start"](name, "EXITED", "RUNNING"))

    def test_stuck_shutdown_can_only_force_workers_that_are_proven_idle(self):
        name = "frappe-bench-workers:frappe-bench-frappe-worker"
        connection = MagicMock()
        kill = MagicMock()
        run = MagicMock(side_effect=subprocess.TimeoutExpired("supervisorctl", 10))
        for idle in (False, True):
            with self.subTest(idle=idle), patch.dict(
                self.runtime["stop_idle_workers"].__globals__,
                processes=lambda: {name: "RUNNING"},
                finish_idle_stops=MagicMock(),
                workers_idle=MagicMock(return_value=idle),
                kill_idle_group=kill,
                subprocess=SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired, DEVNULL=-3),
            ):
                if idle:
                    self.runtime["stop_idle_workers"](connection, [name])
                    kill.assert_called_once_with(connection, name)
                else:
                    with self.assertRaises(SystemExit):
                        self.runtime["stop_idle_workers"](connection, [name])
                    kill.assert_not_called()


class TestMigrationCommandFailure(unittest.TestCase):
    def test_closed_output_pipe_does_not_hide_a_later_nonzero_exit(self):
        base = Base()
        base.log = MagicMock()
        base.update_redis = MagicMock()
        code = "import os,time,sys; os.close(1); os.close(2); time.sleep(0.1); sys.exit(7)"
        with self.assertRaises(AgentException):
            base.execute(f"{quote(sys.executable)} -c {quote(code)}")
        self.assertEqual(base.data["returncode"], 7)


if __name__ == "__main__":
    unittest.main()
