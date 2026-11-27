"""Escalation and replacement never seat a family twice or re-run a failed route; the judge
repairs with its own schema and is planned apart from the advisors."""

from __future__ import annotations

from coldtake.contracts import CoreContract
from coldtake.failures import ProviderFailure
from coldtake.routing import Plan

from .conftest import PACKET
from .fakes import memo
from .helpers import P, S, Scripted, judge_reply, spy_rig

PANEL = {**PACKET, "mode": "panel"}
SEALED = {**PACKET, "mode": "alternate", "sealed_answer": "my sealed answer"}
JUDGE = "COLDTAKE_ROLE: judge"


def _run(rig, packet=PANEL, request_id="req-1"):
    rig.engine.submit(request_id, packet, lead_family="anthropic")
    return rig.engine.wait(request_id, 30)


def _families(out):
    return [m["family"] for m in out["result"]["memos"]]


# -- G5/M1: escalation and replacement pools -------------------------------------------


def test_escalation_skips_a_family_that_is_already_seated(make_rig):
    spy = Scripted()
    plan = Plan(
        slots=((S("a", "f1"),), (S("b", "f2"),)),
        escalation=((S("a-again", "f1"),), (S("c", "f3"),)),
        escalate=lambda memos: True,
        chain=(S("judge", "fj"),),
    )  # fmt: skip
    rig, _ = spy_rig(make_rig, lambda ctx: plan, spy)
    out = _run(rig)
    assert out["status"] == "complete"
    assert _families(out) == ["f1", "f2", "f3"]  # never f1 twice
    assert "a-again" not in spy.counts


def test_escalation_does_not_rerun_a_route_that_already_failed(make_rig):
    def behave(prompt, route, n):
        if route.model == "b":
            raise ProviderFailure("quota", "fallback", "limit")
        return memo(f"via {route.model}")

    spy = Scripted(behave)
    plan = Plan(
        slots=((S("a", "f1"),), (S("b", "f2"),), (S("d", "f4"),)),
        escalation=((S("b", "f2"),), (S("e", "f3"),)),
        escalate=lambda memos: True,
        chain=(S("judge", "fj"),),
    )  # fmt: skip
    rig, _ = spy_rig(make_rig, lambda ctx: plan, spy)
    out = _run(rig)
    assert out["status"] == "complete"
    assert sorted(_families(out)) == ["f1", "f3", "f4"]
    assert spy.counts["b"] == 1  # the one failed attempt; escalation did not retry it


def test_a_pool_with_nothing_eligible_adds_no_member(make_rig):
    spy = Scripted()
    plan = Plan(
        slots=((S("a", "f1"),), (S("b", "f2"),)),
        escalation=((S("a2", "f1"),), (S("b2", "f2"),)),
        escalate=lambda memos: True,
        chain=(S("judge", "fj"),),
    )  # fmt: skip
    rig, _ = spy_rig(make_rig, lambda ctx: plan, spy)
    out = _run(rig)
    assert out["status"] == "complete" and _families(out) == ["f1", "f2"]
    assert "a2" not in spy.counts and "b2" not in spy.counts


def test_a_replacement_never_seats_a_seated_family_or_a_failed_route(make_rig):
    def behave(prompt, route, n):
        if route.model == "lost":
            raise ProviderFailure("quota", "fallback", "limit")
        return memo(f"via {route.model}")

    spy = Scripted(behave)
    plan = Plan(
        slots=((S("a", "f1"),), (S("lost", "f2"),), (S("c", "f3"),)),
        reserve=((S("lost", "f2"),), (S("a-again", "f1"),), (S("r", "f5"),)),
        chain=(S("judge", "fj"),),
    )  # fmt: skip
    rig, _ = spy_rig(make_rig, lambda ctx: plan, spy)
    out = _run(rig)
    assert out["status"] == "complete" and sorted(_families(out)) == ["f1", "f3", "f5"]
    assert spy.counts["lost"] == 1 and "a-again" not in spy.counts


def test_the_lead_family_may_still_be_seated_once_as_the_labelled_last_member(make_rig):
    spy = Scripted()
    plan = Plan(
        slots=((S("a", "f1"),), (S("b", "f2"),)),
        escalation=((S("own", "anthropic"),), (S("own2", "anthropic"),)),
        escalate=lambda memos: True,
        chain=(S("judge", "fj"),),
    )  # fmt: skip
    rig, _ = spy_rig(make_rig, lambda ctx: plan, spy)
    out = _run(rig)
    memos = out["result"]["memos"]
    assert [m["family"] for m in memos] == ["f1", "f2", "anthropic"]
    assert [m["same_family"] for m in memos] == [False, False, True]
    assert "own2" not in spy.counts  # a second lead-family member would be a duplicate


# -- L1: the judge repair asks for the judge schema ----------------------------------------


