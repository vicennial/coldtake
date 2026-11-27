"""Tests for upgraded stats, history, show, init, install, ask, check CLI, and evals --smoke."""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

from coldtake.check import CONTRACT_CHECK
from coldtake.config import SubscriptionRoute, load_config
from coldtake.engine import Engine
from coldtake.onboarding import install_mcp, mcp_snippet, write_init_config
from coldtake.providers import Completion
from coldtake.stats import (
    collect,
    get_run_detail,
    list_runs,
    render,
    render_history,
    render_run_detail,
    render_share,
)

from .conftest import PACKET
from .fakes import memo


def _check_json(verdict: str = "concerns") -> dict:
    return {
        "verdict": verdict,
        "key_risks": ["Unbounded queue growth."],
        "recommendation": "Bound the queue and apply backpressure.",
        "alternatives": ["Use a ring buffer."],
    }


def test_stats_pair_matrix_checks_billing_share_history_and_show(tmp_path):
    class Prov:
        def __init__(self, name: str):
            self.name = name

        def available(self) -> bool:
            return True

        def complete(self, prompt, schema, route, timeout):
            del route, timeout
            if "verdict" in schema.get("properties", {}):
                return Completion(data=_check_json("concerns"))
            if "choice" in schema.get("properties", {}):
                return Completion(data={**memo("pick b"), "choice": "b", "choice_reason": "better"})
            return Completion(data=memo("advisor view"))

    base = load_config()
    cfg = base.model_copy(
        update={
            "state_dir": str(tmp_path / "state"),
            "namespace": "demo",
            "subscription": {
                "openai": SubscriptionRoute(provider="p_o", model="gpt-test"),
                "google": SubscriptionRoute(provider="p_g", model="gem-test"),
                "anthropic": SubscriptionRoute(provider="p_a", model="cla-test"),
            },
            "openrouter": base.openrouter.model_copy(update={"models": ()}),
        }
    )
    engine = Engine(cfg, providers={"p_o": Prov("p_o"), "p_g": Prov("p_g"), "p_a": Prov("p_a")})
    try:
        # 1) Deterministic sealed options alternate -> anthropic->openai conflict
        engine.submit(
            "run-alt",
            {
                **PACKET,
                "mode": "alternate",
                "options": [{"id": "a", "label": "Opt A"}, {"id": "b", "label": "Opt B"}],
                "sealed_choice": "a",
            },
            lead_family="anthropic",
        )
        assert engine.wait("run-alt", 10)["status"] == "complete"

        # 2) Spec check -> verdict=concerns
        engine.submit(
            "run-chk",
            {"question": "Check queue design"},
            lead_family="anthropic",
            contract=CONTRACT_CHECK,
        )
        assert engine.wait("run-chk", 10)["status"] == "complete"

        data = collect(engine.store)
        assert data["billing"]["total_runs"] == 2
        assert data["billing"]["subscription_runs"] == 2
        assert data["billing"]["subscription_share"] == 1.0
        assert data["checks"]["total"] == 1
        assert data["checks"]["verdicts"]["concerns"] == 1
        assert data["sealed"]["by_pair"]["anthropic->openai"]["conflict"] == 1

        text = render(data)
        assert "Billing mix" in text and "Spec checks" in text and "anthropic->openai" in text

        card = render_share(data)
        assert "coldtake scorecard" in card
        assert "anthropic->openai" in card
        assert "Spec checks: 1" in card
        assert PACKET["question"] not in card  # anonymized: never includes prompt text

        items = list_runs(engine.store, limit=10)
        assert len(items) == 2
        assert "verdict=concerns" in render_history(items)
        assert "sealed=conflict" in render_history(items)

        # show works with un-namespaced suffix too
        detail = get_run_detail(engine.store, "run-chk")
        assert detail is not None and detail["mode"] == "check"
        rendered = render_run_detail(detail)
        assert "Check verdict: concerns" in rendered
    finally:
        engine.close()


