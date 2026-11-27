"""The sealed answer must not reach an advisor on ANY path that builds a prompt: repair
(default and custom), templates, escalation, reserve replacement, paid requests and
subclassed contracts. Each test also proves it was not vacuous."""

from __future__ import annotations

import json

import httpx

from coldtake.contracts import CoreContract
from coldtake.failures import ProviderFailure
from coldtake.routing import Plan

from .conftest import PACKET
from .fakes import FakeRouter, memo
from .helpers import S, Scripted, spy_rig

CANARY = "CANARY-5d1e-sealed-text"
SEALED = {**PACKET, "sealed_answer": f"We should pick SQLite. {CANARY}"}
PANEL = {**SEALED, "mode": "panel"}


def _advisor_stdin(rig):
    return [c["stdin"] for fake in (rig.claude, rig.codex) for c in fake.calls("advisor")]


def _judge_stdin(rig):
    return [c["stdin"] for fake in (rig.claude, rig.codex) for c in fake.calls("judge")]


def test_the_default_repair_prompt_never_carries_the_sealed_text(rig):
    rig.codex.script(
        advisor=[{"garbage": True}, {"json": memo("fixed")}], judge=[{"judge_auto": {}}]
    )
    out = rig.run(SEALED)
    assert out["status"] == "complete"
    stdin = _advisor_stdin(rig)
    assert len(stdin) == 2
    assert all(CANARY not in text for text in stdin)
    assert any(CANARY in text for text in _judge_stdin(rig))  # not vacuous


class CustomRepair(CoreContract):
    name = "custom_repair_v1"

    def repair_prompt(self, prompt, prior_raw, error):
        return f"{prompt}\nCUSTOM-REPAIR keep: {prior_raw}\nerror: {error}"


class Plain(CoreContract):
    name = "plain_sub_v1"


def test_a_custom_repair_hook_never_sees_the_sealed_text(rig):
    rig.engine.register_contract(CustomRepair(rig.config))
    rig.codex.script(
        advisor=[{"stdout": "half an answer"}, {"json": memo("fixed")}],
        judge=[{"judge_auto": {}}],
    )
    rig.engine.submit("req-1", SEALED, lead_family="anthropic", contract="custom_repair_v1")
    assert rig.engine.wait("req-1", 30)["status"] == "complete"
    stdin = _advisor_stdin(rig)
    assert len(stdin) == 2 and "CUSTOM-REPAIR" in stdin[1]
    assert all(CANARY not in text for text in stdin)
    assert any(CANARY in text for text in _judge_stdin(rig))


def test_a_core_contract_subclass_keeps_the_sealed_text_out_of_prompts(rig):
    rig.engine.register_contract(Plain(rig.config))
    rig.codex.script(advisor=[{"json": memo("plain")}], judge=[{"judge_auto": {}}])
    rig.engine.submit("req-1", SEALED, lead_family="anthropic", contract="plain_sub_v1")
    assert rig.engine.wait("req-1", 30)["status"] == "complete"
    assert _advisor_stdin(rig) and all(CANARY not in text for text in _advisor_stdin(rig))
    assert any(CANARY in text for text in _judge_stdin(rig))


def test_a_configured_template_and_its_repair_never_carry_the_sealed_text(make_rig):
    rig = make_rig({"prompts": {"alternate": "HOUSE RULES\n{data_rule}\n{request_json}"}})
    rig.codex.script(
        advisor=[{"garbage": True}, {"json": memo("fixed")}], judge=[{"judge_auto": {}}]
    )
    assert rig.run(SEALED)["status"] == "complete"
    stdin = _advisor_stdin(rig)
    assert len(stdin) == 2 and all("HOUSE RULES" in text for text in stdin)
    assert all(CANARY not in text for text in stdin)


def test_escalation_members_never_see_the_sealed_text(make_rig):
    plan = Plan(
        chain=(S("judge", "fj"),),
        slots=((S("a", "f1"),), (S("b", "f2"),)),
        escalation=(S("e", "f3"),),
        escalate=lambda memos: True,
    )
    rig, spy = spy_rig(make_rig, lambda ctx: plan)
    out = rig.run(PANEL)["result"]
    assert out["advisor_count"] == 3 and "e" in spy.counts
    assert all(CANARY not in text for text in spy.calls("advisor"))
    assert any(CANARY in text for text in spy.calls("judge"))


def test_replacement_members_never_see_the_sealed_text(make_rig):
    def behave(prompt, route, n):
        if route.model == "lost":
            raise ProviderFailure("quota", "fallback", "limit")
        if "COLDTAKE_ROLE: judge" in prompt:
            from .helpers import judge_reply

            return judge_reply(prompt)
        return memo(f"via {route.model}")

    plan = Plan(
        chain=(S("judge", "fj"),),
        slots=((S("lost", "f1"),), (S("b", "f2"),)),
        reserve=(S("reserve", "f3"),),
    )
    rig, spy = spy_rig(make_rig, lambda ctx: plan, Scripted(behave))
    out = rig.run(PANEL)["result"]
    assert out["advisor_count"] == 2 and "reserve" in spy.counts
    assert all(CANARY not in text for text in spy.calls("advisor"))
    assert any(CANARY in text for text in spy.calls("judge"))


def test_a_paid_repair_request_never_carries_the_sealed_text(make_rig):
    bad = httpx.Response(
        200,
        json={
            "model": "m",
            "choices": [{"finish_reason": "stop", "message": {"content": "no json here"}}],
            "usage": {"cost": 0.01},
        },
    )
    router = FakeRouter(bad, httpx.Response(200, json=json.loads(_good(memo("fixed")))))
    rig = make_rig(router=router)
    rig.codex.script(
        advisor=[{"error": "You've hit your usage limit", "code": 1}], judge=[{"judge_auto": {}}]
    )
    out = rig.run(SEALED)
    assert out["status"] == "complete" and len(router.requests) >= 2
    assert all(CANARY not in json.dumps(body) for body in router.requests[:2])


def _good(data):
    return json.dumps(
        {
            "model": "m",
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(data)}}],
            "usage": {"cost": 0.01},
        }
    )