class AdvisorRepairIsMemoOnly(CoreContract):
    def repair_prompt(self, prompt, prior_raw, error):
        return f"{prompt}\nMEMO-REPAIR: return a memo with framing and recommendation"


def _judge_repair_rig(make_rig, contract=None):
    def behave(prompt, route, n):
        if JUDGE in prompt:
            if "REPAIR" not in prompt:
                return {"positions": "not valid"}
            return judge_reply(prompt.split("\nREPAIR")[0])
        return memo(f"via {route.model}")

    spy = Scripted(behave)
    plan = Plan(slots=((S("a", "f1"),), (S("b", "f2"),)), chain=(S("judge", "fj"),))
    rig, _ = spy_rig(make_rig, lambda ctx: plan, spy)
    if contract is not None:
        rig.engine.register_contract(contract(rig.config))
    return rig, spy


def test_the_judge_repair_asks_for_the_comparison_schema_not_a_memo(make_rig):
    rig, spy = _judge_repair_rig(make_rig, AdvisorRepairIsMemoOnly)
    out = _run(rig)
    assert out["status"] == "complete" and out["result"]["comparison"]["status"] != "unavailable"
    first, repair = spy.calls("judge")
    assert "REPAIR" not in first and "comparison schema" in repair
    assert "MEMO-REPAIR" not in repair and "memo with framing" not in repair
    # and the advisor repair still uses the contract's own hook
    assert "VALIDATION ERROR TO FIX" in repair


def test_the_advisor_repair_is_unchanged(make_rig):
    def behave(prompt, route, n):
        if JUDGE in prompt:
            return judge_reply(prompt)
        return {"nonsense": 1} if "MEMO-REPAIR" not in prompt else memo("fixed")

    spy = Scripted(behave)
    plan = Plan(slots=((S("a", "f1"),), (S("b", "f2"),)), chain=(S("judge", "fj"),))
    rig, _ = spy_rig(make_rig, lambda ctx: plan, spy)
    rig.engine.register_contract(AdvisorRepairIsMemoOnly(rig.config))
    out = _run(rig)
    assert out["status"] == "complete"
    advisor = spy.calls("advisor")
    assert len(advisor) == 4 and sum("MEMO-REPAIR" in p for p in advisor) == 2


def test_a_contract_can_replace_the_judge_repair_prompt(make_rig):
    class Custom(CoreContract):
        def judge_repair_prompt(self, prompt, prior_raw, error):
            return f"{prompt}\nREPAIR-JUDGE-CUSTOM {error[:20]}"

    rig, spy = _judge_repair_rig(make_rig, Custom)
    out = _run(rig)
    assert out["status"] == "complete"
    assert "REPAIR-JUDGE-CUSTOM" in spy.calls("judge")[1]


def test_a_paid_judge_repair_is_bounded_with_the_judge_framing(make_rig):
    rig, _ = spy_rig(make_rig, lambda ctx: Plan(chain=(S("m"),)))
    handler = rig.engine.contracts["coldtake_v1"]
    route = P("m", max_output_tokens=500)
    prompt = "x" * 100
    judge = len(handler.judge_repair_prompt(prompt, "", "x" * 800).encode()) - 100
    assert rig.engine._repair_bytes(handler, prompt, route, "judge") == judge + 2000
    assert judge != len(handler.repair_prompt(prompt, "", "x" * 800).encode()) - 100


# -- L2: the judge is planned apart from the advisors --------------------------------------


def _sealed_plan(**plan):
    return lambda ctx: Plan(preflight_ladder_budget=True, prune_paid_if_unaffordable=True, **plan)


def test_a_sealed_answer_no_longer_removes_the_paid_advisor_fallback(make_rig):
    spy = Scripted()
    spy.cost = 0.2  # one paid attempt 0.2, with its repair 0.4
    plan = _sealed_plan(chain=(S("sub", "fs"), P("paid", "fp")))
    rig, _ = spy_rig(make_rig, plan, spy, budget={"per_request_usd": 0.5, "daily_usd": 5})
    rig.engine.submit("req-1", SEALED, lead_family="anthropic")
    rig.engine.wait("req-1", 30)
    view = rig.engine.run_view("req-1")
    assert [r["model"] for r in view.plan["chain"]] == ["sub", "paid"]  # nothing dropped here
    assert [r["model"] for r in view.plan["judge"]] == ["sub"]  # the paid judge did not fit
    assert [r["model"] for r in view.plan["judge_dropped"]] == ["paid"]


