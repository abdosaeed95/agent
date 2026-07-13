# Copyright (c) 2026, Fodista and contributors
# See license.txt

import unittest
from subprocess import CompletedProcess
from unittest.mock import patch

from agent.builder import ImageBuilder


class TestImageBuilder(unittest.TestCase):
    def get_builder(self, no_push=False):
        with patch("agent.builder.Base.__init__", return_value=None):
            return ImageBuilder(
                filename="context.tar.gz",
                image_repository="registry.example.com/fodista/bench",
                image_tag="candidate",
                no_cache=False,
                no_push=no_push,
                registry={
                    "url": "registry.example.com",
                    "username": "user",
                    "password": "password",
                },
                platform="linux/amd64",
            )

    def test_build_command_pushes_maximum_zstd_compression(self):
        command = self.get_builder()._get_build_command()

        self.assertIn("type=image", command)
        self.assertIn("--builder press-image-builder", command)
        self.assertIn("push=true", command)
        self.assertIn("compression=zstd", command)
        self.assertIn("compression-level=22", command)
        self.assertIn("force-compression=true", command)
        self.assertIn("oci-mediatypes=true", command)
        self.assertIn("name-canonical=true", command)

    def test_no_push_build_stays_local(self):
        command = self.get_builder(no_push=True)._get_build_command()

        self.assertIn("--load", command)
        self.assertNotIn("--output", command)
        self.assertNotIn("push=true", command)

    @patch("agent.builder.subprocess.run")
    def test_pushed_digest_is_verified(self, run):
        builder = self.get_builder()
        builder.image_digest = "sha256:abc123"
        builder.docker_config_directory = "/tmp/docker-config"

        builder._verify_pushed_image()

        self.assertIn(
            "registry.example.com/fodista/bench:candidate@sha256:abc123",
            run.call_args.args[0],
        )
        self.assertEqual(run.call_args.kwargs["timeout"], 5 * 60)

    def test_push_fails_without_digest(self):
        builder = self.get_builder()

        with self.assertRaisesRegex(RuntimeError, "did not return an image digest"):
            builder._verify_pushed_image()

    @patch("agent.builder.subprocess.run")
    def test_missing_builder_uses_docker_container_driver(self, run):
        run.side_effect = [
            CompletedProcess([], 1, stdout=""),
            CompletedProcess([], 0, stdout=""),
            CompletedProcess([], 0, stdout=""),
        ]

        self.get_builder()._ensure_buildx_builder()

        self.assertEqual(run.call_args_list[1].args[0][-2:], ["--driver", "docker-container"])
        self.assertEqual(run.call_args_list[2].args[0][-1], "--bootstrap")

    @patch("agent.builder.tempfile.mkdtemp", return_value="/tmp/docker-config")
    @patch("agent.builder.subprocess.run")
    def test_registry_password_uses_stdin(self, run, _mkdtemp):
        with patch.dict("agent.builder.os.environ", {"DOCKER_CONFIG": "/persistent/docker"}, clear=True):
            environment = self.get_builder()._get_build_environment()

        command = run.call_args.args[0]
        self.assertIn("--password-stdin", command)
        self.assertNotIn("password", command)
        self.assertEqual(run.call_args.kwargs["input"], "password")
        self.assertEqual(environment["DOCKER_CONFIG"], "/tmp/docker-config")
        self.assertEqual(environment["BUILDX_CONFIG"], "/persistent/docker/buildx")


if __name__ == "__main__":
    unittest.main()
