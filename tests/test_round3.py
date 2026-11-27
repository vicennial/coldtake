"""Findings from the real-CLI smoke and the re-review: optional S stance, repair text,
login markers, option-id case, price checks in the runner, progress end state, one engine
per state directory, panel members, process-group safety, schema migration."""

from __future__ import annotations

import gc
import os
import sqlite3
import subprocess
import time

import pytest

from coldtake import store as store_module
from coldtake.compare import judge_comparison
from coldtake.contracts import CoreContract
from coldtake.engine import Engine, InvalidRequest
from coldtake.failures import ProviderFailure, classify
from coldtake.prompts import render_judge_prompt, repair_text
from coldtake.providers import _Group
from coldtake.routing import Plan
from coldtake.schemas import JudgeOutput, Memo, Submission
from coldtake.store import SCHEMA_VERSION, Store

from .conftest import PACKET
from .fakes import choice_memo, memo
from .helpers import P, S, Scripted, spy_rig

PANEL = {**PACKET, "mode": "panel"}
SEALED = {**PACKET, "sealed_answer": "Choose Postgres"}


# -- R1: the judge's stance for S is optional --------------------------------------------


def _panel_with_sealed(make_rig, **judge):
    from .fakes import FakeRouter

    rig = make_rig({"panel": {"max_members": 3, "max_paid_members": 1}}, FakeRouter(memo("B")))
    rig.codex.script(advisor=[{"json": memo("A")}], judge=[{"judge_auto": judge}])
    return rig


def test_a_judge_that_includes_a_stance_for_s_is_accepted(make_rig):
    rig = _panel_with_sealed(make_rig, sealed="partial")
    comparison = rig.run({**SEALED, "mode": "panel"})["result"]["comparison"]
    assert comparison["status"] == "ok"
    assert {p["memo"] for p in comparison["positions"]} == {"m1", "m2", "sealed"}
    assert comparison["sealed"]["relation"] == "partial"


def test_a_judge_that_leaves_out_the_s_stance_is_accepted_too(make_rig):
    rig = _panel_with_sealed(make_rig, no_s_position=True)
    comparison = rig.run({**SEALED, "mode": "panel"})["result"]["comparison"]
    assert comparison["status"] == "ok"
    assert {p["memo"] for p in comparison["positions"]} == {"m1", "m2"}


def test_s_may_be_listed_at_most_once_and_only_when_a_sealed_answer_exists(make_rig):
    rig = _panel_with_sealed(make_rig, dup_s_position=True)
    assert rig.run({**SEALED, "mode": "panel"})["result"]["comparison"]["status"] == "unavailable"
    memos = [{"id": "m1", "memo": memo()}, {"id": "m2", "memo": memo()}]
    out = JudgeOutput.model_validate(
        {
            "positions": [
                {"label": "A", "stance": "x"},
                {"label": "B", "stance": "y"},
                {"label": "S", "stance": "z"},
            ],
            "clusters": [["A", "B"]],
            "pairwise": [{"a": "A", "b": "B", "relation": "agree", "crux": "c"}],
            "sealed": None, "contradictions": [], "material": False,
        }
    )  # fmt: skip
    with pytest.raises(ValueError):
        judge_comparison(out, [0, 1], memos, has_sealed=False)


def test_the_sealed_wording_matches_the_local_overall_relation():
    prompt = render_judge_prompt("q?", [Memo.model_validate(memo())], [0], "sealed text")
    assert "agree if S agrees with every memo" in prompt
    assert "conflict if S conflicts with every memo" in prompt
    assert "partial in every other case" in prompt


def test_the_default_repair_prompt_names_the_validation_error(rig):
    text = CoreContract(rig.config).repair_prompt("PROMPT", None, "positions: need one per label")
    assert text.startswith("PROMPT") and "positions: need one per label" in text
    assert "VALIDATION ERROR" not in repair_text("")
    assert len(repair_text("x" * 5000)) < 1200
    rig.codex.script(advisor=[{"garbage": True}, {"json": memo()}])
    rig.run(PACKET)
    assert "VALIDATION ERROR TO FIX" in rig.codex.calls("advisor")[1]["stdin"]


