"""Run inside the bench's environment; stdout ends with one JSON result."""

from __future__ import annotations

import base64
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import requests
from redis import Redis
from rq import Queue, Worker
from rq.registry import StartedJobRegistry
from rq.suspension import WORKERS_SUSPENDED, suspend


def processes():
    result = subprocess.run(["supervisorctl", "status"], text=True, stdout=subprocess.PIPE, check=False)
    if result.returncode not in (0, 3):
        sys.exit("Could not read Supervisor process state")
    return {parts[0]: parts[1] for line in result.stdout.splitlines() if (parts := line.split())}


def control(action, name):
    subprocess.run(["supervisorctl", *action.split(), name], check=True, stdout=subprocess.DEVNULL)


def scheduler_alive():
    try:
        with Path("config/scheduler_process").open() as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(lock, fcntl.LOCK_UN)
    except OSError:
        pass
    return False


def desired_processes(states):
    # Worker pools run the scheduler in a thread; the standalone scheduler then exits normally.
    return {
        name: "RUNNING" if name.endswith("-frappe-schedule") and status == "EXITED" else status
        for name, status in states.items()
    }


def processes_ready(expected):
    scheduling = any(
        name.endswith("-frappe-schedule") and status == "RUNNING" for name, status in expected.items()
    )
    return desired_processes(processes()) == expected and (not scheduling or scheduler_alive())


def queue_connection():
    config = json.loads(Path("sites/common_site_config.json").read_text())
    return Redis.from_url(config["redis_queue"], socket_connect_timeout=3, socket_timeout=10)


def snapshot(connection):
    states = desired_processes(processes())
    if not states or any(state not in ("RUNNING", "STOPPED") for state in states.values()):
        sys.exit("Bench processes must be stable before migration")
    directory = Path(connection.config_get("dir")["dir"]).resolve()
    if not directory.is_relative_to(Path("config").resolve()):
        sys.exit("Redis queue persistence must be inside the persistent config mount")
    value = connection.get(WORKERS_SUSPENDED)
    return {
        "processes": states,
        "queue": {
            "value": base64.b64encode(value).decode() if value is not None else None,
            "ttl": connection.pttl(WORKERS_SUSPENDED),
            "captured_at": time.time(),
        },
    }


def workers_idle(connection, require_workers=False):
    active = any(
        connection.zcard(StartedJobRegistry(queue=queue).key) for queue in Queue.all(connection=connection)
    )
    workers = Worker.all(connection=connection)
    return (
        not active
        and (bool(workers) or not require_workers)
        and all(worker.get_state() == "suspended" and not worker.get_current_job_id() for worker in workers)
    )


def stop_producers(connection):
    states = processes()
    workers = [name for name in states if name.startswith("frappe-bench-workers:") and "schedule" not in name]
    for name, status in states.items():
        if status == "RUNNING" and (name.startswith("frappe-bench-web:") or "schedule" in name):
            control("stop", name)
    # A retry can find a worker still finishing its first warm shutdown.
    if not any(worker.shutdown_requested_date for worker in Worker.all(connection=connection)):
        for name in workers:
            if states[name] == "RUNNING":
                control("signal TERM", name)
    return workers


def drain(connection, state, deadline):
    suspend(connection)
    workers = stop_producers(connection)
    while time.monotonic() < deadline:
        if workers_idle(connection):
            stop_idle_workers(connection, workers)
            if not connection.info("persistence")["rdb_bgsave_in_progress"]:
                connection.save()
                return {}
        time.sleep(1)
    sys.exit("Worker drain timed out; no active job was forcibly stopped")


def stop_idle_workers(connection, workers):
    finish_idle_stops(connection)
    running = [name for name, status in processes().items() if name in workers and status == "RUNNING"]
    if not running:
        return
    try:
        subprocess.run(
            ["supervisorctl", "stop", *running],
            check=True,
            stdout=subprocess.DEVNULL,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        if not workers_idle(connection):
            sys.exit("Worker state changed during shutdown; refusing forced cleanup")
        for name in running:
            kill_idle_group(connection, name)


def kill_idle_group(connection, name):
    pid = int(subprocess.check_output(["supervisorctl", "pid", name], text=True))
    if pid <= 1:
        return
    stopped = []
    for worker in Worker.all(connection=connection):
        try:
            if os.getpgid(worker.pid) == pid:
                stopped.append(worker)
        except ProcessLookupError:
            pass
    with suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)
    for worker in stopped:
        worker.register_death()


