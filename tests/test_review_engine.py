"""Stored plans, status and result decisions, deadlines, in-progress state, shared repair,
bounded CLI output, shutdown and startup recovery."""

from __future__ import annotations

import json
import os
import time

import pytest

from coldtake import providers as providers_module
from coldtake.contracts import CoreContract
from coldtake.routing import Plan

from . import helpers
from .conftest import PACKET
from .fakes import memo
from .helpers import S, Scripted, spy_rig
from .test_hooks import _v2
from .test_recovery import Clock, ghost

PANEL = {**PACKET, "mode": "panel"}


# -- M8: lookup before planning; the admitted plan is stored ------------------------------


def test_a_replayed_submission_is_not_planned_again(make_rig):
    rig, spy = spy_rig(make_rig, lambda ctx: Plan(chain=(S("m"),)))
    rig.engine.submit("req-1", PACKET, lead_family="anthropic")
    rig.engine.submit("req-1", PACKET, lead_family="anthropic")
    rig.engine.wait("req-1", 30)
    rig.engine.submit("req-1", PACKET, lead_family="anthropic")
    assert len(helpers.SEEN) == 1 and spy.counts == {"m": 1}


def test_the_admitted_plan_is_stored_and_exposed_to_shapers(make_rig):
    paid = helpers.P("pm", "fp")
    rig, spy = spy_rig(make_rig, lambda ctx: Plan(chain=(S("m", "fa"), paid)))
    rig.run(PACKET)
    stored = json.loads(rig.engine.store.get_run("req-1")["plan_json"])
    assert [r["model"] for r in stored["chain"]] == ["m", "pm"]
    assert stored["chain"][1]["billing"] == "paid" and stored["slots"] == []
    view = rig.engine.run_view("req-1")
    assert view.plan == stored and view.row["plan_json"]


def test_the_stored_plan_is_what_runs(make_rig):
    rig, spy = spy_rig(make_rig, lambda ctx: Plan(chain=(S("first"),)))
    rig.engine.submit("req-1", PACKET, lead_family="anthropic")
    helpers.PLAN = lambda ctx: Plan(chain=(S("second"),))
    out = rig.engine.wait("req-1", 30)
    assert out["result"]["memos"][0]["model"] == "first"
    assert "second" not in spy.counts


# -- M6: decisions come from the stored row ----------------------------------------------


class Weird(CoreContract):
    name = "weird_v1"

    def shape_status(self, run, view):
        return {**view, "status": "shaped-differently"}


def test_wait_and_result_decide_from_the_stored_status_and_shape_last(rig):
    rig.engine.register_contract(Weird(rig.config))
    rig.codex.script(advisor=[{"json": memo()}])
    rig.engine.submit("req-1", PACKET, lead_family="anthropic", contract="weird_v1")
    started = time.monotonic()
    out = rig.engine.wait("req-1", 30)
    assert time.monotonic() - started < 10  # not spinning on the shaped status
    assert out["status"] == "shaped-differently" and out["result"]["advisor_count"] == 1
    assert rig.engine.status("req-1")["status"] == "shaped-differently"


def test_a_failed_job_shows_partial_memos_even_when_the_shape_changes_status(rig):
    rig.engine.register_contract(Weird(rig.config))
    rig.codex.script(advisor=[{"error": "content policy refused", "code": 1}])
    rig.engine.submit("req-1", PACKET, lead_family="anthropic", contract="weird_v1")
    out = rig.engine.wait("req-1", 30)
    assert out["reason"] == "refusal" and out["partial_memos"] == []


# -- M5: the escalation deadline ----------------------------------------------------------


def test_escalation_extends_the_deadline_by_main_repair_and_judge_time(make_rig):
    plan = Plan(
        chain=(S("judge", "fj"),),
        slots=((S("a", "f1"),), (S("b", "f2"),)),
        escalation=(S("e", "f3"),),
        escalate=lambda memos: True,
    )

    def slow(prompt, route, n):
        time.sleep(0.3)
        return helpers.judge_reply(prompt) if "ROLE: judge" in prompt else memo("x")

    rig, spy = spy_rig(make_rig, lambda ctx: plan, Scripted(slow), limits={"repair_seconds": 10})
    out = rig.run(PANEL)
    assert out["status"] == "complete" and out["result"]["advisor_count"] == 3
    row = rig.engine.store.get_run("req-1")
    main, repair, judge = 40, 10, 20  # rig limits
    # reserved at ACK is main + repair + judge; the escalation starts a fresh main allowance
    # after the first two members, so the deadline moved past that by their elapsed time
    assert row["deadline_at"] - row["created_at"] > main + repair + judge + 0.25


def test_escalation_is_skipped_when_the_panel_is_already_full(make_rig):
    plan = Plan(
        chain=(S("judge", "fj"),),
        slots=tuple((S(f"m{i}", f"f{i}"),) for i in range(8)),
        escalation=(S("e", "fe"),),
        escalate=lambda memos: True,
    )
    rig, spy = spy_rig(make_rig, lambda ctx: plan)
    assert rig.run(PANEL)["result"]["advisor_count"] == 8
    assert "e" not in spy.counts


# -- in-progress state --------------------------------------------------------------------


