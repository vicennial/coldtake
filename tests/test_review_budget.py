"""Budget hardening: unusable costs and prices, lease-checked authorization, preflight of
every paid route, repair accounting, wall-clock limits and unique attempt keys."""

from __future__ import annotations

import json
import threading
import time

import httpx
import pytest

from coldtake.budget import BudgetExceeded, LeaseLost, Ledger, clean_cost
from coldtake.contracts import CoreContract
from coldtake.failures import ProviderFailure
from coldtake.routing import Plan

from .conftest import PACKET
from .fakes import FakeRouter, memo
from .helpers import P, S, Scripted, invalid, spy_rig
from .test_recovery import ghost

QUOTA = [{"error": "You've hit your usage limit", "code": 1}]


def _ledger_rows(rig):
    with rig.engine.store.connect() as db:
        return [dict(r) for r in db.execute("SELECT * FROM ledger ORDER BY authorized_at")]


# -- H1: costs and prices ----------------------------------------------------------------


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True, "0.5", None])
def test_clean_cost_accepts_only_finite_non_negative_numbers(value):
    assert clean_cost(value) is None
    assert clean_cost(0) == 0.0 and clean_cost(0.25) == 0.25


@pytest.mark.parametrize("cost", ["NaN", "Infinity", "-1", "true", '"0.5"', "null"])
def test_an_unusable_reported_cost_commits_the_full_allowance(make_rig, cost):
    body = (
        '{"model":"m","choices":[{"finish_reason":"stop","message":{"content":'
        + json.dumps(json.dumps(memo("paid")))
        + '}}],"usage":{"prompt_tokens":1,"completion_tokens":1,"cost":'
        + cost
        + "}}"
    )
    router = FakeRouter(httpx.Response(200, content=body.encode()))
    rig = make_rig(router=router)
    rig.codex.script(advisor=QUOTA)
    assert rig.run(PACKET)["status"] == "complete"
    (row,) = _ledger_rows(rig)
    assert row["status"] == "committed" and row["actual_cost"] is None and row["amount"] > 0
    assert rig.engine.ledger.spent_today() == pytest.approx(row["amount"])


def test_the_ledger_never_stores_an_unusable_cost(store):
    ledger = Ledger(store, 5.0, 5.0)
    for i, bad in enumerate([float("nan"), -3.0, float("inf")]):
        ledger.authorize("r", f"a{i}", "m", 0.1)
        ledger.finalize("r", f"a{i}", "committed", bad)
    assert ledger.spent_today() == pytest.approx(0.3)
    with pytest.raises(BudgetExceeded):
        ledger.authorize("r", "nan", "m", float("nan"))
    with pytest.raises(BudgetExceeded):
        ledger.authorize("r", "neg", "m", -1.0)


@pytest.mark.parametrize(
    "route",
    [
        P("m", completion_usd_per_mtok=0.0),
        P("m", completion_usd_per_mtok=-1.0),
        P("m", completion_usd_per_mtok=float("nan")),
        P("m", prompt_usd_per_mtok=-0.5),
        P("m", prompt_usd_per_mtok=float("inf")),
        P("m", max_output_tokens=0),
        P("m", max_output_tokens=-5),
    ],
)
def test_an_unusable_resolver_price_is_refused_before_ack(make_rig, route):
    rig, spy = spy_rig(make_rig, lambda ctx: Plan(chain=(S("sub"), route)))
    with pytest.raises(BudgetExceeded, match="unusable"):
        rig.engine.submit("req-1", PACKET, lead_family="anthropic")
    assert rig.engine.store.get_run("req-1") is None and spy.prompts == []


def test_an_unusable_price_in_a_panel_reserve_or_escalation_is_refused_too(make_rig):
    bad = P("bad", completion_usd_per_mtok=0.0)
    panel = {**PACKET, "mode": "panel"}
    for plan in (
        Plan(slots=((S("a"),), (S("b", "f2"),)), reserve=(bad,)),
        Plan(slots=((S("a"),), (S("b", "f2"),)), escalation=(bad,), escalate=lambda m: True),
    ):
        rig, _ = spy_rig(make_rig, lambda ctx, plan=plan: plan)
        with pytest.raises(BudgetExceeded):
            rig.engine.submit("req-1", panel, lead_family="anthropic")


def test_a_zero_worst_case_from_a_provider_override_is_refused(make_rig):
    spy = Scripted()
    spy.cost = 0.0
    rig, _ = spy_rig(make_rig, lambda ctx: Plan(chain=(P("m"),)), spy)
    with pytest.raises(BudgetExceeded, match="worst-case"):
        rig.engine.submit("req-1", PACKET, lead_family="anthropic")


def test_a_paid_repair_with_a_reply_is_committed_even_without_the_accepted_flag(make_rig):
    def behave(prompt, route, n):
        raise invalid("not a memo")  # a reply arrived but it is unusable; accepted is unset

    spy = Scripted(behave)
    rig, _ = spy_rig(make_rig, lambda ctx: Plan(chain=(P("m"),)), spy)
    out = rig.run(PACKET)
    assert out["status"] == "failed" and out["reason"] == "schema_exhausted"
    rows = _ledger_rows(rig)
    assert [r["status"] for r in rows] == ["committed", "committed"]
    assert rig.engine.ledger.spent_today() == pytest.approx(sum(r["amount"] for r in rows))


