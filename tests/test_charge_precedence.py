"""A paid attempt that may have been billed outranks every other cause; job billing comes
from the ledger, not from the route that happened to fail; a paid failure is released only
when the provider proves the request never left; contract-hook failures keep their record."""

from __future__ import annotations

import time

import pytest

from coldtake.contracts import CoreContract
from coldtake.failures import ProviderFailure
from coldtake.routing import Plan

from .conftest import PACKET
from .fakes import memo
from .helpers import P, S, Scripted, spy_rig

PANEL = {**PACKET, "mode": "panel"}


def _ledger(rig):
    with rig.engine.store.connect() as db:
        return [dict(r) for r in db.execute("SELECT model, status, actual_cost FROM ledger")]


def _attempts(rig):
    with rig.engine.store.connect() as db:
        return [dict(r) for r in db.execute("SELECT * FROM attempts ORDER BY id")]


def _panel(make_rig, behave, *, slots, chain=(), spy_cost=0.01, **config):
    spy = Scripted(behave)
    spy.cost = spy_cost
    rig, _ = spy_rig(make_rig, lambda ctx: Plan(slots=slots, chain=chain), spy, **config)
    return rig, spy


def _run(rig, packet=PANEL, request_id="req-1"):
    rig.engine.submit(request_id, packet, lead_family="anthropic")
    return rig.engine.wait(request_id, 30)


# -- H1: charge_unknown wins, whatever order the members fail in --------------------------


@pytest.mark.parametrize(
    "sub_failure",
    [
        ProviderFailure("timed_out", "timed_out", "slow"),
        ProviderFailure("refusal", "terminal", "no"),
        ProviderFailure("unknown", "terminal", "boom"),
    ],
)
def test_a_paid_charge_unknown_is_not_hidden_behind_an_earlier_member(make_rig, sub_failure):
    def behave(prompt, route, n):
        if route.model == "sub":
            time.sleep(0.1)  # let the paid member start before slot 1 fails and cancels
            raise sub_failure
        time.sleep(0.3)  # the paid member finishes last, after slot 1 already failed
        raise ProviderFailure("provider_deadline_exceeded", "charge_unknown", "lost reply")

    rig, _ = _panel(make_rig, behave, slots=((S("sub", "f1"),), (P("paid", "f2"),)))
    out = _run(rig)
    assert out["status"] == "charge_unknown" and out["reason"] == "provider_deadline_exceeded"
    assert out["billing"] == "paid" and out["charge_unknown"] is True
    assert out["failed_route_billing"] == "paid"
    assert [r["status"] for r in _ledger(rig)] == ["charge_unknown"]
    assert "do not resubmit" in out["recovery"].lower()


def test_job_billing_is_paid_when_a_paid_member_committed_but_a_subscription_failure_decided(
    make_rig,
):
    def behave(prompt, route, n):
        if route.model == "sub":
            time.sleep(0.2)
            raise ProviderFailure("timed_out", "timed_out", "slow")
        return memo("paid answer")

    rig, _ = _panel(make_rig, behave, slots=((S("sub", "f1"),), (P("paid", "f2"),)))
    out = _run(rig)
    assert out["status"] == "timed_out"
    assert out["billing"] == "paid"  # money was committed
    assert out["failed_route_billing"] == "subscription"  # the route that decided the job
    assert out["charge_unknown"] is False
    assert [r["status"] for r in _ledger(rig)] == ["committed"]


def test_a_single_ladder_reports_paid_billing_after_a_committed_paid_attempt(make_rig):
    def behave(prompt, route, n):
        if route.model == "paid":  # answered, unusable, accepted: committed, then falls back
            raise ProviderFailure("quota", "fallback", "reply cut", accepted=True, cost_usd=0.002)
        raise ProviderFailure("timed_out", "timed_out", "slow")

    spy = Scripted(behave)
    rig, _ = spy_rig(make_rig, lambda ctx: Plan(chain=(P("paid", "fp"), S("sub", "fs"))), spy)
    out = _run(rig, PACKET)
    assert out["status"] == "timed_out"
    assert out["billing"] == "paid" and out["failed_route_billing"] == "subscription"


