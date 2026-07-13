from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
from datetime import datetime
from subprocess import Popen
from typing import TYPE_CHECKING

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
        image_compression: str = "zstd",
        image_compression_level: int = 22,
        force_compression: bool = True,
        oci_mediatypes: bool = True,
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
        self.image_digest = ""
        self.docker_config_directory = ""
        self.metadata_file = f"{self.filepath}.metadata.json"
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
        if not self.no_push and not is_registry_healthy(
            self.registry["url"], self.registry["username"], self.registry["password"]
        ):
            raise RegistryDownException("Registry is currently down")

        self._build_image()
        if not self.build_failed and not self.no_push:
            self._push_docker_image()
        return self.data

    @step("Build Image")
    def _build_image(self):
        # Note: build command and environment are different from when
        # build runs on the press server.
        self._ensure_buildx_builder()
        command = self._get_build_command()
        environment = self._get_build_environment()
        result = self._run(
            command=command,
            environment=environment,
            input_filepath=self.filepath,
        )
        self.output["build"] = []
        self._publish_docker_build_output(result)
        self._load_image_digest()
        return {"output": self.output["build"]}

    def _get_build_command(self) -> str:
        command = f"docker buildx build --builder {self.BUILDER_NAME} --platform {self.platform}"
        command = f"{command} -t {self._get_image_name()}"
        command = f"{command} --metadata-file {self.metadata_file}"

        if self.no_cache:
            command = f"{command} --no-cache"

        if self.no_push:
            command = f"{command} --load"
        else:
            command = f"{command} --provenance=false"
            output = ",".join(
                [
                    "type=image",
                    f"name={self._get_image_name()}",
                    "push=true",
                    f"compression={self.image_compression}",
                    f"compression-level={self.image_compression_level}",
                    f"force-compression={str(self.force_compression).lower()}",
                    f"oci-mediatypes={str(self.oci_mediatypes).lower()}",
                    "name-canonical=true",
                ]
            )
            command = f"{command} --output {output}"

        return f"{command} - "

    def _ensure_buildx_builder(self):
        with FileLock(self.BUILDER_LOCK):
            result = subprocess.run(
                ["docker", "buildx", "inspect", self.BUILDER_NAME],
                text=True,
                capture_output=True,
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
                )

            subprocess.run(
                ["docker", "buildx", "inspect", self.BUILDER_NAME, "--bootstrap"],
                check=True,
                capture_output=True,
                text=True,
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

        if not self.no_push:
            self.docker_config_directory = tempfile.mkdtemp(prefix="agent-docker-config-")
            environment["DOCKER_CONFIG"] = self.docker_config_directory
            subprocess.run(
                [
                    "docker",
                    "login",
                    self.registry["url"],
                    "--username",
                    self.registry["username"],
                    "--password-stdin",
                ],
                input=self.registry["password"],
                text=True,
                check=True,
                capture_output=True,
                env=environment,
            )

        return environment

    def _load_image_digest(self):
        if self.build_failed or not os.path.exists(self.metadata_file):
            return

        with open(self.metadata_file) as file:
            metadata = json.load(file)

        self.image_digest = metadata.get("containerimage.digest", "")
        if not self.image_digest:
            return

        self.data["image_digest"] = self.image_digest
        self.output["build"].append(f"#0 writing image {self.image_digest} done\n")
        self._publish_throttled_output(True)

    def _publish_docker_build_output(self, result):
        for line in result:
            self.output["build"].append(line)
            self._publish_throttled_output(False)
        self._publish_throttled_output(True)

    @step("Push Docker Image")
    def _push_docker_image(self):
        self._verify_pushed_image()

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
        self._publish_throttled_output(True)
        return self.output["push"]

    def _verify_pushed_image(self):
        if not self.image_digest:
            raise RuntimeError("BuildKit did not return an image digest")

        environment = os.environ.copy()
        environment["DOCKER_CONFIG"] = self.docker_config_directory
        subprocess.run(
            [
                "docker",
                "buildx",
                "imagetools",
                "inspect",
                f"{self._get_image_name()}@{self.image_digest}",
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

    def _get_image_name(self):
        return f"{self.image_repository}:{self.image_tag}"

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
        for path in [self.filepath, self.metadata_file]:
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