def test_status_exposes_the_attempt_in_progress_to_shapers(make_rig):
    seen = {}

    class Watch(CoreContract):
        name = "watch_v1"

        def shape_status(self, run, view):
            seen["progress"] = json.loads(run["progress_json"])
            return view

    rig = make_rig()
    rig.engine.register_contract(Watch(rig.config))
    rig.codex.script(advisor=[{"sleep": 2, "then": {"json": memo()}}])
    rig.engine.submit("req-1", PACKET, lead_family="anthropic", contract="watch_v1")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        view = rig.engine.status("req-1")
        if view.get("progress"):
            break
        time.sleep(0.05)
    state = view["progress"]["advisor1"]
    assert state["phase"] == "advisor" and state["dispatched"] is True
    assert state["route_index"] == 0 and state["attempt_index"] == 0
    assert state["provider"] == "codex" and state["billing"] == "subscription"
    assert seen["progress"]["advisor1"]["phase"] == "advisor"
    assert rig.engine.run_view("req-1").progress["advisor1"]["dispatched"] is True
    assert rig.engine.wait("req-1", 30)["status"] == "complete"
    assert "progress" not in rig.engine.status("req-1")  # only while running


def test_progress_tracks_the_repair_attempt(make_rig):
    marks = []

    def behave(prompt, route, n):
        marks.append(rig.engine.status("req-1")["progress"]["advisor1"]["attempt_index"])
        return {"answer": 5} if n == 1 else {"answer": "fixed"}

    spy = Scripted(behave)
    rig, _ = spy_rig(make_rig, lambda ctx: Plan(chain=(S("m"),)), spy)
    _v2(rig)
    rig.engine.submit("req-1", {"decision_id": "d", "question": "q"}, lead_family="x",
                      contract="strict_v2")  # fmt: skip
    assert rig.engine.wait("req-1", 30)["status"] == "complete"
    assert marks == [0, 1]


# -- shared repair ------------------------------------------------------------------------


def _flaky(calls):
    def behave(prompt, route, n):
        calls.append(route.model)
        return {"answer": 5} if n == 1 else {"answer": "fixed"}

    return behave


def _panel_of_two(make_rig, shared):
    calls: list[str] = []
    plan = Plan(slots=((S("a", "f1"),), (S("b", "f2"),)))
    rig, _ = spy_rig(make_rig, lambda ctx: plan, Scripted(_flaky(calls)))
    _v2(rig, shared_repair=shared)
    packet = {"decision_id": "d", "question": "q", "mode": "panel"}
    rig.engine.submit("req-1", packet, lead_family="x", contract="strict_v2")
    return rig, rig.engine.wait("req-1", 30), calls


def test_each_route_gets_its_own_repair_by_default(make_rig):
    rig, out, calls = _panel_of_two(make_rig, shared=False)
    assert out["status"] == "complete" and len(calls) == 4


def test_a_shared_repair_is_spent_once_per_job(make_rig):
    rig, out, calls = _panel_of_two(make_rig, shared=True)
    assert out["status"] == "failed" and out["reason"] == "schema_exhausted"
    assert len(calls) == 3  # two first attempts and exactly one repair


# -- L1, L7: bounded CLI output, shutdown --------------------------------------------------


def test_cli_output_over_the_bound_stops_the_process(make_rig, monkeypatch):
    monkeypatch.setattr(providers_module, "MAX_OUTPUT_BYTES", 10_000)
    rig = make_rig()
    rig.codex.script(advisor=[{"flood": 200_000, "then_sleep": 30}])
    started = time.monotonic()
    out = rig.run(PACKET)
    assert time.monotonic() - started < 15  # killed, not waited out
    assert out["status"] == "failed" and out["reason"] == "output_too_large"


def test_close_kills_cli_process_groups_after_the_drain(make_rig):
    rig = make_rig()
    rig.codex.script(advisor=[{"sleep": 60, "then": {"json": memo()}}])
    rig.engine.submit("req-1", PACKET, lead_family="anthropic")
    deadline = time.monotonic() + 10
    while not rig.codex.calls() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert providers_module._LIVE_GROUPS
    started = time.monotonic()
    assert rig.engine.close(0) is False  # the job did not drain
    time.sleep(0.5)
    assert rig.engine.wait("req-1", 15)["status"] != "running"
    assert time.monotonic() - started < 20 and not providers_module._LIVE_GROUPS


def test_a_cli_that_forks_a_straggler_leaves_nothing_behind(make_rig, tmp_path):
    pid_file = tmp_path / "straggler.pid"
    rig = make_rig()
    rig.codex.script(advisor=[{"json": memo()}])
    spawn = (
        "import subprocess\n"
        "child = subprocess.Popen(['sleep', '30'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
    )
    script = rig.codex.path.read_text().replace(
        'if "sleep" in spec:', spawn + 'if "sleep" in spec:', 1
    )
    rig.codex.path.write_text(script)
    started = time.monotonic()
    assert rig.run(PACKET)["status"] == "complete"
    assert time.monotonic() - started < 15
    pid = int(pid_file.read_text())
    for _ in range(50):  # the process group is killed when the attempt ends
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        else:
            with open(f"/proc/{pid}/stat") as stat:  # a zombie awaiting reaping is dead
                if stat.read().split(")")[1].split()[0] == "Z":
                    break
        time.sleep(0.1)
    else:
        pytest.fail("the straggler process outlived the attempt")


# -- L6: startup recovery only for older instances -----------------------------------------


def test_startup_recovery_leaves_jobs_of_a_newer_instance_alone(rig):
    rig.engine.store.clock = Clock(1_000_000.0)
    ghost(rig.engine, "old", lease=10_000, instance="old-instance")
    rig.engine.store.clock.now += 50
    ghost(rig.engine, "new", lease=10_000, instance="new-instance")
    with rig.engine.store.connect() as db:
        db.execute("UPDATE runs SET owner_pid=?", (os.getpid(),))
    recovered = rig.engine.store.reconcile(startup_instance="me", startup_time=1_000_000.0 + 10)
    assert recovered == ["old"]
    assert rig.engine.store.get_run("old")["status"] == "lost"
    assert rig.engine.store.get_run("new")["status"] == "running"
