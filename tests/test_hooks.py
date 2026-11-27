"""Generic hooks a layer on top of coldtake relies on: contracts, planners, comparison,
tool surface, result access, pricing detail, repair, operations."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from coldtake.budget import BudgetExceeded
from coldtake.contracts import CompareContext, Contract, CoreContract, Parsed
from coldtake.engine import InvalidRequest
from coldtake.providers import Completion, Route, worst_case_cost
from coldtake.routing import Plan
from coldtake.server import build_server

from .conftest import PACKET
from .fakes import FakeRouter, memo

QUOTA = [{"error": "You've hit your usage limit", "code": 1}]


# -- a second contract with its own packet, memo and prompt --------------------------------


class DecisionPacket(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision_id: str
    question: str
    mode: str = "alternate"


class StrictV2(Contract):
    name = "strict_v2"
    packet_model = DecisionPacket

    def parse(self, data, mode):
        packet = DecisionPacket.model_validate({**data, **({"mode": mode} if mode else {})})
        return Parsed(packet, identity=packet.model_dump())

    def mode(self, packet):
        return packet.mode

    def render_prompt(self, packet):
        return f"COLDTAKE_ROLE: advisor\nSTRICT-V2 {packet.decision_id}: {packet.question}"

    def memo_schema(self, packet):
        return {"type": "object", "properties": {"answer": {"type": "string"}}}

    def validate_memo(self, data, packet):
        if not isinstance(data.get("answer"), str):
            raise ValueError("answer must be a string")
        return {"answer": data["answer"]}

    def repair_prompt(self, prompt, prior_raw, error):
        return f"{prompt}\nREPAIR-V2 keep this content: {prior_raw}\nerror: {error}"

    def shape_result(self, run):
        return {"decision_id": run.packet.decision_id, "answers": [m["memo"] for m in run.memos]}

    def shape_status(self, run, view):
        return {**view, "phase": "custom"}


def _v2(rig, **attrs):
    contract = StrictV2()
    for key, value in attrs.items():
        setattr(contract, key, value)
    rig.engine.register_contract(contract)
    return contract


V2 = {"decision_id": "d-1", "question": "Pick one"}


def test_a_registered_contract_owns_packet_prompt_memo_and_shapes(rig):
    _v2(rig)
    rig.codex.script(advisor=[{"json": {"answer": "yes"}}])
    rig.engine.submit("c1", V2, lead_family="anthropic", contract="strict_v2")
    out = rig.engine.wait("c1", 30)
    assert out["status"] == "complete" and out["phase"] == "custom"
    assert out["result"] == {"decision_id": "d-1", "answers": [{"answer": "yes"}]}
    assert out["contract"] == "strict_v2"
    assert "STRICT-V2 d-1" in rig.codex.calls("advisor")[0]["stdin"]
    assert rig.engine.store.get_run("c1")["contract"] == "strict_v2"
    view = rig.engine.run_view("c1")
    assert view.contract == "strict_v2" and view.memos[0]["memo"] == {"answer": "yes"}
    assert view.attempts and view.row["status"] == "complete"


def test_contract_is_part_of_the_idempotency_identity(rig):
    _v2(rig)
    rig.codex.script(advisor=[{"json": {"answer": "yes"}}])
    rig.engine.submit("c1", V2, lead_family="anthropic", contract="strict_v2")
    rig.engine.submit("c1", V2, lead_family="anthropic", contract="strict_v2")  # same: fine
    with pytest.raises(InvalidRequest):
        rig.engine.submit("c1", PACKET, lead_family="anthropic")  # core contract, same id
    with pytest.raises(InvalidRequest, match="unknown contract"):
        rig.engine.submit("c2", V2, lead_family="anthropic", contract="nope")
    rig.engine.wait("c1", 30)


def test_repair_hook_receives_the_prior_reply_and_can_preserve_it(rig):
    _v2(rig)
    rig.codex.script(advisor=[{"json": {"answer": 5}}, {"json": {"answer": "fixed"}}])
    rig.engine.submit("c1", V2, lead_family="anthropic", contract="strict_v2")
    assert rig.engine.wait("c1", 30)["status"] == "complete"
    second = rig.codex.calls("advisor")[1]["stdin"]
    assert "REPAIR-V2 keep this content" in second and '"answer": 5' in second.replace(":5", ": 5")


def test_repair_gets_its_own_allowance_after_a_readable_reply(make_rig):
    rig = make_rig({"limits": {"attempt_seconds": 1, "job_seconds": 5}})
    _v2(rig, repair_seconds=10)
    rig.codex.script(advisor=[{"garbage": True}, {"sleep": 2, "then": {"json": {"answer": "ok"}}}])
    rig.engine.submit("c1", V2, lead_family="anthropic", contract="strict_v2")
    assert rig.engine.wait("c1", 30)["status"] == "complete"  # 2 s > attempt_seconds=1
    row = rig.engine.store.get_run("c1")
    assert row["deadline_at"] - row["created_at"] >= 5 + 10  # reserved in the job deadline


def test_schema_exhaustion_falls_back_only_when_the_contract_opts_in(make_rig):
    router = FakeRouter({"answer": "from paid"})
    rig = make_rig(router=router)
    _v2(rig, fallback_on_schema_exhausted=True)
    rig.codex.script(advisor=[{"garbage": True}])
    rig.engine.submit("c1", V2, lead_family="anthropic", contract="strict_v2")
    out = rig.engine.wait("c1", 30)
    assert out["status"] == "complete" and len(router.requests) == 1


def test_per_contract_deadline_is_reserved_on_the_run(rig):
    _v2(rig, deadline_seconds=77)
    rig.codex.script(advisor=[{"json": {"answer": "x"}}])
    rig.engine.submit("c1", V2, lead_family="anthropic", contract="strict_v2")
    rig.engine.wait("c1", 30)
    row = rig.engine.store.get_run("c1")
    assert row["deadline_at"] - row["created_at"] == pytest.approx(77, abs=0.01)


# -- planner ----------


class Spy:
    """A provider that sleeps, counts concurrency and can fail on named models."""

    name = "spy"

    def __init__(self):
        self.active = self.peak = 0
        self.calls: list[str] = []
        self.fail: dict[str, str] = {}
        self.lock = threading.Lock()
        self.seen_ctx: Any = None
        self.paid_cost = None

    def available(self):
        return True

    def complete(self, prompt, schema, route, timeout):
        from coldtake.failures import ProviderFailure

        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.calls.append(route.model)
        try:
            time.sleep(0.15)
            if route.model in self.fail:
                raise ProviderFailure(self.fail[route.model], "fallback", "spy failure")
            return Completion(memo(f"via {route.model}"), cost_usd=0.001)
        finally:
            with self.lock:
                self.active -= 1

    def worst_case_cost(self, route, prompt_bytes):
        return (
            self.paid_cost if self.paid_cost is not None else worst_case_cost(route, prompt_bytes)
        )


def _spy_rig(make_rig, planner, **extra):
    spy = Spy()
    holder = {"plan": planner}

    def resolver(ctx):
        spy.seen_ctx = ctx
        return holder["plan"](ctx)

    import tests.test_hooks as module

    module.RESOLVER = staticmethod(resolver)
    rig = make_rig({"routing": {"resolver": "tests.test_hooks:resolve"}, **extra})
    rig.engine.providers["spy"] = spy
    return rig, spy


RESOLVER = None


def resolve(ctx):
    return RESOLVER(ctx)  # type: ignore[misc]


def R(model, family, **kw):
    return Route("spy", family, model, **kw)


def test_planner_receives_the_packet_namespace_contract_and_prompt_size(make_rig):
    rig, spy = _spy_rig(make_rig, lambda ctx: Plan(chain=(R("m1", "fam"),)), namespace="nsx")
    rig.run({**PACKET, "stakes": "high", "reversibility": "irreversible"})
    ctx = spy.seen_ctx
    assert ctx.packet.stakes == "high" and ctx.packet.reversibility == "irreversible"
    assert ctx.namespace == "nsx" and ctx.contract == "coldtake_v1" and ctx.mode == "alternate"
    assert (
        ctx.prompt_bytes > 100 and ctx.lead_family == "anthropic" and ctx.config and ctx.providers
    )


def test_panel_member_ladders_replacement_pool_and_parallelism(make_rig):
    def plan(ctx):
        return Plan(
            slots=((R("a1", "fa"), R("a2", "fa")), (R("b1", "fb"),), (R("c1", "fc"),)),
            reserve=(R("r1", "fr"),),
            parallel=2,
        )

    rig, spy = _spy_rig(make_rig, plan)
    spy.fail = {"a1": "quota", "b1": "quota"}  # a1 -> continuity a2; b1 -> replaced by r1
    out = rig.run({**PACKET, "mode": "panel"})
    assert out["status"] == "complete"
    models = sorted(m["model"] for m in out["result"]["memos"])
    assert models == ["a2", "c1", "r1"]  # continuity inside a member; replacement from the pool
    assert spy.peak == 2  # parallel=2 with three members


def test_escalation_pool_runs_only_when_the_predicate_says_so(make_rig):
    def plan(ctx):
        return Plan(
            slots=((R("a", "fa"),), (R("b", "fb"),)),
            escalation=(R("e", "fe"),),
            escalate=lambda memos: ctx.packet.stakes == "high" and len(memos) == 2,
            parallel=2,
        )

    rig, spy = _spy_rig(make_rig, plan)
    rig.run({**PACKET, "mode": "panel", "stakes": "high"}, "esc")
    assert "e" in spy.calls and len(spy.calls) == 3
    spy.calls.clear()
    rig.run({**PACKET, "mode": "panel", "stakes": "low"}, "noesc")
    assert "e" not in spy.calls


def test_pre_ack_ladder_budget_check_and_pruning(make_rig):
    paid = Route("spy", "fp", "paid-m", None, "paid", 1.0, 1.0, 1000)

    def plan(ctx, **flags):
        return Plan(chain=(R("sub", "fa"), paid), slots=((R("a", "fa"),), (paid,)), **flags)

    rig, spy = _spy_rig(
        make_rig,
        lambda c: plan(c, preflight_ladder_budget=True),
        budget={"per_request_usd": 0.001, "daily_usd": 1},
    )
    spy.paid_cost = 0.01  # initial + repair of the paid route = 0.02 > 0.001
    with pytest.raises(BudgetExceeded):
        rig.engine.submit("p1", PACKET, lead_family="anthropic")
    assert rig.engine.store.get_run("p1") is None  # refused before anything was stored
    with pytest.raises(BudgetExceeded):
        rig.engine.submit("p2", {**PACKET, "mode": "panel"}, lead_family="anthropic")
    # single-advisor modes can instead drop the paid tail
    import tests.test_hooks as module

    module.RESOLVER = staticmethod(lambda c: plan(c, prune_paid_if_unaffordable=True))
    out = rig.run(PACKET, "p3")
    assert out["status"] == "complete" and spy.calls == ["sub"]


# -- comparison ----------


def _named_core(name, **kw):
    contract = CoreContract(**kw)
    contract.name = name
    return contract


def test_compare_none_and_callable_make_no_judge_call(make_rig):
    router = FakeRouter(memo("paid"))
    rig = make_rig({"panel": {"max_members": 3, "max_paid_members": 1}}, router)
    rig.engine.register_contract(_named_core("core_none", compare="none"))

    def lexical(cx: CompareContext):
        return {"status": "ok", "method": "custom", "n": len(cx.memos), "sealed": None}

    rig.engine.register_contract(_named_core("core_custom", compare=lexical))
    rig.codex.script(advisor=[{"json": memo("A")}], judge=[{"judge_auto": {}}])
    panel = {**PACKET, "mode": "panel", "sealed_answer": "mine"}
    for contract, expected in (("core_none", None), ("core_custom", "custom")):
        rig.engine.submit(contract, panel, lead_family="anthropic", contract=contract)
        out = rig.engine.wait(contract, 30)["result"]
        comparison = out["comparison"]
        assert (comparison and comparison["method"]) == expected
    assert not rig.codex.calls("judge")
    # the default contract still judges
    out = rig.run(panel, "default-judge")
    assert out["result"]["comparison"]["method"] == "judge"
    assert rig.codex.calls("judge")


# -- tool surface ----------


def test_builtin_tools_can_be_hidden_and_server_settings_applied(make_rig):
    rig = make_rig(
        {
            "tools": {"builtins": False},
            "server": {"name": "custom-name", "stateless_http": True},
            "limits": {"max_request_bytes": 123456},
        }
    )
    server = build_server(rig.config, rig.engine)
    assert not [t for t in asyncio.run(server.list_tools()) if t.name.startswith("coldtake_")]
    assert server.name == "custom-name"
    assert server.settings.stateless_http is True
    assert server.settings.max_request_body_size == 123456


# -- result access and record sources ----------


def test_load_result_is_hash_verified_for_inline_and_file_results(rig):
    rig.codex.script(advisor=[{"json": memo("small")}])
    rig.run(PACKET, "small")
    assert rig.engine.load_result("small")["memos"][0]["memo"]["recommendation"] == "small"
    rig.codex.script(advisor=[{"json": memo("big", evidence=["x" * 9000] * 9)}])
    out = rig.run(PACKET, "big")
    assert "result" not in out
    assert rig.engine.load_result("big")["advisor_count"] == 1
    path = out["result_path"]
    with open(path, "a") as handle:
        handle.write(" ")  # tamper
    with pytest.raises(ValueError, match="SHA-256"):
        rig.engine.load_result("big")
    with pytest.raises(LookupError):
        rig.engine.load_result("never-existed")


def test_record_sources_answer_for_ids_core_does_not_own(rig):
    class Legacy:
        def status(self, request_id):
            return (
                {"request_id": request_id, "status": "complete", "legacy": True}
                if request_id == "old-1"
                else None
            )

        def result(self, request_id):
            return {"request_id": request_id, "legacy_result": 1} if request_id == "old-1" else None

        def reserved(self, request_id):
            return request_id.startswith("branch-")

    rig.engine.register_record_source(Legacy())
    assert rig.engine.status("old-1")["legacy"] is True
    assert rig.engine.result("old-1") == {"request_id": "old-1", "legacy_result": 1}
    assert rig.engine.status("missing")["status"] == "not_found"
    with pytest.raises(InvalidRequest, match="reserved"):
        rig.engine.submit("branch-1", PACKET, lead_family="anthropic")


# -- paid pricing detail ----------


def test_paid_model_pricing_detail(make_rig):
    models = [
        {
            "model": "x/m", "family": "fx", "prompt_usd_per_mtok": 1.0,
            "completion_usd_per_mtok": 1.0, "max_output_tokens": 1000,
            "input_reserve_tokens": 32000, "endpoints_only": ["ep/a", "ep/b"],
        }
    ]  # fmt: skip
    router = FakeRouter(memo("paid"))
    rig = make_rig({"openrouter": {"models": models}}, router)
    rig.codex.script(advisor=QUOTA)
    rig.run(PACKET)
    assert router.requests[0]["provider"]["only"] == ["ep/a", "ep/b"]
    route = rig.config.openrouter.models[0]
    assert route.input_reserve_tokens == 32000
    paid = Route.paid(route)
    assert worst_case_cost(paid, 0) == pytest.approx((32000 * 1.0 + 1000 * 1.0) / 1e6, rel=1e-6)


def test_context_fit_skips_a_paid_route_that_cannot_hold_the_prompt(make_rig):
    models = [
        {
            "model": "x/m", "family": "fx", "prompt_usd_per_mtok": 1.0,
            "completion_usd_per_mtok": 1.0, "max_output_tokens": 1000, "context_tokens": 1500,
        }
    ]  # fmt: skip
    router = FakeRouter(memo("paid"))
    rig = make_rig({"openrouter": {"models": models}}, router)
    rig.codex.script(advisor=QUOTA)
    out = rig.run(PACKET)
    assert out["status"] == "failed" and out["reason"] == "context_rejected"
    assert router.requests == []


def test_paid_usage_tokens_are_stored_with_the_attempt(make_rig):
    router = FakeRouter(memo("paid"))
    rig = make_rig(router=router)
    rig.codex.script(advisor=QUOTA)
    rig.run(PACKET)
    paid = [a for a in rig.attempts() if a["billing"] == "paid"][0]
    assert (paid["input_tokens"], paid["output_tokens"]) == (10, 10)


def test_external_daily_spend_counts_against_the_daily_cap(make_rig):
    router = FakeRouter(memo("paid"))
    rig = make_rig({"budget": {"per_request_usd": 1.0, "daily_usd": 5.0}}, router)
    rig.engine.ledger.external_daily_spend = lambda day: 4.999
    rig.codex.script(advisor=QUOTA)
    out = rig.run(PACKET)
    assert out["status"] == "failed" and out["reason"] == "budget_exceeded"
    assert rig.engine.ledger.spent_today() == pytest.approx(4.999)
    assert router.requests == []


# -- operations ----------


def test_accepting_new_jobs_switch_blocks_only_new_jobs(make_rig):
    rig = make_rig()
    rig.codex.script(advisor=[{"json": memo()}])
    rig.run(PACKET, "before")
    rig.engine.accepting_new_jobs = False
    with pytest.raises(InvalidRequest, match="not being accepted"):
        rig.engine.submit("after", PACKET, lead_family="anthropic")
    assert rig.engine.submit("before", PACKET, lead_family="anthropic")["status"] == "complete"
    assert make_rig({"accepting_new_jobs": False}).engine.accepting_new_jobs is False


def test_drain_none_waits_without_a_bound_and_wait_none_blocks_until_terminal(make_rig):
    rig = make_rig()
    rig.codex.script(advisor=[{"sleep": 1.5, "then": {"json": memo()}}])
    rig.engine.submit("slow", PACKET, lead_family="anthropic")
    assert rig.engine.drain(0.1) is False  # a bounded drain gives up
    assert rig.engine.drain(None) is True  # an unbounded one waits for the job
    assert rig.engine.status("slow")["status"] == "complete"
    assert rig.engine.wait("slow", None)["status"] == "complete"
    assert make_rig({"limits": {"drain_seconds": None}}).config.limits.drain_seconds is None