def test_an_unaffordable_judge_leaves_the_advisor_paid_route_and_the_comparison_unavailable(
    make_rig,
):
    spy = Scripted()
    spy.cost = 0.2
    plan = _sealed_plan(chain=(P("paid", "fp"),))
    rig, _ = spy_rig(make_rig, plan, spy, budget={"per_request_usd": 0.5, "daily_usd": 5})
    out = _run(rig, SEALED)
    assert out["status"] == "complete"
    assert [m["model"] for m in out["result"]["memos"]] == ["paid"]  # the advisor ran paid
    comparison = out["result"]["comparison"]
    assert comparison["status"] == "unavailable" and comparison["cause"] == "judge_unaffordable"
    assert spy.calls("judge") == []
    view = rig.engine.run_view("req-1")
    assert [r["model"] for r in view.plan["judge_dropped"]] == ["paid"]
    assert view.plan["chain"] and view.plan["judge"] == []


def test_a_paid_judge_that_fits_beside_the_advisors_is_kept(make_rig):
    spy = Scripted()
    spy.cost = 0.2
    plan = _sealed_plan(chain=(S("sub", "fs"), P("paid", "fp")))
    rig, _ = spy_rig(make_rig, plan, spy, budget={"per_request_usd": 1.0, "daily_usd": 5})
    out = _run(rig, SEALED)
    assert out["status"] == "complete"
    view = rig.engine.run_view("req-1")
    assert [r["model"] for r in view.plan["judge"]] == ["sub", "paid"]
    assert view.plan["judge_dropped"] == []


def test_a_resolver_can_name_its_own_judge_ladder(make_rig):
    spy = Scripted()
    plan = lambda ctx: Plan(chain=(S("adv", "fa"),), judge=(S("my-judge", "fj"),))  # noqa: E731
    rig, _ = spy_rig(make_rig, plan, spy)
    out = _run(rig, SEALED)
    assert (
        out["status"] == "complete" and out["result"]["comparison"]["judge"]["model"] == "my-judge"
    )
    assert [m["model"] for m in out["result"]["memos"]] == ["adv"]


def test_without_a_budget_check_the_judge_ladder_is_the_chain(make_rig):
    spy = Scripted()
    rig, _ = spy_rig(make_rig, lambda ctx: Plan(chain=(S("m", "f1"),)), spy)
    out = _run(rig, SEALED)
    assert out["result"]["comparison"]["judge"]["model"] == "m"
    view = rig.engine.run_view("req-1")
    assert view.plan["judge"] == view.plan["chain"] and view.plan["judge_dropped"] == []


def test_an_advisor_ladder_that_cannot_be_afforded_is_still_pruned_to_subscription(make_rig):
    spy = Scripted()
    spy.cost = 0.4  # the paid advisor alone is 0.8 against a 0.5 cap
    plan = _sealed_plan(chain=(S("sub", "fs"), P("paid", "fp")))
    rig, _ = spy_rig(make_rig, plan, spy, budget={"per_request_usd": 0.5, "daily_usd": 5})
    out = _run(rig, SEALED)
    assert out["status"] == "complete"
    view = rig.engine.run_view("req-1")
    assert [r["model"] for r in view.plan["chain"]] == ["sub"]


def test_prune_only_in_a_panel_leaves_the_paid_judge_alone(make_rig):
    spy = Scripted()
    spy.cost = 0.4  # a paid judge would be 0.8 against the 0.5 cap
    plan = lambda ctx: Plan(  # noqa: E731
        slots=((S("a", "f1"),), (S("b", "f2"),)),
        chain=(P("judge", "fj"),),
        prune_paid_if_unaffordable=True,
    )
    rig, _ = spy_rig(make_rig, plan, spy, budget={"per_request_usd": 0.5, "daily_usd": 5})
    rig.engine.submit("req-1", PANEL, lead_family="anthropic")
    rig.engine.wait("req-1", 30)
    view = rig.engine.run_view("req-1")
    assert [r["model"] for r in view.plan["judge"]] == ["judge"]
    assert view.plan["judge_dropped"] == []  # only preflight_ladder_budget plans the judge


def test_a_replacement_pool_that_filters_to_nothing_adds_no_member(make_rig):
    def behave(prompt, route, n):
        if route.model == "lost":
            raise ProviderFailure("quota", "fallback", "limit")
        return memo(f"via {route.model}")

    spy = Scripted(behave)
    plan = Plan(
        slots=((S("a", "f1"),), (S("lost", "f2"),), (S("c", "f3"),)),
        reserve=((S("lost", "f2"),), (S("a-again", "f1"),), (S("c-again", "f3"),)),
        chain=(S("judge", "fj"),),
    )  # fmt: skip
    rig, _ = spy_rig(make_rig, lambda ctx: plan, spy)
    out = _run(rig)
    assert out["status"] == "complete" and sorted(_families(out)) == ["f1", "f3"]
    assert {m: spy.counts[m] for m in ("a", "lost", "c")} == {"a": 1, "lost": 1, "c": 1}
    assert "a-again" not in spy.counts and "c-again" not in spy.counts
