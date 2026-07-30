from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from subprocess import Popen
from typing import TYPE_CHECKING

import docker
from filelock import FileLock

from agent.base import Base
from agent.exceptions import RegistryDownException
from agent.job import Job, Step, job, step
from agent.utils import is_registry_healthy

if TYPE_CHECKING:
    from typing import Literal

    OutputKey = Literal["build", "push"]
    Output = dict[OutputKey, list[str]]


class ImageBuilder(Base):
    BUILDER_NAME = "press-image-builder"
    BUILDER_LOCK = "/tmp/press-image-builder.lock"
    MAX_BUILD_ATTEMPTS = 3
    REGISTRY_RETRY_DELAY = 60
    REGISTRY_ERROR_MARKERS = (
        "connection refused",
        "connection reset by peer",
        "context deadline exceeded",
        "failed to do request",
        "failed to push",
        "i/o timeout",
        "tls handshake timeout",
        "unexpected status from",
    )

    output: Output

    def __init__(
        self,
        filename: str,
        image_repository: str,
        image_tag: str,
        no_cache: bool,
        no_push: bool,
        registry: dict,
        platform: str,
        image_compression: str = "gzip",
        image_compression_level: int = 0,
        force_compression: bool = False,
        oci_mediatypes: bool = False,
        build_runtime_image: bool = False,
        apply_new_build: bool = False,
    ) -> None:
        super().__init__()

        # Image push params
        self.image_repository = image_repository
        self.image_tag = image_tag
        self.registry = registry
        self.platform = platform

        # Build context, params
        self.filename = filename
        self.filepath = os.path.join(
            get_image_build_context_directory(),
            self.filename,
        )
        self.no_cache = no_cache
        self.no_push = no_push
        self.image_compression = image_compression
        self.image_compression_level = image_compression_level
        self.force_compression = force_compression
        self.oci_mediatypes = oci_mediatypes
        self.apply_new_build = apply_new_build
        self.build_runtime_image = apply_new_build and build_runtime_image
        self.image_digest = ""
        self.runtime_image_digest = ""
        self.docker_config_directory = ""
        self.metadata_file = f"{self.filepath}.metadata.json"
        self.runtime_metadata_file = f"{self.filepath}.runtime.metadata.json"
        self.last_published = datetime.now()
        self.build_failed = False

        cwd = os.getcwd()
        self.config_file = os.path.join(cwd, "config.json")

        # Lines from build and push are sent to press for processing
        # and updating the respective Deploy Candidate
        self.output = {
            "build": [],
            "push": [],
        }
        self.push_output_lines = []

        self.job = None
        self.step = None

    @property
    def job_record(self):
        if self.job is None:
            self.job = Job()
        return self.job

    @property
    def step_record(self):
        if self.step is None:
            self.step = Step()
        return self.step

    @step_record.setter
    def step_record(self, value):
        self.step = value

    @job("Run Remote Builder")
    def run_remote_builder(self):
        try:
            return self._build_and_push()
        finally:
            self._cleanup_context()

    def _build_and_push(self):
        self._build_image()
        if self.build_failed:
            raise RuntimeError("Docker image build or registry push failed")

        if self.build_runtime_image and not self.no_push:
            self._build_image(runtime=True)
            if self.build_failed:
                raise RuntimeError("Docker image build or registry push failed")

        if not self.build_failed and not self.no_push:
            self._push_docker_image()
        return self.data

    @step("Build Image")
    def _build_image(self, runtime: bool = False):
        # Note: build command and environment are different from when
        # build runs on the press server.
        environment = self._get_build_environment()
        command = self._get_build_command(runtime)

        if not self.apply_new_build:
            result = self._run(
                command=command,
                environment=environment,
                input_filepath=self.filepath,
            )
            self.output["build"] = []
            self._publish_docker_build_output(result)
            return {"output": self.output["build"]}

        self._ensure_buildx_builder(environment)
        variant_output_start = len(self.output["build"])

        for attempt in range(self.MAX_BUILD_ATTEMPTS):
            result = self._run(
                command=command,
                environment=environment,
                input_filepath=self.filepath,
            )
            del self.output["build"][variant_output_start:]
            self._publish_docker_build_output(result)
            if not self.build_failed:
                self._load_image_digest(runtime)
                break

            if (
                self.no_push
                or attempt == self.MAX_BUILD_ATTEMPTS - 1
                or not self._is_retryable_registry_failure(variant_output_start)
            ):
                raise RuntimeError("Docker image build or registry push failed")

            time.sleep(self.REGISTRY_RETRY_DELAY)

        return {"output": self.output["build"]}

    def _is_retryable_registry_failure(self, output_start: int = 0):
        output = "".join(self.output["build"][output_start:]).lower()
        return any(marker in output for marker in self.REGISTRY_ERROR_MARKERS)

    def _get_build_command(self, runtime: bool = False) -> str:
        if not self.apply_new_build:
            command = f"docker buildx build --platform {self.platform}"
            command = f"{command} -t {self._get_image_name()}"
            if self.no_cache:
                command = f"{command} --no-cache"
            return f"{command} - "

        command = f"docker buildx build --builder {self.BUILDER_NAME} --platform {self.platform}"
        image_name = self._get_image_name(runtime)
        metadata_file = self.runtime_metadata_file if runtime else self.metadata_file
        command = f"{command} -t {image_name}"
        command = f"{command} --metadata-file {metadata_file}"

        if runtime:
            command = f"{command} --target runtime"

        if self.no_cache:
            command = f"{command} --no-cache"

        if runtime and not self.no_push:
            cache_ref = f"{self.image_repository}:runtime-buildcache"
            command = f"{command} --cache-from type=registry,ref={cache_ref}"
            command = (
                f"{command} --cache-to type=registry,ref={cache_ref},mode=min,"
                f"compression={self.image_compression},"
                f"compression-level={self.image_compression_level},"
                "force-compression=false,oci-mediatypes=true,"
                "image-manifest=true,ignore-error=true"
            )

        if self.no_push:
            command = f"{command} --load"
        else:
            command = f"{command} --provenance=false"
            force_compression = self.force_compression and not runtime
            output = ",".join(
                [
                    "type=image",
                    f"name={image_name}",
                    "push=true",
                    f"compression={self.image_compression}",
                    f"compression-level={self.image_compression_level}",
                    f"force-compression={str(force_compression).lower()}",
                    f"oci-mediatypes={str(self.oci_mediatypes).lower()}",
                    "name-canonical=true",
                ]
            )
            command = f"{command} --output {output}"

        return f"{command} - "

    def _ensure_buildx_builder(self, environment: dict):
        with FileLock(self.BUILDER_LOCK):
            result = subprocess.run(
                ["docker", "buildx", "inspect", self.BUILDER_NAME],
                text=True,
                capture_output=True,
                env=environment,
            )
            driver = next(
                (
                    line.partition(":")[2].strip()
                    for line in result.stdout.splitlines()
                    if line.startswith("Driver:")
                ),
                "",
            )
            if not result.returncode and driver != "docker-container":
                subprocess.run(
                    ["docker", "buildx", "rm", self.BUILDER_NAME],
                    check=True,
                    capture_output=True,
                    text=True,
                    env=environment,
                )
                result = subprocess.CompletedProcess(result.args, 1)

            if result.returncode:
                subprocess.run(
                    [
                        "docker",
                        "buildx",
                        "create",
                        "--name",
                        self.BUILDER_NAME,
                        "--driver",
                        "docker-container",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    env=environment,
                )

            subprocess.run(
                ["docker", "buildx", "inspect", self.BUILDER_NAME, "--bootstrap"],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )

    def _get_build_environment(self) -> dict:
        environment = os.environ.copy()
        environment.update(
            {
                "DOCKER_BUILDKIT": "1",
                "BUILDKIT_PROGRESS": "plain",
                "PROGRESS_NO_TRUNC": "1",
            }
        )

        if not self.apply_new_build:
            return environment

        environment["BUILDX_CONFIG"] = environment.get(
            "BUILDX_CONFIG",
            os.path.join(environment.get("DOCKER_CONFIG", os.path.expanduser("~/.docker")), "buildx"),
        )

        if not self.no_push:
            if not self.docker_config_directory:
                self.docker_config_directory = tempfile.mkdtemp(prefix="agent-docker-config-")
                self._login_to_registry(environment)

            environment["DOCKER_CONFIG"] = self.docker_config_directory

        return environment

    def _login_to_registry(self, environment):
        registry_url = self.registry["url"]
        if registry_url == "registry-1.docker.io":
            registry_url = "docker.io"
        command = [
            "docker",
            "login",
            registry_url,
            "--username",
            self.registry["username"],
            "--password-stdin",
        ]
        environment = {**environment, "DOCKER_CONFIG": self.docker_config_directory}
        for attempt in range(self.MAX_BUILD_ATTEMPTS):
            try:
                subprocess.run(
                    command,
                    input=self.registry["password"],
                    text=True,
                    check=True,
                    capture_output=True,
                    env=environment,
                )
                return
            except subprocess.CalledProcessError:
                if attempt == self.MAX_BUILD_ATTEMPTS - 1:
                    raise
                time.sleep(self.REGISTRY_RETRY_DELAY)

    def _load_image_digest(self, runtime: bool = False):
        metadata_file = self.runtime_metadata_file if runtime else self.metadata_file
        if self.build_failed or not os.path.exists(metadata_file):
            return

        with open(metadata_file) as file:
            metadata = json.load(file)

        image_digest = metadata.get("containerimage.digest", "")
        if not image_digest:
            return

        if runtime:
            self.runtime_image_digest = image_digest
            self.data["runtime_image_digest"] = image_digest
        else:
            self.image_digest = image_digest
            self.data["image_digest"] = image_digest
        self.output["build"].append(f"#0 writing image {image_digest} done\n")
        self._publish_throttled_output(True)

    def _publish_docker_build_output(self, result):
        for line in result:
            self.output["build"].append(line)
            self._publish_throttled_output(False)
        self._publish_throttled_output(True)

    @step("Push Docker Image")
    def _push_docker_image(self):
        if not self.apply_new_build:
            return self._push_legacy_docker_image()

        self._verify_pushed_image()
        if self.build_runtime_image:
            self._verify_pushed_image(runtime=True)

        if not is_registry_healthy(
            self.registry["url"], self.registry["username"], self.registry["password"]
        ):
            raise RegistryDownException("Registry became unhealthy after push")

        self.output["push"].append(
            {
                "id": self._get_image_name(),
                "status": "Pushed",
                "progress": self.image_digest,
            }
        )
        if self.build_runtime_image:
            self.output["push"].append(
                {
                    "id": self._get_image_name(runtime=True),
                    "status": "Pushed",
                    "progress": self.runtime_image_digest,
                }
            )
        self._publish_throttled_output(True)
        return self.output["push"]

    def _push_legacy_docker_image(self):
        client = docker.from_env(environment=os.environ.copy(), timeout=5 * 60)

        for attempt in range(self.MAX_BUILD_ATTEMPTS):
            self.output["push"].append({"id": "Retry", "output": "", "status": f"Success {attempt}"})
            try:
                if not is_registry_healthy(
                    self.registry["url"], self.registry["username"], self.registry["password"]
                ):
                    raise RegistryDownException("Registry is currently down")

                self._push_legacy_image(client)

                if not is_registry_healthy(
                    self.registry["url"], self.registry["username"], self.registry["password"]
                ):
                    raise RegistryDownException("Registry became unhealthy after push")

                return self.output["push"]
            except RegistryDownException as error:
                if attempt == self.MAX_BUILD_ATTEMPTS - 1:
                    self._publish_throttled_output(True)
                    raise RuntimeError("Failed to push image after multiple attempts") from error
                time.sleep(self.REGISTRY_RETRY_DELAY)
            except Exception:
                self._publish_throttled_output(True)
                raise

        return self.output["push"]

    def _push_legacy_image(self, client):
        auth_config = {
            "username": self.registry["username"],
            "password": self.registry["password"],
            "serveraddress": self.registry["url"],
        }
        for line in client.images.push(
            self.image_repository,
            self.image_tag,
            stream=True,
            decode=True,
            auth_config=auth_config,
        ):
            self.output["push"].append(line)
            self._publish_throttled_output(False)

    def _verify_pushed_image(self, runtime: bool = False):
        image_digest = self.runtime_image_digest if runtime else self.image_digest
        if not image_digest:
            raise RuntimeError("BuildKit did not return an image digest")

        environment = os.environ.copy()
        environment["DOCKER_CONFIG"] = self.docker_config_directory
        subprocess.run(
            [
                "docker",
                "buildx",
                "imagetools",
                "inspect",
                f"{self._get_image_name(runtime)}@{image_digest}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5 * 60,
            env=environment,
        )

    def _publish_throttled_output(self, flush: bool):
        if flush:
            self.publish_data(self.output)
            return

        now = datetime.now()
        if (now - self.last_published).total_seconds() <= 1:
            return

        self.last_published = now
        self.publish_data(self.output)

    def _get_image_name(self, runtime: bool = False):
        image_tag = f"{self.image_tag}-slim" if runtime else self.image_tag
        return f"{self.image_repository}:{image_tag}"

    def _run(
        self,
        command: str,
        environment: dict,
        input_filepath: str,
    ):
        with open(input_filepath, "rb") as input_file:
            process = Popen(
                shlex.split(command),
                stdin=input_file,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=environment,
                universal_newlines=True,
            )

        yield from process.stdout

        process.stdout.close()
        input_file.close()

        return_code = process.wait()
        self._publish_throttled_output(True)

        self.build_failed = return_code != 0
        self.data.update({"build_failed": self.build_failed})

    @step("Cleanup Context")
    def _cleanup_context(self):
        cleaned = False
        for path in [self.filepath, self.metadata_file, self.runtime_metadata_file]:
            if os.path.exists(path):
                os.remove(path)
                cleaned = True

        if self.docker_config_directory:
            shutil.rmtree(self.docker_config_directory, ignore_errors=True)
            cleaned = True

        return {"cleanup": cleaned}


def get_image_build_context_directory():
    path = os.path.join(os.getcwd(), "build_context")
    if not os.path.exists(path):
        os.makedirs(path)
    return path