def test_a_judge_repair_carries_what_the_judge_got_wrong(make_rig):
    rig = _panel_with_sealed(make_rig, drop_position=True)
    rig.run({**SEALED, "mode": "panel"})
    first, second = (c["stdin"] for c in rig.codex.calls("judge"))
    assert "VALIDATION ERROR" not in first
    assert "positions must contain exactly one entry per memo label" in second


# -- smoke (a): login markers --------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Not logged in · Please run /login",
        "Please run /login to continue",
        "error: you are not logged in",
        "Please log in again",
    ],
)
def test_login_prompts_are_expired_auth_and_safe_to_fall_back(text):
    failure = classify(text)
    assert (failure.reason, failure.kind) == ("auth_expired", "fallback")


def test_a_claude_not_logged_in_envelope_falls_back_to_the_next_family(make_rig):
    rig = make_rig(same_family=True)
    rig.claude.script(advisor=[{"claude_error": "Not logged in · Please run /login"}])
    rig.codex.script(advisor=[{"json": memo("from codex")}])
    out = rig.run(PACKET, lead="other")
    assert out["result"]["memos"][0]["family"] == "openai"
    assert rig.attempts()[0]["reason"] == "auth_expired"


# -- smoke (b): option ids in any case ------------------------------------------------------

LETTERS = [{"id": "A", "label": "Postgres"}, {"id": "B", "label": "SQLite"}]


def test_option_ids_and_the_sealed_choice_are_normalized_to_lower_case():
    sub = Submission.model_validate({**PACKET, "options": LETTERS, "sealed_choice": "A"})
    assert sub.option_ids() == ["a", "b"] and sub.sealed_choice == "a"


@pytest.mark.parametrize("ids", [["A", "a"], ["NONE", "B"], ["None", "B"]])
def test_case_variants_collide_and_none_stays_reserved(ids):
    options = [{"id": i, "label": i} for i in ids]
    with pytest.raises(ValueError):
        Submission.model_validate({**PACKET, "options": options})


def test_an_options_job_with_upper_case_ids_runs_end_to_end(rig):
    rig.codex.script(advisor=[{"json": choice_memo("A")}])  # the model answers in upper case
    out = rig.run({**PACKET, "options": LETTERS, "sealed_choice": "B"})["result"]
    assert out["memos"][0]["memo"]["choice"] == "a"
    assert out["comparison"]["sealed"]["relation"] == "conflict"
    schema = rig.codex.calls("advisor")[0]["schema"]
    assert schema["properties"]["choice"]["enum"] == ["a", "b", "none"]
    assert '"id": "a"' in rig.codex.calls("advisor")[0]["stdin"]


# -- R2: the price check runs inside the attempt gate ---------------------------------------


def test_run_chain_refuses_a_zero_price_route_before_calling_the_provider(make_rig):
    rig, spy = spy_rig(make_rig, lambda ctx: Plan(chain=(S("sub"),)))
    route = P("m", completion_usd_per_mtok=0.0)
    schema = {"type": "object", "properties": {}}
    with pytest.raises(ProviderFailure) as raised:
        rig.engine.run_chain("ext-1", (route,), "hello", schema, lambda d: d)
    assert raised.value.reason == "budget_exceeded" and "unusable" in raised.value.message
    assert spy.prompts == []
    with rig.engine.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0
    # a usable paid route through the same runner still works
    spy.cost = 0.01
    value, _ = rig.engine.run_chain("ext-2", (P("ok"),), "hello", schema, lambda d: d)
    assert value["recommendation"]


def test_the_judge_runner_cannot_use_an_unusable_route(make_rig):
    rig, spy = spy_rig(make_rig, lambda ctx: Plan(chain=(S("sub"),)))
    job = rig.engine  # the gate is the engine's own check
    bad = P("m", max_output_tokens=-1)
    assert "unusable" in job._price_problem(bad, 100)
    assert job._price_problem(P("fine"), 100) is None and job._price_problem(S("s"), 100) is None


# -- R3: progress ends with the attempt ----------------------------------------------------


