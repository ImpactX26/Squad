"""run_all starts every server over HTTP, then stops them all on Ctrl+Break / SIGTERM and frees the ports."""

import asyncio
import signal
import subprocess
import sys
import time

import pytest
from mcp import Client

from mcp_servers import run_all


@pytest.fixture
def ports():
    ports = {name: run_all.port_of(name) for name in run_all.SERVERS}
    busy = [port for port in ports.values() if run_all.listening(port)]
    if busy:
        pytest.skip(f"ports {busy} are already in use: stop the running MCP servers to run this test")
    return ports


async def test_run_all_starts_every_server_and_stops_them_all(ports):
    group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if run_all.WINDOWS else {"start_new_session": True}
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp_servers.run_all"], cwd=run_all.BACKEND_DIR,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **group,
    )
    try:
        deadline = time.monotonic() + 60
        while not all(run_all.listening(port) for port in ports.values()):
            assert proc.poll() is None, f"run_all exited early with {proc.returncode}"
            assert time.monotonic() < deadline, "the servers did not start within 60 s"
            await asyncio.sleep(0.2)
        for name, port in ports.items():
            async with Client(f"http://127.0.0.1:{port}/mcp") as client:
                assert (await client.list_tools()).tools, name
    finally:
        # What Ctrl+C in run_all's console (Windows) or `systemctl stop` (Linux) does.
        if proc.poll() is None:
            proc.send_signal(signal.CTRL_BREAK_EVENT if run_all.WINDOWS else signal.SIGTERM)
        try:
            output, _ = proc.communicate(timeout=40)
        except subprocess.TimeoutExpired:
            proc.kill()
            output, _ = proc.communicate()
            pytest.fail(f"run_all did not stop within 40 s:\n{output}")

    assert proc.returncode == 0, output
    assert "Stopping the MCP servers" in output
    assert not any(run_all.listening(port) for port in ports.values())
