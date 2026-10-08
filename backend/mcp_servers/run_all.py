"""Start every MCP server built so far, one process each (make mcp; ARCHITECTURE.md §3, §5).

Run from backend/: uv run python -m mcp_servers.run_all

Each server runs in its own process group, so a Windows Ctrl+C in this console reaches only
run_all, which then stops each server with CTRL_BREAK_EVENT (SIGTERM elsewhere) and waits for it:
the ports are released (§18.2). If a server exits on its own, the others are stopped and run_all
exits with status 1, so systemd (Restart=always) brings the whole set back.
"""

import importlib
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

# Block 1. payments (:8105), dispatch (:8106) and inventory (:8107) join in Blocks 3 and 4.
SERVERS = ("tickets", "catalog", "knowledge", "messaging")

BACKEND_DIR = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"
WINDOWS = sys.platform == "win32"
START_TIMEOUT_SECONDS = 30
STOP_TIMEOUT_SECONDS = 10


def port_of(name: str) -> int:
    return importlib.import_module(f"mcp_servers.{name}_server").PORT


def listening(port: int) -> bool:
    try:
        with socket.create_connection((HOST, port), timeout=0.5):
            return True
    except OSError:
        return False


def start(name: str) -> subprocess.Popen:
    group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS else {"start_new_session": True}
    return subprocess.Popen([sys.executable, "-m", f"mcp_servers.{name}_server"], cwd=BACKEND_DIR, **group)


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.CTRL_BREAK_EVENT if WINDOWS else signal.SIGTERM)
    try:
        proc.wait(timeout=STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _interrupt(signum, frame) -> None:
    raise KeyboardInterrupt


def main() -> int:
    # systemd stops with SIGTERM; Ctrl+Break on Windows is SIGBREAK. Both end like Ctrl+C.
    signal.signal(signal.SIGTERM, _interrupt)
    if WINDOWS:
        signal.signal(signal.SIGBREAK, _interrupt)

    ports = {name: port_of(name) for name in SERVERS}
    busy = [f"{name} :{port}" for name, port in ports.items() if listening(port)]
    if busy:
        print(f"Already in use: {', '.join(busy)}. Is another run_all or server running?", file=sys.stderr)
        return 1

    procs: dict[str, subprocess.Popen] = {}
    try:
        for name in SERVERS:
            procs[name] = start(name)
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        waiting = dict(ports)
        while waiting:
            for name, port in list(waiting.items()):
                if listening(port):
                    print(f"  {name:<10} http://{HOST}:{port}/mcp", flush=True)
                    del waiting[name]
                elif procs[name].poll() is not None or time.monotonic() > deadline:
                    print(f"{name} did not start (exit code {procs[name].poll()})", file=sys.stderr)
                    return 1
            time.sleep(0.2)
        print(f"All {len(SERVERS)} MCP servers are up. Ctrl+C stops them.", flush=True)

        while True:
            for name, proc in procs.items():
                if proc.poll() is not None:
                    print(f"{name} exited with code {proc.returncode}; stopping the others.", file=sys.stderr)
                    return 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("Stopping the MCP servers...", flush=True)
        return 0
    finally:
        for proc in procs.values():
            stop(proc)


if __name__ == "__main__":
    sys.exit(main())
