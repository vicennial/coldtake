"""SIGTERM is a graceful stop on both transports: running jobs are drained, not cut off."""

from __future__ import annotations

import json
import signal
import socket
import subprocess
import sys
import time

import pytest

from coldtake.store import Store

from .conftest import PACKET
from .fakes import memo

# -- H2: SIGTERM drains running jobs -----------------------------------------------------


def _server(rig, tmp_path, *args):
    """A server subprocess (a context manager: its pipes are closed on exit)."""
    home = tmp_path / "sigterm-home"
    home.mkdir(exist_ok=True)
    return subprocess.Popen(
        [sys.executable, "-m", "coldtake", "--config", str(rig.config_path), "serve", *args],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )  # fmt: skip


def _submit_call(request_id):
    arguments = {"request_id": request_id, "packet": PACKET}
    return {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "coldtake_submit", "arguments": arguments},
    }  # fmt: skip


def _reap(process):
    if process.poll() is None:
        process.kill()
        process.wait()
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe is not None:
            pipe.close()


def _slow_rig(make_rig):
    rig = make_rig({"limits": {"drain_seconds": 20}})
    rig.codex.script(advisor=[{"sleep": 2, "then": {"json": memo("slow but finished")}}])
    return rig


def _wait_status(rig, request_id, want, seconds=20):
    store = Store(rig.config.resolved_state_dir())
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        row = store.get_run(request_id)
        if row is not None and row["status"] in want:
            return row
        time.sleep(0.1)
    return store.get_run(request_id)


def test_sigterm_on_the_http_transport_drains_the_running_job(make_rig, tmp_path):
    rig = _slow_rig(make_rig)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = _server(rig, tmp_path, "--transport", "http", "--port", str(port))
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("the HTTP server did not start")
        import asyncio

        from mcp import ClientSession, types
        from mcp.client.streamable_http import streamable_http_client

        async def submit():
            async with streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (r, w, _):
                info = types.Implementation(name="claude-code", version="1")
                async with ClientSession(r, w, client_info=info) as session:
                    await session.initialize()
                    await session.call_tool(
                        "coldtake_submit", {"request_id": "t1", "packet": PACKET}
                    )

        asyncio.run(submit())
        assert _wait_status(rig, "t1", {"running"})["status"] == "running"
        process.send_signal(signal.SIGTERM)
        assert process.wait(30) == 0  # a graceful stop, not death by the signal
    finally:
        _reap(process)
    row = Store(rig.config.resolved_state_dir()).get_run("t1")
    assert row["status"] == "complete"  # the drain ran, so the job was not cut off


def test_sigterm_on_the_stdio_transport_drains_the_running_job(make_rig, tmp_path):
    rig = _slow_rig(make_rig)
    process = _server(rig, tmp_path)
    try:

        def send(message):
            process.stdin.write((json.dumps(message) + "\n").encode())
            process.stdin.flush()

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "claude-code", "version": "1"}}})  # fmt: skip
        assert json.loads(process.stdout.readline())["id"] == 1
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send(_submit_call("t2"))
        assert json.loads(process.stdout.readline())["id"] == 2
        assert _wait_status(rig, "t2", {"running"})["status"] == "running"
        process.send_signal(signal.SIGTERM)
        assert process.wait(30) == 0
    finally:
        _reap(process)
    assert Store(rig.config.resolved_state_dir()).get_run("t2")["status"] == "complete"


def test_a_second_sigterm_during_the_drain_ends_the_process(make_rig, tmp_path):
    rig = make_rig({"limits": {"drain_seconds": 60}})
    rig.codex.script(advisor=[{"sleep": 30, "then": {"json": memo("never")}}])
    process = _server(rig, tmp_path)
    try:

        def send(message):
            process.stdin.write((json.dumps(message) + "\n").encode())
            process.stdin.flush()

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "claude-code", "version": "1"}}})  # fmt: skip
        process.stdout.readline()
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send(_submit_call("t3"))
        process.stdout.readline()
        _wait_status(rig, "t3", {"running"})
        process.send_signal(signal.SIGTERM)
        time.sleep(1.5)
        assert process.poll() is None  # draining
        process.send_signal(signal.SIGTERM)
        assert process.wait(15) != 0  # the default action: no more waiting
    finally:
        _reap(process)


def _handshake(process, request_id):
    def send(message):
        process.stdin.write((json.dumps(message) + "\n").encode())
        process.stdin.flush()

    send({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "claude-code", "version": "1"},
        },
    })  # fmt: skip
    process.stdout.readline()
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    send(_submit_call(request_id))
    process.stdout.readline()


def test_sigterm_during_the_drain_after_stdin_closes_does_not_skip_the_drain(make_rig, tmp_path):
    rig = _slow_rig(make_rig)
    process = _server(rig, tmp_path)
    try:
        _handshake(process, "t4")
        assert _wait_status(rig, "t4", {"running"})["status"] == "running"
        process.stdin.close()  # end of input: the server stops serving and starts draining
        time.sleep(0.5)
        process.send_signal(signal.SIGTERM)  # the first one is only a request; keep draining
        time.sleep(0.5)
        assert process.poll() is None
        assert process.wait(30) == 0
    finally:
        _reap(process)
    assert Store(rig.config.resolved_state_dir()).get_run("t4")["status"] == "complete"


def test_a_drain_that_times_out_exits_non_zero(make_rig, tmp_path):
    rig = make_rig({"limits": {"drain_seconds": 1}})
    rig.codex.script(advisor=[{"sleep": 30, "then": {"json": memo("never")}}])
    process = _server(rig, tmp_path)
    try:
        _handshake(process, "t5")
        _wait_status(rig, "t5", {"running"})
        process.send_signal(signal.SIGTERM)
        assert process.wait(30) == 1  # cut off: not a clean stop
    finally:
        _reap(process)
