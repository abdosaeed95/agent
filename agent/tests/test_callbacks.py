# Copyright (c) 2026, Fodista and contributors

from unittest import TestCase
from unittest.mock import MagicMock, patch

from agent.callbacks import callback


class TestCallbacks(TestCase):
    def test_callback_has_http_timeout(self):
        job = MagicMock(id="123")

        with patch("agent.server.Server") as server, patch("agent.callbacks.requests.post") as post:
            server.return_value.press_url = "https://press.example.com"

            callback(job, None, None)

        post.assert_called_once_with(
            url="https://press.example.com/api/method/press.api.callbacks.callback",
            data={"job_id": "123"},
            timeout=10,
        )
