from __future__ import annotations

import json
import shlex
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

from agent.bench import Bench
from agent.bench_restart import CHECKPOINT
from agent.cli_sandbox import POLICY_LABEL, PROFILE_NAME, SECCOMP_PROFILE, setup_cli_sandbox
from agent.patches.enable_cli_sandbox import execute


class TestBenchCLISandbox(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.apps_file = self.root / "apps.txt"
        self.apps_file.write_text("frappe\nai\n")
        self.bench = object.__new__(Bench)
        self.bench.name = "test-bench"
        self.bench.apps_file = str(self.apps_file)
        self.bench.sites_directory = str(self.root / "sites")
        self.bench.logs_directory = str(self.root / "logs")
        self.bench.config_directory = str(self.root / "config")
        self.bench.mounts = None
        self.bench.execute = MagicMock(return_value={"output": ""})
        self.config = {
            "single_container": True,
            "docker_image": "example/bench:test",
            "web_port": 8000,
            "socketio_port": 9000,
            "codeserver_port": 8088,
        }
        patcher = patch.object(Bench, "bench_config", new_callable=PropertyMock, return_value=self.config)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_ai_bench_is_verified_before_replacing_its_container(self):
        self.bench.start()
        commands = [call.args[0] for call in self.bench.execute.call_args_list]
        self.assertIn("--entrypoint /usr/bin/bwrap", commands[0])
        self.assertIn("--rm --network none -u frappe", commands[0])
        self.assertEqual(commands[1:3], ["docker stop test-bench", "docker rm test-bench"])
        for command in (commands[0], commands[-1]):
            args = shlex.split(command)
            self.assertIn(f"seccomp={SECCOMP_PROFILE}", args)
            self.assertIn(f"apparmor={PROFILE_NAME}", args)
            self.assertIn("systempaths=unconfined", args)
            self.assertIn("no-new-privileges", args)
            self.assertIn(f"{POLICY_LABEL}=1", args)
            self.assertNotIn("--privileged", args)
            self.assertNotIn("--cap-add", args)

    def test_failed_sandbox_check_preserves_the_running_container(self):
        self.bench.execute.side_effect = RuntimeError("sandbox failed")
        with self.assertRaisesRegex(RuntimeError, "sandbox failed"):
            self.bench.start()
        self.assertEqual(self.bench.execute.call_count, 1)
        self.assertNotIn("docker stop", self.bench.execute.call_args.args[0])

    def test_invalid_launch_configuration_preserves_the_running_container(self):
        self.config.pop("codeserver_port")
        with self.assertRaises(KeyError):
            self.bench.start()
        self.assertEqual(self.bench.execute.call_count, 1)
        self.assertNotIn("docker stop", self.bench.execute.call_args.args[0])

    def test_recreated_container_keeps_configured_resource_limits(self):
        self.config.update(memory_high=256, memory_max=512, memory_swap=512, vcpu=1.5)
        self.bench.start()
        args = shlex.split(self.bench.execute.call_args.args[0])
        for option in ("--memory-reservation=256M", "--memory=512M", "--memory-swap=512M", "--cpus=1.5"):
            self.assertIn(option, args)

    def test_other_apps_keep_existing_docker_security_defaults(self):
        self.apps_file.write_text("frappe\nerpnext\n")
        self.bench.start()
        self.assertEqual(self.bench.execute.call_count, 3)
        self.assertNotIn("--security-opt", self.bench.execute.call_args.args[0])

    def test_legacy_stack_deploy_is_preserved(self):
        self.config["single_container"] = False
        self.bench.start()
        self.assertEqual(self.bench.execute.call_count, 1)
        self.assertTrue(self.bench.execute.call_args.args[0].startswith("docker stack deploy"))

    def test_policy_loader_supports_old_and_new_apparmor_parsers(self):
        for major, expected in ((3, False), (4, True)):
            with self.subTest(major=major), patch(
                "agent.cli_sandbox.APPARMOR_PROFILE", self.root / PROFILE_NAME
            ), patch(
                "agent.cli_sandbox.subprocess.check_output",
                return_value=f"AppArmor parser version {major}.0.0\n",
            ), patch("agent.cli_sandbox.subprocess.run") as run:
                setup_cli_sandbox()
                policy = self.root / PROFILE_NAME
                text = policy.read_text()
                self.assertEqual("  userns," in text, expected)
                self.assertIn("  deny @{PROC}/{bus,fs,irq,sys}{,/**} wklx,", text)
                self.assertEqual(policy.stat().st_mode & 0o777, 0o644)
                self.assertEqual(run.call_args.args[0][:2], ["apparmor_parser", "--replace"])
                self.assertTrue(run.call_args.kwargs["check"])

    def test_rejected_profile_preserves_the_previous_persistent_policy(self):
        policy = self.root / PROFILE_NAME
        policy.write_text("previous policy")
        with patch("agent.cli_sandbox.APPARMOR_PROFILE", policy), patch(
            "agent.cli_sandbox.subprocess.check_output", return_value="AppArmor parser version 3.0.0\n"
        ), patch(
            "agent.cli_sandbox.subprocess.run", side_effect=RuntimeError("invalid policy")
        ), self.assertRaisesRegex(RuntimeError, "invalid policy"):
            setup_cli_sandbox()
        self.assertEqual(policy.read_text(), "previous policy")
        self.assertEqual(list(self.root.glob(f".{PROFILE_NAME}-*")), [])

    def test_seccomp_keeps_default_denial_and_restricts_unshare_to_user_namespaces(self):
        policy = json.loads(SECCOMP_PROFILE.read_text())
        self.assertEqual(policy["defaultAction"], "SCMP_ACT_ERRNO")
        unshare = next(rule for rule in policy["syscalls"] if rule["names"] == ["unshare"])
        self.assertEqual(unshare["args"], [{"index": 0, "value": 0x10000000, "op": "SCMP_CMP_EQ"}])
        self.assertFalse(
            any("bpf" in rule["names"] and not rule.get("includes") for rule in policy["syscalls"])
        )


class TestCLISandboxUpgrade(unittest.TestCase):
    def test_interrupted_upgrade_is_recovered_even_when_container_is_stopped_or_labelled(self):
        with TemporaryDirectory() as root:
            (Path(root) / CHECKPOINT).write_text("{}")
            bench = SimpleNamespace(
                directory=root, requires_cli_sandbox=True, bench_config={"single_container": True}
            )
            for containers in ("", "interrupted 1"):
                with self.subTest(containers=containers):
                    server = SimpleNamespace(
                        benches_directory=root,
                        benches={"interrupted": bench},
                        execute=MagicMock(return_value={"output": containers}),
                    )
                    with patch("agent.server.Server", return_value=server), patch(
                        "agent.patches.enable_cli_sandbox.restart_for_cli_sandbox"
                    ) as restart:
                        execute()
                    restart.assert_called_once_with(bench)

    def test_upgrade_only_recreates_running_ai_benches_without_current_policy(self):
        with TemporaryDirectory() as root:

            def bench(ai=True, single=True):
                return SimpleNamespace(
                    directory=root,
                    requires_cli_sandbox=ai,
                    bench_config={"single_container": single},
                    start=MagicMock(),
                )

            benches = {
                "running": bench(),
                "already-updated": bench(),
                "stopped": bench(),
                "other-app": bench(ai=False),
                "legacy": bench(single=False),
            }
            server = SimpleNamespace(
                benches_directory=root,
                benches=benches,
                execute=MagicMock(
                    return_value={"output": "running \nalready-updated 1\nother-app \nlegacy \n"}
                ),
            )
            with patch("agent.server.Server", return_value=server), patch(
                "agent.patches.enable_cli_sandbox.restart_for_cli_sandbox"
            ) as restart:
                execute()
            restart.assert_called_once_with(benches["running"])
            for name in ("already-updated", "stopped", "other-app", "legacy"):
                benches[name].start.assert_not_called()

    def test_non_app_servers_do_not_require_docker(self):
        with TemporaryDirectory() as root:
            for directory in (root, str(Path(root) / "missing")):
                with self.subTest(directory=directory):
                    server = SimpleNamespace(benches_directory=directory, benches={}, execute=MagicMock())
                    with patch("agent.server.Server", return_value=server):
                        execute()
                    server.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
