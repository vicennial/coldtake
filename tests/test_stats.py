"""`coldtake stats` from local run state."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import httpx

from coldtake.stats import collect, render

from .conftest import OPTIONS, PACKET
from .fakes import FakeRouter, choice_memo, memo

PANEL = {**PACKET, "mode": "panel"}


def _populate(make_rig):
    router = FakeRouter(choice_memo("lite"), httpx.ReadTimeout("slow"))
    rig = make_rig({"panel": {"max_members": 3, "max_paid_members": 1}}, router)
    rig.codex.script(
        advisor=[{"json": choice_memo("pg")}], judge=[{"judge_auto": {"sealed": "conflict"}}]
    )
    # panel + options + sealed choice: pg agrees, lite conflicts
    rig.run({**PANEL, "options": OPTIONS, "sealed_choice": "pg"}, "p1")
    # alternate via the judge path with a sealed answer
    rig.codex.script(advisor=[{"json": memo()}], judge=[{"judge_auto": {"sealed": "partial"}}])
    rig.run({**PACKET, "sealed_answer": "mine"}, "a1")
    # a subscription failure and a paid ambiguity
    rig.codex.script(advisor=[{"error": "usage limit", "code": 1}])
    rig.run(PACKET, "c1")
    return rig


def test_collect_counts_runs_routes_spend_and_sealed_rates(make_rig):
    rig = _populate(make_rig)
    data = collect(rig.engine.store)
    assert data["runs"]["panel"] == {"complete": 1}
    assert data["runs"]["alternate"] == {"complete": 1, "charge_unknown": 1}
    codex = data["routes"]["codex:default"]
    assert codex["billing"] == "subscription" and codex["outcomes"]["quota"] == 1
    paid = data["routes"]["openrouter:google/gemini-3.7-flash"]
    assert paid["billing"] == "paid" and paid["outcomes"]["charge_unknown"] == 1
    assert data["fallback_attempts"] >= 1
    assert data["paid"]["charge_unknown_held"]["attempts"] == 1
    assert sum(data["paid"]["committed_usd_by_day"].values()) > 0
    sealed = data["sealed"]
    assert sealed["by_mode"]["panel"]["agree"] == 1 and sealed["by_mode"]["panel"]["conflict"] == 1
    assert sealed["by_mode"]["alternate"]["partial"] == 1
    assert sealed["by_family"]["openai"]["n"] == 2
    assert sealed["by_family"]["google"]["conflict"] == 1
    assert sealed["by_mode"]["panel"]["agree_rate"] == 0.5
    assert data["latency_seconds"]["panel"]["p50"] is not None


def test_render_is_readable_text(make_rig):
    text = render(collect(_populate(make_rig).engine.store))
    for fragment in ("Runs by mode", "charge_unknown", "agree / partial / conflict", "p95="):
        assert fragment in text


def test_window_excludes_old_runs(make_rig):
    rig = _populate(make_rig)
    rig.engine.store.clock = lambda: rig.engine.store.get_run("p1")["created_at"] + 10 * 86400
    assert collect(rig.engine.store, days=1)["runs"] == {}
    assert collect(rig.engine.store)["runs"]


def test_stats_cli_in_a_fake_home(make_rig, tmp_path):
    rig = _populate(make_rig)
    home = tmp_path / "fake-home"
    home.mkdir()
    # `env -u` keeps the developer's real CLI config dirs out of the subprocess.
    command = [
        "env", "-u", "CODEX_HOME", "-u", "CLAUDE_CONFIG_DIR",
        sys.executable, "-m", "coldtake", "--config", str(rig.config_path), "stats", "--json",
    ]  # fmt: skip
    env = {"HOME": str(home), "PATH": os.environ["PATH"]}
    done = subprocess.run(command, env=env, capture_output=True, text=True, check=True)
    data = json.loads(done.stdout)
    assert data["sealed"]["by_mode"]["panel"]["n"] == 2
    text = subprocess.run(
        [*command[:-1]], env=env, capture_output=True, text=True, check=True
    ).stdout
    assert "coldtake stats" in text