def test_a_subscription_only_job_reports_subscription_billing(make_rig):
    rig, _ = spy_rig(make_rig, lambda ctx: Plan(chain=(S("sub"),)))
    out = _run(rig, PACKET)
    assert out["status"] == "complete" and out["billing"] == "subscription"
    assert out["result"]["billing"] == "subscription" and out["result"]["charge_unknown"] is False


def test_a_paid_judge_charge_unknown_is_surfaced_on_a_complete_job(make_rig):
    def behave(prompt, route, n):
        if "COLDTAKE_ROLE: judge" in prompt:
            raise ProviderFailure("provider_deadline_exceeded", "charge_unknown", "lost reply")
        return memo(f"via {route.model}")

    rig, _ = _panel(
        make_rig, behave, slots=((S("a", "f1"),), (S("b", "f2"),)), chain=(P("judge", "fj"),)
    )
    out = _run(rig)
    assert out["status"] == "complete" and len(out["result"]["memos"]) == 2
    comparison = out["result"]["comparison"]
    assert comparison["status"] == "unavailable" and comparison["cause"] == "charge_unknown"
    assert comparison["cause_detail"]["reason"] == "provider_deadline_exceeded"
    # the top-level flag, in the stored result and in the status view
    assert out["result"]["charge_unknown"] is True and out["result"]["billing"] == "paid"
    assert out["charge_unknown"] is True and out["billing"] == "paid"
    assert out["result"]["paid"]["charge_unknown_held_usd"] > 0
    assert [r["status"] for r in _ledger(rig)] == ["charge_unknown"]


def test_a_failed_judge_that_was_provably_unsent_is_not_a_charge(make_rig):
    def behave(prompt, route, n):
        if "COLDTAKE_ROLE: judge" in prompt:
            raise ProviderFailure("rate_limited", "fallback", "429", proven_unsent=True)
        return memo(f"via {route.model}")

    rig, _ = _panel(
        make_rig, behave, slots=((S("a", "f1"),), (S("b", "f2"),)), chain=(P("judge", "fj"),)
    )
    out = _run(rig)
    assert out["status"] == "complete" and out["result"]["charge_unknown"] is False
    assert out["result"]["comparison"]["cause"] == "rate_limited"
    assert [r["status"] for r in _ledger(rig)] == ["released"]
    assert out["billing"] == "subscription"  # a released attempt never ran


def test_an_unfinalized_paid_attempt_makes_any_failure_a_charge_unknown(make_rig, monkeypatch):
    spy = Scripted()
    rig, _ = spy_rig(make_rig, lambda ctx: Plan(chain=(P("paid", "fp"),)), spy)
    real = rig.engine.store.set_progress
    seen = []

    def explode(request_id, key, state):
        seen.append(state and state.get("dispatched"))
        if state and state.get("dispatched"):
            raise RuntimeError("store went away after authorization")
        return real(request_id, key, state)

    monkeypatch.setattr(rig.engine.store, "set_progress", explode)
    out = _run(rig, PACKET)
    assert out["status"] == "charge_unknown" and out["reason"] == "internal_error"
    assert [r["status"] for r in _ledger(rig)] == ["charge_unknown"]
    assert spy.counts == {}  # the provider was never called, but nobody can prove it


# -- M4: failures raised by contract hooks keep their attempt record ----------------------


class HookFails(CoreContract):
    def __init__(self, config, error):
        super().__init__(config)
        self.error = error

    def validate_memo(self, data, packet):
        raise self.error


@pytest.mark.parametrize("route_factory,billing", [(P, "paid"), (S, "subscription")])
def test_a_provider_failure_from_a_hook_is_recorded_with_the_right_billing(
    make_rig, route_factory, billing
):
    rig, _ = spy_rig(make_rig, lambda ctx: Plan(chain=(route_factory("m", "fx"),)))
    rig.engine.register_contract(
        HookFails(rig.config, ProviderFailure("capacity", "terminal", "no room"))
    )
    out = _run(rig, PACKET)
    assert out["status"] == "failed" and out["reason"] == "capacity"
    (attempt,) = _attempts(rig)
    assert attempt["billing"] == billing and attempt["outcome"] == "capacity"
    assert attempt["role"] == "advisor" and attempt["cause_message"] == "no room"
    assert out["failed_route_billing"] == billing
    assert out["billing"] == ("paid" if billing == "paid" else "subscription")
    if billing == "paid":
        assert attempt["cost_usd"] == pytest.approx(0.001)  # the committed completion cost
        assert [r["status"] for r in _ledger(rig)] == ["committed"]


