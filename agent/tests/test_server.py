from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from agent.server import Server


class TestServerProxyDetection(unittest.TestCase):
    def _get_server(self, config: dict) -> Server:
        with patch.object(Server, "__init__", new=lambda self: None):
            server = Server()
        server.get_config = MagicMock(return_value=config)
        server.directory = "."
        server.setup_supervisor = MagicMock()
        server.setup_nginx = MagicMock()
        server._config_file_lock = None
        return server

    def test_update_agent_cli_starts_nginx_manager_for_proxy_flag(self):
        server = self._get_server(
            {
                "name": "proxy-server",
                "is_proxy_server": True,
                "domain": "",
                "workers": 0,
            }
        )

        commands = []

        def fake_execute(command, *args, **kwargs):
            commands.append(command)
            return {"output": ""}

        with patch(
            "agent.server.get_supervisor_processes_status",
            side_effect=[
                {"web": "RUNNING", "worker": {}, "redis": "RUNNING"},
                {"redis": "RUNNING"},
            ],
        ), patch.object(Server, "execute", side_effect=fake_execute):
            server.update_agent_cli(
                restart_redis=False,
                restart_rq_workers=False,
                restart_web_workers=False,
                skip_repo_setup=False,
                skip_patches=True,
            )

        self.assertIn("sudo supervisorctl stop agent:nginx_reload_manager", commands)
        self.assertIn("sudo supervisorctl start agent:nginx_reload_manager", commands)

    def test_generate_supervisor_config_marks_proxy_when_domain_present(self):
        server = self._get_server(
            {
                "name": "app-server",
                "domain": "example.com",
                "workers": 1,
                "web_port": 8000,
                "redis_port": 11000,
                "user": "frappe",
            }
        )

        with patch.object(Server, "_render_template") as render_template:
            server._generate_supervisor_config()

        args, _ = render_template.call_args
        _, context, _ = args
        self.assertTrue(context.get("is_proxy_server"))

    def test_generate_supervisor_config_respects_false_proxy_flag(self):
        server = self._get_server(
            {
                "name": "app-server",
                "domain": "example.com",
                "is_proxy_server": False,
                "workers": 1,
                "web_port": 8000,
                "redis_port": 11000,
                "user": "frappe",
            }
        )

        with patch.object(Server, "_render_template") as render_template:
            server._generate_supervisor_config()

        args, _ = render_template.call_args
        _, context, _ = args
        self.assertFalse(context.get("is_proxy_server", False))


if __name__ == "__main__":
    unittest.main()