# -- M4: authorization is lease-checked --------------------------------------------------


def test_authorization_requires_a_running_job_owned_by_the_caller(rig):
    ghost(rig.engine, "g1", lease=100, instance="owner-a")
    ledger = rig.engine.ledger
    with pytest.raises(LeaseLost):
        ledger.authorize("g1", "a0", "m", 0.01, instance="owner-b")
    ledger.authorize("g1", "a1", "m", 0.01, instance="owner-a")
    rig.engine.store.finish("g1", "owner-a", "failed", reason="x")
    with pytest.raises(LeaseLost):
        ledger.authorize("g1", "a2", "m", 0.01, instance="owner-a")
    with pytest.raises(LeaseLost):
        ledger.authorize("missing", "a3", "m", 0.01, instance="owner-a")
    assert [r["attempt_key"] for r in _ledger_rows(rig)] == ["g1:a1"]


def test_a_recovered_job_never_spends(make_rig):
    """The worker's job is recovered (stored as lost) while its first attempt runs; the
    paid fallback must be refused by the ledger transaction and send nothing."""
    router = FakeRouter(memo("paid"))
    rig = make_rig(router=router)
    rig.codex.script(advisor=[{"sleep": 1.5, "then": {"error": "usage limit", "code": 1}}])
    rig.engine.submit("req-1", PACKET, lead_family="anthropic")
    deadline = time.monotonic() + 10
    while not rig.codex.calls() and time.monotonic() < deadline:
        time.sleep(0.05)
    with rig.engine.store.connect() as db:
        db.execute("UPDATE runs SET status='lost', reason='lost' WHERE request_id='req-1'")
    rig.engine.drain(15)
    assert router.requests == []
    assert rig.engine.store.get_run("req-1")["status"] == "lost"
    assert _ledger_rows(rig) == []


def test_a_failing_heartbeat_stops_the_job_from_starting_new_attempts(make_rig, monkeypatch):
    router = FakeRouter(memo("paid"))
    rig = make_rig({"limits": {"lease_seconds": 5}}, router)

    def broken(*args, **kwargs):
        raise OSError("database is locked")

    monkeypatch.setattr(rig.engine.store, "heartbeat", broken)
    rig.codex.script(advisor=[{"sleep": 3, "then": {"error": "usage limit", "code": 1}}])
    out = rig.run(PACKET)
    assert out["status"] == "failed" and out["reason"] == "cancelled"
    assert router.requests == [] and _ledger_rows(rig) == []


# -- M7: preflight and repair size -------------------------------------------------------

PANEL = {**PACKET, "mode": "panel"}


def _preflight(**plan):
    return lambda ctx: Plan(preflight_ladder_budget=True, **plan)


def test_preflight_counts_the_replacement_pool(make_rig):
    spy = Scripted()
    spy.cost = 0.3  # one paid route: 0.3 + 0.3 repair = 0.6
    plan = _preflight(slots=((S("a"),), (S("b", "f2"),)), reserve=(P("r"),))
    rig, _ = spy_rig(make_rig, plan, spy, budget={"per_request_usd": 0.5, "daily_usd": 5})
    with pytest.raises(BudgetExceeded, match="ladder"):
        rig.engine.submit("req-1", PANEL, lead_family="anthropic")
    rig, _ = spy_rig(make_rig, plan, spy, budget={"per_request_usd": 0.7, "daily_usd": 5})
    rig.engine.submit("req-2", PANEL, lead_family="anthropic")
    assert rig.engine.wait("req-2", 30)["status"] == "complete"


def test_preflight_counts_the_escalation_pool(make_rig):
    spy = Scripted()
    spy.cost = 0.3
    plan = _preflight(
        slots=((S("a"),), (S("b", "f2"),)), escalation=(P("e"),), escalate=lambda memos: False
    )
    rig, _ = spy_rig(make_rig, plan, spy, budget={"per_request_usd": 0.5, "daily_usd": 5})
    with pytest.raises(BudgetExceeded):
        rig.engine.submit("req-1", PANEL, lead_family="anthropic")


def test_an_unaffordable_paid_judge_makes_the_comparison_unavailable_not_the_panel_refused(
    make_rig,
):
    spy = Scripted()
    spy.cost = 0.3
    plan = _preflight(slots=((S("a"),), (S("b", "f2"),)), chain=(P("judge"),))
    rig, _ = spy_rig(make_rig, plan, spy, budget={"per_request_usd": 0.5, "daily_usd": 5})
    rig.engine.submit("req-1", PANEL, lead_family="anthropic")  # admitted: advisors are free
    out = rig.engine.wait("req-1", 30)
    assert out["status"] == "complete"
    comparison = out["result"]["comparison"]
    assert comparison["status"] == "unavailable" and comparison["cause"] == "judge_unaffordable"
    view = rig.engine.run_view("req-1")
    assert [r["model"] for r in view.plan["judge_dropped"]] == ["judge"]
    assert view.plan["judge"] == []
    assert not [c for c in spy.calls("judge")]  # nothing was ever sent to the judge