def test_progress_is_finalized_when_an_attempt_ends(rig):
    rig.codex.script(advisor=[{"json": memo()}], judge=[{"sleep": 2, "then": {"judge_auto": {}}}])
    rig.engine.submit("req-1", SEALED, lead_family="anthropic")
    deadline = time.monotonic() + 15
    progress = {}
    while time.monotonic() < deadline:
        progress = rig.engine.status("req-1").get("progress", {})
        if "judge0" in progress:
            break
        time.sleep(0.05)
    advisor, judge = progress["advisor1"], progress["judge0"]
    assert advisor["dispatched"] is False and advisor["outcome"] == "returned"
    assert advisor["finished_at"] >= advisor["started_at"]
    assert judge["dispatched"] is True and "finished_at" not in judge
    assert rig.engine.wait("req-1", 30)["status"] == "complete"


def test_progress_records_a_failed_attempt(make_rig):
    def behave(prompt, route, n):
        raise ProviderFailure("quota", "fallback", "limit")

    rig, spy = spy_rig(
        make_rig, lambda ctx: Plan(chain=(S("a", "f1"), S("b", "f2"))), Scripted(behave)
    )
    seen = []
    orig = rig.engine.store.set_progress

    def spy_progress(request_id, key, state):
        seen.append(dict(state) if state else state)
        return orig(request_id, key, state)

    rig.engine.store.set_progress = spy_progress
    rig.run(PACKET)
    ends = [s for s in seen if s and not s["dispatched"]]
    assert [e["outcome"] for e in ends] == ["quota", "quota"]


# -- L1: one engine per state directory -----------------------------------------------------


def test_a_second_engine_for_the_same_state_directory_is_refused(rig):
    with pytest.raises(RuntimeError, match="already exists"):
        Engine(rig.config, rig.providers)
    rig.engine.close(0)
    again = Engine(rig.config, rig.providers)  # released by close
    with pytest.raises(RuntimeError):
        Engine(rig.config, rig.providers)
    del again
    gc.collect()
    Engine(rig.config, rig.providers)  # an engine that was dropped releases it too


# -- L2: a panel needs two planned members ---------------------------------------------------


def test_the_replacement_pool_does_not_make_a_panel(make_rig):
    plan = Plan(slots=((S("a", "f1"),),), reserve=(S("r", "f2"),))
    rig, spy = spy_rig(make_rig, lambda ctx: plan)
    with pytest.raises(InvalidRequest, match="at least two members"):
        rig.engine.submit("req-1", PANEL, lead_family="anthropic")
    assert rig.engine.store.get_run("req-1") is None and spy.prompts == []


# -- L3: never signal a reaped group ---------------------------------------------------------


def test_a_reaped_process_group_is_never_signalled(monkeypatch):
    calls = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: calls.append((pid, sig)))
    process = subprocess.Popen(["true"], start_new_session=True)
    group = _Group(process)
    for _ in range(100):
        if group.exited():
            break
        time.sleep(0.02)
    assert group.exited() and process.returncode is None  # exited but not reaped
    group.kill()
    assert len(calls) == 1  # the pid is still ours, so the group may be signalled
    group.reap()
    assert group.reaped and process.returncode == 0
    group.kill()
    group.kill()
    assert len(calls) == 1  # nothing after the reap


# -- L5: schema migration --------------------------------------------------------------------


@pytest.mark.parametrize(
    "missing", [("plan_json", "progress_json", "failed_route_billing"), ("failed_route_billing",)]
)
def test_an_old_state_file_is_migrated_in_place(tmp_path, missing):
    root = tmp_path / "old-state"
    root.mkdir(mode=0o700)
    ddl = store_module._SCHEMA
    start = ddl.index("CREATE TABLE IF NOT EXISTS runs")
    end = ddl.index(");", start) + 2
    runs = "\n".join(
        line for line in ddl[start:end].splitlines() if not any(name in line for name in missing)
    )
    db = sqlite3.connect(root / "state.db")
    db.executescript(runs)
    db.execute(
        "INSERT INTO runs (request_id, mode, status, packet_hash, packet_json, created_at,"
        " updated_at, deadline_at) VALUES ('old', 'alternate', 'complete', 'h', '{}', 1, 1, 2)"
    )
    db.commit()
    assert db.execute("PRAGMA user_version").fetchone()[0] == 0
    db.close()
    store = Store(root)
    with store.connect() as conn:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
        assert {"plan_json", "progress_json", "failed_route_billing"} <= columns
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    row = store.get_run("old")
    assert row["status"] == "complete" and row["progress_json"] == "{}" and row["plan_json"] is None
    Store(root)  # opening again is a no-op
