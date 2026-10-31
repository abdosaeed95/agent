from __future__ import annotations

from pathlib import Path
from time import monotonic

from agent.bench_restart import CHECKPOINT, restart_for_cli_sandbox
from agent.cli_sandbox import POLICY_LABEL, POLICY_VERSION


def execute():
    from agent.server import Server

    server = Server()
    if not Path(server.benches_directory).is_dir():
        return
    benches = {
        name: bench
        for name, bench in server.benches.items()
        if bench.bench_config.get("single_container") and bench.requires_cli_sandbox
    }
    if not benches:
        return
    containers = server.execute(f"docker ps --format '{{{{.Names}}}} {{{{.Label \"{POLICY_LABEL}\"}}}}'")[
        "output"
    ]
    running = {}
    for line in containers.splitlines():
        name, _, version = line.partition(" ")
        running[name] = version
    deadline = monotonic() + 5400
    for name, bench in benches.items():
        recovering = (Path(bench.directory) / CHECKPOINT).exists()
        if recovering or (name in running and running[name] != POLICY_VERSION):
            if monotonic() > deadline:
                server.execute(
                    "echo 'CLI sandbox migration time budget reached; retry the Agent update.' >&2; exit 1"
                )
            restart_for_cli_sandbox(bench)