def test_a_paid_advisor_ladder_that_overflows_the_cap_is_still_refused(make_rig):
    spy = Scripted()
    spy.cost = 0.3
    plan = _preflight(slots=((S("a"),), (P("b", "f2"),)))
    rig, _ = spy_rig(make_rig, plan, spy, budget={"per_request_usd": 0.5, "daily_usd": 5})
    with pytest.raises(BudgetExceeded):
        rig.engine.submit("req-1", PANEL, lead_family="anthropic")


def test_the_repair_bound_is_the_framing_plus_four_bytes_per_output_token(rig):
    handler = rig.engine.contracts["coldtake_v1"]
    route = P("m", max_output_tokens=500)
    prompt = "x" * 100
    framing = len(handler.repair_prompt(prompt, "", "x" * 800).encode()) - 100
    assert rig.engine._repair_bytes(handler, prompt, route) == framing + 2000

    class Hooked(CoreContract):
        def repair_overhead_bytes(self, prompt, route):
            return 7

    assert rig.engine._repair_bytes(Hooked(), prompt, route) == 7


def test_a_blocked_repair_is_terminal_and_keeps_the_partial_advice(make_rig):
    def behave(prompt, route, n):
        raise invalid("not json", accepted=True, raw="the half-formed advice")

    spy = Scripted(behave)
    spy.cost = 0.4  # the first attempt fits the 0.5 cap; the repair does not
    rig, _ = spy_rig(
        make_rig, lambda ctx: Plan(chain=(P("m"), S("sub2", "f2"))), spy,
        budget={"per_request_usd": 0.5, "daily_usd": 5},
    )  # fmt: skip
    out = rig.run(PACKET)
    assert out["status"] == "failed" and out["reason"] == "schema_exhausted"
    assert out["partial_advice"]["raw"] == "the half-formed advice"
    assert "sub2" not in spy.counts  # terminal: no fallback to the next route
    assert [a["outcome"] for a in rig.attempts()][-1] == "budget_blocked"


# -- L2: wall-clock limit on the paid call -----------------------------------------------


def test_a_slow_paid_response_is_charge_unknown_at_the_wall_clock_limit(make_rig):
    release = threading.Event()

    def handler(request):
        release.wait(5)
        return httpx.Response(200, json={})

    router = FakeRouter()
    router.transport = httpx.MockTransport(handler)
    rig = make_rig({"limits": {"attempt_seconds": 1, "job_seconds": 20}}, router)
    rig.codex.script(advisor=QUOTA)
    started = time.monotonic()
    out = rig.run(PACKET)
    release.set()
    assert out["status"] == "charge_unknown"
    assert time.monotonic() - started < 4
    (row,) = _ledger_rows(rig)
    assert row["status"] == "charge_unknown" and row["amount"] > 0
    assert rig.attempts()[-1]["cause_type"] == "WallClockTimeout"


def test_an_oversized_paid_response_is_charge_unknown(make_rig, monkeypatch):
    monkeypatch.setattr("coldtake.providers.MAX_OUTPUT_BYTES", 1000)
    router = FakeRouter(httpx.Response(200, content=b"x" * 5000))
    rig = make_rig(router=router)
    rig.codex.script(advisor=QUOTA)
    assert rig.run(PACKET)["status"] == "charge_unknown"


# -- L9: unique attempt keys -------------------------------------------------------------


def test_run_chain_calls_for_one_request_never_collide(make_rig):
    spy = Scripted()
    spy.cost = 0.1
    rig, _ = spy_rig(make_rig, lambda ctx: Plan(chain=(S("sub"),)), spy)
    schema = {"type": "object", "properties": {}}
    for _ in range(3):
        rig.engine.run_chain("ext-1", (P("m"),), "hello", schema, lambda d: d)
    keys = [r["attempt_key"] for r in _ledger_rows(rig)]
    assert len(keys) == 3 == len(set(keys))


def test_reserve_replacement_attempts_have_their_own_ledger_keys(make_rig):
    def behave(prompt, route, n):
        if route.model == "lost":
            raise ProviderFailure("quota", "fallback", "limit", proven_unsent=True)
        return memo(f"via {route.model}")

    spy = Scripted(behave)
    spy.cost = 0.1
    plan = Plan(
        slots=((P("lost", "f1"),), (P("b", "f2"),)), reserve=(P("r", "f3"),),
    )  # fmt: skip
    rig, _ = spy_rig(make_rig, lambda ctx: plan, spy)
    out = rig.run(PANEL)
    assert out["status"] == "complete" and out["result"]["advisor_count"] == 2
    keys = [r["attempt_key"] for r in _ledger_rows(rig)]
    assert len(keys) == 3 == len(set(keys))
