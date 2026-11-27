"""Real stdio MCP round trips against fake provider executables."""

from __future__ import annotations

import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

from .conftest import PACKET
from .fakes import memo


def _session_params(rig):
    # `env -u` so the real CLI config dirs can never leak into the server subprocess.
    return StdioServerParameters(
        command="env",
        args=[
            "-u",
            "CODEX_HOME",
            "-u",
            "CLAUDE_CONFIG_DIR",
            sys.executable,
            "-m",
            "coldtake",
            "--config",
            str(rig.config_path),
            "serve",
        ],  # fmt: skip
        env={"HOME": str(rig.tmp / "e2e-home")},
    )


async def _call(rig, client_name, tool, args):
    info = types.Implementation(name=client_name, version="1.0")
    async with stdio_client(_session_params(rig)) as (read, write):
        async with ClientSession(read, write, client_info=info) as session:
            await session.initialize()
            tools = await session.list_tools()
            result = await session.call_tool(tool, args)
            text = result.content[0].text
            return {t.name for t in tools.tools}, json.loads(text)


def test_lead_family_is_detected_from_client_info(make_rig):
    rig = make_rig(same_family=True)
    (rig.tmp / "e2e-home").mkdir()
    rig.codex.script(advisor=[{"json": memo("from codex")}])
    rig.claude.script(advisor=[{"json": memo("from claude")}])

    names, out = asyncio.run(
        _call(rig, "claude-code", "coldtake_alternate", {"request_id": "e1", "packet": PACKET})
    )
    assert {"coldtake_submit", "coldtake_alternate", "coldtake_panel"} <= names
    assert out["status"] == "complete" and out["lead_family"] == "anthropic"
    assert out["result"]["memos"][0]["family"] == "openai"
    assert len(rig.codex.calls()) == 1 and not rig.claude.calls()

    _, out = asyncio.run(
        _call(rig, "codex-mcp-client", "coldtake_alternate", {"request_id": "e2", "packet": PACKET})
    )
    assert out["lead_family"] == "openai"
    assert out["result"]["memos"][0]["family"] == "anthropic"
    assert len(rig.claude.calls()) == 1


def test_submit_then_poll_with_the_same_id(rig):
    (rig.tmp / "e2e-home").mkdir()
    rig.codex.script(advisor=[{"json": memo()}])

    async def flow():
        info = types.Implementation(name="claude-code", version="1")
        async with stdio_client(_session_params(rig)) as (read, write):
            async with ClientSession(read, write, client_info=info) as session:
                await session.initialize()
                ack = await session.call_tool(
                    "coldtake_submit", {"request_id": "e3", "packet": PACKET}
                )
                ack = json.loads(ack.content[0].text)
                for _ in range(100):
                    status = json.loads(
                        (await session.call_tool("coldtake_status", {"request_id": "e3"}))
                        .content[0].text
                    )  # fmt: skip
                    if status["status"] != "running":
                        break
                    await asyncio.sleep(0.1)
                result = await session.call_tool("coldtake_result", {"request_id": "e3"})
                return ack, status, json.loads(result.content[0].text)

    ack, status, result = asyncio.run(flow())
    assert ack["request_id"] == "e3" and ack["status"] in {"running", "complete"}
    assert status["status"] == "complete" and result["result"]["advisor_count"] == 1


def test_version_and_http_loopback_guard(tmp_path):
    import subprocess

    base = [sys.executable, "-m", "coldtake"]
    assert subprocess.run([*base, "--version"], capture_output=True, text=True).stdout.startswith(
        "coldtake 0.1.0"
    )
    refused = subprocess.run(
        [*base, "serve", "--transport", "http", "--host", "example.invalid"],
        capture_output=True, text=True, env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )  # fmt: skip
    assert refused.returncode == 2 and "127.0.0.1" in refused.stderr