def test_init_and_install_for_all_clients_without_touching_real_home(tmp_path):
    # init writes starter config and refuses overwrite unless force=True
    cfg_out = write_init_config(
        {"claude": {"executable": "/usr/bin/claude"}, "codex": {"executable": None}},
        home=tmp_path,
    )
    assert cfg_out == tmp_path / ".config" / "coldtake" / "config.yaml"
    assert "/usr/bin/claude" in cfg_out.read_text(encoding="utf-8")

    # install for claude, codex, gemini, cursor into isolated fake home
    for client in ("claude", "codex", "gemini", "cursor"):
        snip = mcp_snippet(client)
        assert "coldtake" in snip
        res = install_mcp(client, with_skill=True, home=tmp_path)
        cfg_path = Path(res["config_path"])
        assert cfg_path.is_file()
        assert res["skill_path"] and Path(res["skill_path"]).is_file()
        if client == "codex":
            parsed = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
            assert parsed["mcp_servers"]["coldtake"]["command"] == "uvx"
        else:
            parsed = json.loads(cfg_path.read_text(encoding="utf-8"))
            assert parsed["mcpServers"]["coldtake"]["command"] == "uvx"

    # Re-running codex install is idempotent (replaces block cleanly, valid TOML)
    res2 = install_mcp("codex", command="coldtake serve", home=tmp_path)
    parsed2 = tomllib.loads(Path(res2["config_path"]).read_text(encoding="utf-8"))
    assert parsed2["mcp_servers"]["coldtake"] == {"command": "coldtake", "args": ["serve"]}


def test_cli_ask_check_history_show_stats_share_and_install_print(make_rig, tmp_path):
    rig = make_rig()
    rig.codex.script(advisor=[{"json": memo("cli recommendation")}])
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    base = [sys.executable, "-m", "coldtake", "--config", str(rig.config_path)]

    # 1) coldtake ask
    ask = subprocess.run(  # noqa: S603
        [*base, "ask", "Choose an index structure", "--lead", "anthropic", "--request-id", "c1"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert "cli recommendation" in ask.stdout

    # 2) coldtake check --file (explicit local file allowed automatically for CLI check)
    spec = tmp_path / "design.md"
    spec.write_text("# Queue\nSingle mutex.\n", encoding="utf-8")
    rig.codex.script(advisor=[{"json": _check_json("sound")}])
    chk = subprocess.run(  # noqa: S603
        [
            *base,
            "check",
            "Is this lock safe?",
            "--file",
            str(spec),
            "--lead",
            "anthropic",
            "--request-id",
            "c2",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert "Check verdict: sound" in chk.stdout

    # 3) coldtake history, show, stats --share
    hist = subprocess.run(  # noqa: S603
        [*base, "history"], capture_output=True, text=True, env=env, check=True
    )
    assert "c1" in hist.stdout and "c2" in hist.stdout

    shown = subprocess.run(  # noqa: S603
        [*base, "show", "c2"], capture_output=True, text=True, env=env, check=True
    )
    assert "Check verdict: sound" in shown.stdout

    share = subprocess.run(  # noqa: S603
        [*base, "stats", "--share"], capture_output=True, text=True, env=env, check=True
    )
    assert "coldtake scorecard" in share.stdout and "Spec checks: 1" in share.stdout

    # 4) coldtake install --print
    printed = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "coldtake", "install", "codex", "--print"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert "[mcp_servers.coldtake]" in printed.stdout

    # 5) coldtake report --out scorecard.html
    html_out = tmp_path / "scorecard.html"
    subprocess.run(  # noqa: S603
        [*base, "report", "--out", str(html_out)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    html_text = html_out.read_text(encoding="utf-8")
    assert "coldtake scorecard" in html_text and "c2" in html_text


def test_mcp_stats_and_history_resources(make_rig):
    import asyncio

    from coldtake.server import build_server

    rig = make_rig()
    rig.codex.script(advisor=[{"json": memo("res check")}])
    assert rig.run(PACKET, request_id="res-1")["status"] == "complete"
    server = build_server(rig.config, rig.engine)
    resources = {str(r.uri) for r in asyncio.run(server.list_resources())}
    assert {"coldtake://stats", "coldtake://history"} <= resources

    stats_raw = asyncio.run(server.read_resource("coldtake://stats"))
    stats_text = stats_raw[0].content if isinstance(stats_raw, list) else str(stats_raw)
    assert json.loads(stats_text)["billing"]["total_runs"] == 1

    hist_raw = asyncio.run(server.read_resource("coldtake://history"))
    hist_text = hist_raw[0].content if isinstance(hist_raw, list) else str(hist_raw)
    assert json.loads(hist_text)[0]["request_id"] == "res-1"


def test_evals_smoke_slice_selects_six_stratified_tasks():
    root = Path(__file__).resolve().parents[1]
    res = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "evals.run", "--smoke", "--dry-run"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "tasks: 6" in res.stdout