def test_a_security_failure_from_a_hook_on_a_paid_route_is_not_a_subscription_job(make_rig):
    rig, _ = spy_rig(make_rig, lambda ctx: Plan(chain=(P("m", "fx"), S("s", "fs"))))
    rig.engine.register_contract(
        HookFails(rig.config, ProviderFailure("suspected_compromise", "terminal", "tampered"))
    )
    out = _run(rig, PACKET)
    assert out["status"] == "failed" and out["billing"] == "paid"
    assert [a["billing"] for a in _attempts(rig)] == ["paid"]


def test_a_hook_that_raises_a_fallback_failure_moves_to_the_next_route(make_rig):
    class Once(CoreContract):
        def validate_memo(self, data, packet):
            if "via first" in data["recommendation"]:
                raise ProviderFailure("capacity", "fallback", "full", proven_unsent=True)
            return super().validate_memo(data, packet)

    rig, _ = spy_rig(make_rig, lambda ctx: Plan(chain=(S("first", "f1"), S("second", "f2"))))
    rig.engine.register_contract(Once(rig.config))
    out = _run(rig, PACKET)
    assert out["status"] == "complete" and out["result"]["memos"][0]["model"] == "second"
    assert [a["outcome"] for a in _attempts(rig)] == ["capacity", "ok"]


def test_a_bug_in_a_hook_is_an_internal_error_with_a_record_and_billing(make_rig):
    rig, _ = spy_rig(make_rig, lambda ctx: Plan(chain=(P("m", "fx"),)))
    rig.engine.register_contract(HookFails(rig.config, KeyError("oops")))
    out = _run(rig, PACKET)
    assert out["status"] == "failed" and out["reason"] == "internal_error"
    assert out["failed_route_billing"] == "paid" and out["billing"] == "paid"
    (attempt,) = _attempts(rig)
    assert attempt["outcome"] == "KeyError" and attempt["billing"] == "paid"


# -- M3: a paid failure is released only when it is proven unsent --------------------------


@pytest.mark.parametrize(
    "failure",
    [
        ProviderFailure("quota", "fallback", "limit"),
        ProviderFailure("refusal", "terminal", "no"),
        ProviderFailure("unavailable", "fallback", "502"),
    ],
)
def test_a_paid_failure_that_is_not_proven_unsent_is_charge_unknown(make_rig, failure):
    def behave(prompt, route, n):
        if route.model == "first":
            raise failure
        return memo("never reached")

    spy = Scripted(behave)
    rig, _ = spy_rig(make_rig, lambda ctx: Plan(chain=(P("first", "f1"), S("second", "f2"))), spy)
    out = _run(rig, PACKET)
    assert out["status"] == "charge_unknown" and out["billing"] == "paid"
    assert [r["status"] for r in _ledger(rig)] == ["charge_unknown"]
    assert "second" not in spy.counts  # no fallback after a possibly billed attempt
    assert failure.reason in _attempts(rig)[0]["cause_message"]  # the cause is kept


def test_a_proven_unsent_paid_failure_is_released_and_falls_back(make_rig):
    def behave(prompt, route, n):
        if route.model == "first":
            raise ProviderFailure("rate_limited", "fallback", "429", proven_unsent=True)
        return memo("second answer")

    spy = Scripted(behave)
    rig, _ = spy_rig(make_rig, lambda ctx: Plan(chain=(P("first", "f1"), S("second", "f2"))), spy)
    out = _run(rig, PACKET)
    assert out["status"] == "complete" and out["billing"] == "subscription"
    assert [r["status"] for r in _ledger(rig)] == ["released"]