def finish_idle_stops(connection):
    stopping = [
        name
        for name, status in processes().items()
        if name.startswith("frappe-bench-workers:") and "schedule" not in name and status == "STOPPING"
    ]
    if stopping and workers_idle(connection):
        for name in stopping:
            kill_idle_group(connection, name)


def restore_processes(connection, state, deadline):
    while time.monotonic() < deadline:
        finish_idle_stops(connection)
        states = processes()
        if set(states) != set(state["processes"]):
            sys.exit("Replacement Supervisor configuration does not match the original bench")
        for name, original in state["processes"].items():
            if needs_start(name, states[name], original):
                control("start", name)
            elif original == "STOPPED" and states[name] == "RUNNING":
                control("stop", name)
        if processes_ready(state["processes"]):
            return {}
        time.sleep(1)
    sys.exit("Bench processes did not return to their original state")


def needs_start(name, current, original):
    if original != "RUNNING" or current not in ("STOPPED", "EXITED"):
        return False
    return not (name.endswith("-frappe-schedule") and scheduler_alive())


def site_health(site, web_running):
    import frappe

    directory = Path.cwd()
    os.chdir(directory / "sites")
    try:
        frappe.init(site=site)
        frappe.connect()
        frappe.db.sql("SELECT 1")
        if not web_running:
            return True
        response = requests.get(
            "http://127.0.0.1:8000/api/method/ping",
            headers={"Host": site, "Accept": "application/json"},
            timeout=3,
        )
        maintenance = frappe.conf.maintenance_mode and not frappe.conf.allow_reads_during_maintenance
        if maintenance:
            return response.status_code == 503 and response.json().get("exc_type") == "SessionStopped"
        return response.status_code == 200 and response.json().get("message") == "pong"
    finally:
        os.chdir(directory)
        frappe.destroy()


def health_once(connection, state):
    if not processes_ready(state["processes"]):
        return False
    connection.ping()
    if state.get("quiesced") and not queue_health(connection, state):
        return False
    config = json.loads(Path("sites/common_site_config.json").read_text())
    Redis.from_url(config["redis_cache"], socket_connect_timeout=3, socket_timeout=3).ping()
    web = state["processes"].get("frappe-bench-web:frappe-bench-frappe-web")
    if not all(site_health(site, web == "RUNNING") for site in state["sites"]):
        return False
    socketio = state["processes"].get("frappe-bench-web:frappe-bench-node-socketio")
    if socketio == "RUNNING":
        response = requests.get("http://127.0.0.1:9000/socket.io/?EIO=4&transport=polling", timeout=3)
        if response.status_code != 200 or not response.text.startswith("0{"):
            return False
    return True


def queue_health(connection, state):
    workers_running = any(
        name.startswith("frappe-bench-workers:") and "schedule" not in name and status == "RUNNING"
        for name, status in state["processes"].items()
    )
    return connection.exists(WORKERS_SUSPENDED) and workers_idle(connection, require_workers=workers_running)


def health(connection, state, deadline):
    healthy = 0
    while time.monotonic() < deadline:
        try:
            if health_once(connection, state):
                healthy += 1
                if healthy == 3:
                    return {}
            else:
                healthy = 0
        except Exception:
            healthy = 0
        time.sleep(1)
    sys.exit("Bench health check failed: Supervisor, Redis, site database/HTTP, or Socket.IO")


def restore_queue(connection, state):
    original = state["queue"]
    remaining = original["ttl"] - int((time.time() - original["captured_at"]) * 1000)
    if original["value"] is None or (original["ttl"] >= 0 and remaining <= 0):
        connection.delete(WORKERS_SUSPENDED)
    else:
        connection.set(
            WORKERS_SUSPENDED,
            base64.b64decode(original["value"]),
            px=remaining if original["ttl"] >= 0 else None,
        )
    connection.save()
    return {}


def main():
    action, encoded, timeout = sys.argv[1:]
    state = json.loads(encoded)
    deadline = time.monotonic() + int(timeout)
    connection = queue_connection()
    if action == "snapshot":
        result = snapshot(connection)
    elif action == "drain":
        result = drain(connection, state, deadline)
    elif action == "restore-processes":
        result = restore_processes(connection, state, deadline)
    elif action == "restore-queue":
        result = restore_queue(connection, state)
    elif action == "health":
        result = health(connection, state, deadline)
    else:
        sys.exit("Unknown CLI sandbox migration operation")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
