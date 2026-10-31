from __future__ import annotations

import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from jinja2 import Environment

PROFILE_NAME = "frappe-bench-cli"
POLICY_VERSION = "1"
POLICY_LABEL = "io.fodista.cli-sandbox"
SECCOMP_PROFILE = Path(__file__).parent / "templates" / "bench" / "cli-seccomp.json"
APPARMOR_PROFILE = Path("/etc/apparmor.d") / PROFILE_NAME


def setup_cli_sandbox() -> None:
    version = subprocess.check_output(["apparmor_parser", "--version"], text=True)
    parser_major = int(version.splitlines()[0].split()[-1].split(".")[0])
    template = Environment().from_string(SECCOMP_PROFILE.with_name("cli-apparmor.jinja2").read_text())
    with TemporaryDirectory(dir=APPARMOR_PROFILE.parent, prefix=f".{PROFILE_NAME}-") as directory:
        policy = Path(directory) / PROFILE_NAME
        policy.write_text(template.render(profile_name=PROFILE_NAME, userns=parser_major >= 4) + "\n")
        policy.chmod(0o644)
        subprocess.run(["apparmor_parser", "--replace", str(policy)], check=True)
        policy.replace(APPARMOR_PROFILE)