def test_a_paid_failure_that_carries_a_reply_is_committed(make_rig):
    def behave(prompt, route, n):
        raise ProviderFailure(
            "refusal", "terminal", "model declined", accepted=True, cost_usd=0.004
        )

    spy = Scripted(behave)
    rig, _ = spy_rig(make_rig, lambda ctx: Plan(chain=(P("only", "f1"),)), spy)
    out = _run(rig, PACKET)
    assert out["status"] == "failed" and out["reason"] == "refusal"
    (row,) = _ledger(rig)
    assert row["status"] == "committed" and row["actual_cost"] == pytest.approx(0.004)


def test_a_subscription_failure_is_never_a_charge(make_rig):
    def behave(prompt, route, n):
        raise ProviderFailure("quota", "fallback", "limit")  # no proven_unsent needed

    spy = Scripted(behave)
    rig, _ = spy_rig(make_rig, lambda ctx: Plan(chain=(S("a", "f1"), S("b", "f2"))), spy)
    out = _run(rig, PACKET)
    assert out["status"] == "failed" and out["billing"] == "subscription"
    assert _ledger(rig) == []


def test_the_built_in_openrouter_marks_only_what_it_can_prove(make_rig):
    import httpx

    from coldtake.providers import OpenRouterProvider

    provider = OpenRouterProvider("key", "https://router.invalid/api")
    for status in (400, 401, 402, 403, 404, 413, 422, 429):  # rejected before inference
        with pytest.raises(ProviderFailure) as raised:
            provider._interpret(status, "no")
        assert raised.value.proven_unsent is True
    for status in (408, 418, 500, 502, 503, 504, 529):  # sent: ambiguous by policy
        with pytest.raises(ProviderFailure) as raised:
            provider._interpret(status, "no")
        assert raised.value.kind == "charge_unknown" and raised.value.proven_unsent is False

    def never_connects(request):
        raise httpx.ConnectError("refused")

    provider = OpenRouterProvider(
        "key", "https://router.invalid/api", transport=httpx.MockTransport(never_connects)
    )
    with pytest.raises(ProviderFailure) as raised:
        provider._send({}, {}, 5)
    assert raised.value.kind == "fallback" and raised.value.proven_unsent is True

    def resets(request):
        raise httpx.ReadError("reset")

    provider = OpenRouterProvider(
        "key", "https://router.invalid/api", transport=httpx.MockTransport(resets)
    )
    with pytest.raises(ProviderFailure) as raised:
        provider._send({}, {}, 5)
    assert raised.value.kind == "charge_unknown"


def test_a_paid_503_holds_the_allowance_and_does_not_fall_back(make_rig):
    import httpx

    from .fakes import FakeRouter

    router = FakeRouter(httpx.Response(503, text="busy"))
    rig = make_rig(router=router)
    rig.codex.script(advisor=[{"error": "You've hit your usage limit", "code": 1}])
    out = rig.run(PACKET)
    assert out["status"] == "charge_unknown" and out["billing"] == "paid"
    assert [r["status"] for r in _ledger(rig)] == ["charge_unknown"]
    assert len(router.requests) == 1


# -- recovery uses the same precedence rule --------------------------------------------------


def test_a_recovered_run_with_a_finalized_charge_unknown_row_is_charge_unknown(store):
    from coldtake.budget import Ledger

    clock = [1000.0]
    store.clock = lambda: clock[0]
    ledger = Ledger(store, 1.0, 10.0)
    store.create_run(
        "r1", mode="panel", packet_hash="h", packet_json="{}", sealed_answer=None,
        sealed_choice=None, sealed_sha256=None, lead_family="claude", warnings=[],
        instance="i1", lease_seconds=30, job_seconds=600,
    )  # fmt: skip
    ledger.authorize("r1", "advisor1-aa-0-r0-a0", "m", 0.1, instance="i1")
    ledger.finalize("r1", "advisor1-aa-0-r0-a0", "charge_unknown")  # finalized, none authorized
    clock[0] += 120  # the lease expires while another member still runs
    assert store.reconcile("r1") == ["r1"]
    row = store.get_run("r1")
    assert row["status"] == "charge_unknown" and row["reason"] == "charge_unknown"
    assert ledger.request_state("r1")["charge_unknown"] is True


