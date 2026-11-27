"""A real streamable-http round trip against a server subprocess (loopback only, fake
provider executables), including enforcement of the request body limit."""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import time

import httpx
import pytest
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from .conftest import PACKET
from .fakes import memo

LIMIT = 6000


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def http_server(make_rig, tmp_path):
    rig = make_rig({"limits": {"max_request_bytes": LIMIT}})
    rig.codex.script(advisor=[{"json": memo("over http")}])
    (tmp_path / "http-home").mkdir()
    port = _free_port()
    process = subprocess.Popen(
        [
            "env", "-u", "CODEX_HOME", "-u", "CLAUDE_CONFIG_DIR", sys.executable, "-m",
            "coldtake", "--config", str(rig.config_path), "serve", "--transport", "http",
            "--port", str(port),
        ],  # fmt: skip
        env={"HOME": str(tmp_path / "http-home"), "PATH": "/usr/bin:/bin"},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("the HTTP server did not start")
        yield rig, f"http://127.0.0.1:{port}/mcp"
    finally:
        process.terminate()
        process.wait(10)


def test_a_streamable_http_round_trip_runs_a_job(http_server):
    rig, url = http_server

    async def flow():
        async with streamable_http_client(url) as (read, write, _):
            info = types.Implementation(name="claude-code", version="1")
            async with ClientSession(read, write, client_info=info) as session:
                await session.initialize()
                names = {t.name for t in (await session.list_tools()).tools}
                ack = await session.call_tool(
                    "coldtake_submit", {"request_id": "h1", "packet": PACKET}
                )
                for _ in range(100):
                    status = json.loads(
                        (await session.call_tool("coldtake_status", {"request_id": "h1"}))
                        .content[0].text
                    )  # fmt: skip
                    if status["status"] != "running":
                        break
                    await asyncio.sleep(0.1)
                result = await session.call_tool("coldtake_result", {"request_id": "h1"})
                return names, json.loads(ack.content[0].text), json.loads(result.content[0].text)

    names, ack, result = asyncio.run(flow())
    assert "coldtake_submit" in names and ack["request_id"] == "h1"
    assert result["status"] == "complete" and result["lead_family"] == "anthropic"
    assert result["result"]["memos"][0]["family"] == "openai" and len(rig.codex.calls()) == 1


def test_the_request_body_limit_is_enforced(http_server):
    _, url = http_server
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    small = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    big = {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {"pad": "x" * (LIMIT * 3)}}
    with httpx.Client() as client:
        refused = client.post(url, json=big, headers=headers)
        accepted = client.post(url, json=small, headers=headers)
    assert refused.status_code == 413
    assert accepted.status_code != 413


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "example.invalid", "127.0.0.2"])
def test_only_loopback_hosts_are_accepted(tmp_path, host):
    refused = subprocess.run(
        [sys.executable, "-m", "coldtake", "serve", "--transport", "http", "--host", host],
        capture_output=True, text=True, timeout=30,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )  # fmt: skip
    assert refused.returncode == 2 and "127.0.0.1, localhost or ::1" in refused.stderr


def test_loopback_names_are_recognised():
    from coldtake.cli import _is_loopback

    assert all(_is_loopback(h) for h in ("127.0.0.1", "localhost", "::1"))
