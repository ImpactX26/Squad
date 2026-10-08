"""Start every MCP server that exists, one process each (ARCHITECTURE.md §3, §5).

    cd backend && uv run python -m mcp_servers.run_all

Each server is its own process, so one crashing doesn't take the others down, and each gets
its own fastembed instance only if it needs one. Ctrl+C stops all of them cleanly.

On Windows a console Ctrl+C goes to the whole process group, which would race this script's
own shutdown. The children are started in a new process group instead and are sent an explicit
CTRL_BREAK_EVENT, so each one unwinds its own lifespan (pools closed, port released).
"""

import signal
import subprocess
import sys
import time
from pathlib import Path

from mcp_servers import SERVER_NAMES, host_port_path

BACKEND_DIR = Path(__file__).resolve().parents[1]
STOP_GRACE_SECONDS = 10
IS_WINDOWS = sys.platform == "win32"


def existing_servers() -> list[str]:
    """The §5 servers whose module has been built; the rest are still to come."""
    return [name for name in SERVER_NAMES if (BACKEND_DIR / "mcp_servers" / f"{name}_server.py").exists()]


def start(name: str) -> subprocess.Popen:
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
    return subprocess.Popen(
        [sys.executable, "-m", f"mcp_servers.{name}_server"],
        cwd=str(BACKEND_DIR),
        creationflags=creationflags,
        start_new_session=not IS_WINDOWS,
    )


def stop(name: str, process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.terminate()
    except (OSError, ValueError):  # already gone
        return
    try:
        process.wait(timeout=STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        print(f"  {name} did not stop in {STOP_GRACE_SECONDS}s; killing it", flush=True)
        process.kill()
        process.wait()


def main() -> int:
    names = existing_servers()
    if not names:
        print("No *_server.py files in mcp_servers/ yet.")
        return 1

    missing = [n for n in SERVER_NAMES if n not in names]
    processes: dict[str, subprocess.Popen] = {}
    for name in names:
        host, port, path = host_port_path(name)
        processes[name] = start(name)
        print(f"  {name:10} http://{host}:{port}{path}  pid {processes[name].pid}", flush=True)
    if missing:
        print(f"  not built yet: {', '.join(missing)}", flush=True)
    print(f"{len(processes)} MCP server(s) running. Ctrl+C to stop.", flush=True)

    # Ctrl+C in this console also reaches the children on POSIX; on Windows it does not,
    # because they are in their own process group. stop() handles both.
    try:
        while True:
            for name, process in list(processes.items()):
                if process.poll() is not None:
                    print(f"  {name} exited with code {process.returncode}", flush=True)
                    del processes[name]
            if not processes:
                print("Every server exited.", flush=True)
                return 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping...", flush=True)
    finally:
        for name, process in processes.items():
            stop(name, process)
        print("All MCP servers stopped.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())