def test_a_crashed_panel_run_reads_charge_unknown_with_do_not_resubmit(make_rig):
    def behave(prompt, route, n):
        if route.model == "paid":
            raise ProviderFailure("provider_deadline_exceeded", "charge_unknown", "lost reply")
        time.sleep(0.5)
        return memo("x")

    rig, _ = _panel(make_rig, behave, slots=((P("paid", "f1"),), (S("b", "f2"),)))
    rig.engine._fail = lambda job, failed: None  # the process died before storing the failure
    rig.engine.submit("r2", PANEL, lead_family="anthropic")
    time.sleep(1.0)
    with rig.engine.store.transaction() as db:
        db.execute("UPDATE runs SET lease_expires_at=0 WHERE request_id='r2'")
    out = rig.engine.status("r2")
    assert out["status"] == "charge_unknown" and out["charge_unknown"] is True
    assert "do not resubmit" in out["recovery"].lower()


# -- a complete job never leaves a paid attempt authorized ----------------------------------


def test_a_store_error_after_a_judge_authorization_is_a_charge_unknown_comparison(
    make_rig, monkeypatch
):
    rig, _ = _panel(
        make_rig, lambda p, r, n: memo(f"via {r.model}"),
        slots=((S("a", "f1"),), (S("b", "f2"),)), chain=(P("judge", "fj"),),
    )  # fmt: skip
    real = rig.engine.store.set_progress

    def explode(request_id, key, state):
        if key.startswith("judge") and state and state.get("dispatched"):
            raise RuntimeError("store hiccup after judge authorization")
        return real(request_id, key, state)

    monkeypatch.setattr(rig.engine.store, "set_progress", explode)
    out = _run(rig)
    assert out["status"] == "complete" and out["charge_unknown"] is True
    comparison = out["result"]["comparison"]
    assert comparison["status"] == "unavailable" and comparison["cause"] == "charge_unknown"
    assert out["result"]["charge_unknown"] is True and out["billing"] == "paid"
    assert [r["status"] for r in _ledger(rig)] == ["charge_unknown"]  # nothing left authorized


# -- a hook may end the job with its own failure ---------------------------------------------


def test_a_job_failure_from_a_hook_keeps_its_status_and_reason(make_rig):
    from coldtake.engine import JobFailure

    rig, _ = spy_rig(make_rig, lambda ctx: Plan(chain=(P("m", "fx"),)))
    rig.engine.register_contract(
        HookFails(rig.config, JobFailure("timed_out", "my_reason", {"type": "T", "message": "m"}))
    )
    out = _run(rig, PACKET)
    assert out["status"] == "timed_out" and out["reason"] == "my_reason"
    assert out["failed_route_billing"] == "paid" and out["billing"] == "paid"
    (attempt,) = _attempts(rig)
    assert attempt["outcome"] == "my_reason" and attempt["billing"] == "paid"


ROUTING_503 = (
    '{"error": {"code": 503, "message": '
    '"No available model provider meets your routing requirements"}}'
)


def test_only_openrouters_routing_503_is_proven_unsent(make_rig):
    from coldtake.providers import OpenRouterProvider, routing_rejection

    provider = OpenRouterProvider("key", "https://router.invalid/api")
    with pytest.raises(ProviderFailure) as raised:
        provider._interpret(503, ROUTING_503)
    failure = raised.value
    assert failure.kind == "fallback" and failure.proven_unsent is True
    assert failure.cause_type == "HTTP503NoProvider"
    assert "No available model provider meets your routing requirements" in failure.message
    for body in (
        "busy",
        "",
        "{}",
        '{"error": {"message": "upstream overloaded"}}',
        '{"error": "No available model provider meets your routing requirements"}',
        '{"error": {"message": "No available model provider"}}',
        '["error"]',
    ):
        assert routing_rejection(body) is None
        with pytest.raises(ProviderFailure) as raised:
            provider._interpret(503, body)
        assert raised.value.kind == "charge_unknown" and raised.value.proven_unsent is False
    with pytest.raises(ProviderFailure) as raised:  # the same body on another status
        provider._interpret(502, ROUTING_503)
    assert raised.value.kind == "charge_unknown"


