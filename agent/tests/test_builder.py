# Copyright (c) 2026, Fodista and contributors
# See license.txt

import unittest
from subprocess import CalledProcessError, CompletedProcess
from unittest.mock import Mock, call, patch

from agent.builder import ImageBuilder


class TestImageBuilder(unittest.TestCase):
    def get_builder(self, no_push=False, maximum_compression=True, build_runtime_image=False):
        compression = {
            "image_compression": "zstd",
            "image_compression_level": 22,
            "force_compression": True,
            "oci_mediatypes": True,
        }
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
                build_runtime_image=build_runtime_image,
                **(compression if maximum_compression else {}),
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

    def test_default_compression_preserves_existing_clients(self):
        command = self.get_builder(maximum_compression=False)._get_build_command()

        self.assertIn("compression=gzip", command)
        self.assertIn("compression-level=0", command)
        self.assertIn("force-compression=false", command)
        self.assertIn("oci-mediatypes=false", command)

    def test_no_push_build_stays_local(self):
        command = self.get_builder(no_push=True)._get_build_command()

        self.assertIn("--load", command)
        self.assertNotIn("--output", command)
        self.assertNotIn("push=true", command)

    def test_runtime_build_uses_runtime_target_and_tag(self):
        command = self.get_builder(build_runtime_image=True)._get_build_command(runtime=True)

        self.assertIn("--target runtime", command)
        self.assertIn("-t registry.example.com/fodista/bench:candidate-runtime", command)
        self.assertIn("name=registry.example.com/fodista/bench:candidate-runtime", command)
        self.assertIn(".runtime.metadata.json", command)

    @patch.object(ImageBuilder, "_push_docker_image")
    @patch.object(ImageBuilder, "_build_image")
    def test_runtime_image_is_built_after_full_image(self, build_image, _push_image):
        builder = self.get_builder(build_runtime_image=True)
        builder.data = {}

        builder._build_and_push()

        self.assertEqual(build_image.call_args_list, [call(), call(runtime=True)])

    @patch.object(ImageBuilder, "_push_docker_image")
    @patch.object(ImageBuilder, "_build_image")
    def test_runtime_image_is_not_built_by_default(self, build_image, _push_image):
        builder = self.get_builder()
        builder.data = {}

        builder._build_and_push()

        build_image.assert_called_once_with()

    @patch.object(ImageBuilder, "_push_docker_image")
    @patch.object(ImageBuilder, "_build_image")
    def test_no_push_does_not_build_runtime_image(self, build_image, push_image):
        builder = self.get_builder(no_push=True, build_runtime_image=True)
        builder.data = {}

        builder._build_and_push()

        build_image.assert_called_once_with()
        push_image.assert_not_called()

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

    @patch("agent.builder.subprocess.run")
    def test_runtime_digest_is_verified_against_runtime_tag(self, run):
        builder = self.get_builder(build_runtime_image=True)
        builder.runtime_image_digest = "sha256:def456"
        builder.docker_config_directory = "/tmp/docker-config"

        builder._verify_pushed_image(runtime=True)

        self.assertIn(
            "registry.example.com/fodista/bench:candidate-runtime@sha256:def456",
            run.call_args.args[0],
        )

    @patch("agent.builder.is_registry_healthy", return_value=True)
    @patch.object(ImageBuilder, "_verify_pushed_image")
    def test_full_and_runtime_digests_are_verified_after_push(self, verify, _registry):
        builder = self.get_builder(build_runtime_image=True)
        builder.data = {}
        builder.image_digest = "sha256:abc123"
        builder.runtime_image_digest = "sha256:def456"
        builder.job = Mock()
        builder.job.model.id = 1
        builder.step = Mock()
        builder.publish_data = Mock()

        output = builder._push_docker_image()

        self.assertEqual(verify.call_args_list, [call(), call(runtime=True)])
        self.assertEqual(
            [item["id"] for item in output],
            [
                "registry.example.com/fodista/bench:candidate",
                "registry.example.com/fodista/bench:candidate-runtime",
            ],
        )

    def test_push_fails_without_digest(self):
        builder = self.get_builder()

        with self.assertRaisesRegex(RuntimeError, "did not return an image digest"):
            builder._verify_pushed_image()

    def test_registry_push_failure_is_retryable(self):
        builder = self.get_builder()
        builder.output["build"] = ["ERROR: failed to push: connection reset by peer\n"]

        self.assertTrue(builder._is_retryable_registry_failure())

    def test_dockerfile_failure_is_not_retryable(self):
        builder = self.get_builder()
        builder.output["build"] = ['ERROR: process "/bin/sh -c false" did not complete successfully\n']

        self.assertFalse(builder._is_retryable_registry_failure())

    def test_runtime_retry_ignores_previous_full_image_output(self):
        builder = self.get_builder()
        builder.output["build"] = [
            "failed to push: connection reset by peer\n",
            'ERROR: process "/bin/sh -c false" did not complete successfully\n',
        ]

        self.assertFalse(builder._is_retryable_registry_failure(output_start=1))

    @patch.object(ImageBuilder, "_build_image")
    def test_failed_build_cannot_complete_successfully(self, build_image):
        builder = self.get_builder(no_push=True)
        build_image.side_effect = lambda: setattr(builder, "build_failed", True)

        with self.assertRaisesRegex(RuntimeError, "build or registry push failed"):
            builder._build_and_push()

    @patch("agent.builder.subprocess.run")
    def test_missing_builder_uses_docker_container_driver(self, run):
        run.side_effect = [
            CompletedProcess([], 1, stdout=""),
            CompletedProcess([], 0, stdout=""),
            CompletedProcess([], 0, stdout=""),
        ]
        environment = {"BUILDX_CONFIG": "/persistent/docker/buildx"}

        self.get_builder()._ensure_buildx_builder(environment)

        self.assertEqual(run.call_args_list[1].args[0][-2:], ["--driver", "docker-container"])
        self.assertEqual(run.call_args_list[2].args[0][-1], "--bootstrap")
        self.assertIs(run.call_args_list[0].kwargs["env"], environment)
        self.assertIs(run.call_args_list[1].kwargs["env"], environment)
        self.assertIs(run.call_args_list[2].kwargs["env"], environment)

    @patch("agent.builder.tempfile.mkdtemp", return_value="/tmp/docker-config")
    @patch("agent.builder.subprocess.run")
    def test_registry_password_uses_stdin_and_keeps_builder_config(self, run, _mkdtemp):
        with patch.dict("agent.builder.os.environ", {"DOCKER_CONFIG": "/persistent/docker"}, clear=True):
            environment = self.get_builder()._get_build_environment()

        command = run.call_args.args[0]
        self.assertIn("--password-stdin", command)
        self.assertNotIn("password", command)
        self.assertEqual(run.call_args.kwargs["input"], "password")
        self.assertEqual(environment["DOCKER_CONFIG"], "/tmp/docker-config")
        self.assertEqual(environment["BUILDX_CONFIG"], "/persistent/docker/buildx")

    @patch("agent.builder.time.sleep")
    @patch("agent.builder.subprocess.run")
    def test_registry_login_retries_transient_failure(self, run, sleep):
        run.side_effect = [
            CalledProcessError(1, ["docker", "login"]),
            CompletedProcess([], 0),
        ]
        builder = self.get_builder()
        builder.docker_config_directory = "/tmp/docker-config"

        builder._login_to_registry({})

        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(builder.REGISTRY_RETRY_DELAY)


if __name__ == "__main__":
    unittest.main()