def test_a_routing_503_releases_the_allowance(make_rig):
    import httpx

    from .fakes import FakeRouter

    router = FakeRouter(httpx.Response(503, text=ROUTING_503))
    rig = make_rig(router=router)
    rig.codex.script(advisor=[{"error": "You've hit your usage limit", "code": 1}])
    out = rig.run(PACKET)
    assert out["status"] == "failed" and out["billing"] == "subscription"
    assert out["status"] == "failed"
    statuses = {r["status"] for r in _ledger(rig)}
    assert statuses == {"released"}  # every paid route was refused before inference


# -- a JobFailure from a hook stops sibling members too ------------------------------------


def test_a_job_failure_from_a_hook_in_one_member_cancels_the_others(make_rig):
    from coldtake.engine import JobFailure

    class HookFailsOnBad(HookFails):
        def validate_memo(self, data, packet):
            if data.get("recommendation") == "bad":
                raise self.error
            return super(HookFails, self).validate_memo(data, packet)

    def behave(prompt, route, n):
        if route.model == "a":
            return memo("bad")
        if route.model == "b1":
            time.sleep(0.6)  # still running when member 1 ends the job
            raise ProviderFailure("rate_limited", "fallback", "busy")
        return memo("ok")

    rig, spy = _panel(make_rig, behave, slots=((S("a", "f1"),), (S("b1", "f2"), P("b2", "f2"))))
    rig.engine.register_contract(
        HookFailsOnBad(rig.config, JobFailure("failed", "my_reason", {"type": "T", "message": "m"}))
    )
    out = _run(rig)
    assert out["status"] == "failed" and out["reason"] == "my_reason"
    assert "b2" not in spy.counts  # the sibling's paid fallback never started
    assert _ledger(rig) == []


def test_a_charge_unknown_member_still_outranks_a_hook_job_failure(make_rig):
    from coldtake.engine import JobFailure

    class HookFailsOnBad(HookFails):
        def validate_memo(self, data, packet):
            if data.get("recommendation") == "bad":
                raise self.error
            return super(HookFails, self).validate_memo(data, packet)

    def behave(prompt, route, n):
        if route.model == "a":
            return memo("bad")
        time.sleep(0.3)
        raise ProviderFailure("provider_deadline_exceeded", "charge_unknown", "lost reply")

    rig, _ = _panel(make_rig, behave, slots=((S("a", "f1"),), (P("paid", "f2"),)))
    rig.engine.register_contract(
        HookFailsOnBad(rig.config, JobFailure("failed", "my_reason", {"type": "T", "message": "m"}))
    )
    out = _run(rig)
    assert out["status"] == "charge_unknown" and out["billing"] == "paid"


@pytest.mark.parametrize("status", ["failed", "timed_out", "lost", "charge_unknown"])
def test_job_failure_accepts_only_the_terminal_statuses(status):
    from coldtake.engine import JobFailure

    assert JobFailure(status, "r", {}).status == status


@pytest.mark.parametrize("status", ["complete", "running", "ok", ""])
def test_job_failure_rejects_any_other_status(status):
    from coldtake.engine import JobFailure

    with pytest.raises(ValueError, match="status"):
        JobFailure(status, "r", {})


def test_a_job_failure_from_a_judge_validator_is_a_compare_error_and_the_job_completes(make_rig):
    from coldtake.engine import JobFailure

    def compare(cx):
        def validate(data):
            raise JobFailure("failed", "judge_says_no", {"type": "T", "message": "m"})

        cx.judge("COLDTAKE_ROLE: judge\nx", {"type": "object"}, validate)

    rig, _ = _panel(
        make_rig, lambda p, r, n: memo(f"via {r.model}"),
        slots=((S("a", "f1"),), (S("b", "f2"),)), chain=(S("judge", "fj"),),
    )  # fmt: skip
    rig.engine.register_contract(CoreContract(rig.config, compare=compare))
    out = _run(rig)
    assert out["status"] == "complete"
    comparison = out["result"]["comparison"]
    assert comparison["status"] == "unavailable" and comparison["cause"] == "compare_error"
