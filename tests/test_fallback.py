"""Fallback only on classified safe failures; paid fallback is budget-gated."""

from __future__ import annotations

import httpx
import pytest

from coldtake.failures import classify

from .conftest import PACKET
from .fakes import FakeRouter, memo


@pytest.mark.parametrize(
    ("text", "reason", "kind"),
    [
        ("You've hit your usage limit", "quota", "fallback"),
        ("weekly limit reached", "quota", "fallback"),
        ("Authentication expired, login required", "auth_expired", "fallback"),
        ("refresh_token_reused", "auth_expired", "fallback"),
        ("Rate limit exceeded", "rate_limited", "fallback"),
        ("The model is overloaded", "overloaded", "fallback"),
        ("service unavailable", "unavailable", "fallback"),
        ("I cannot assist with that (safety)", "refusal", "terminal"),
        ("content policy violation", "refusal", "terminal"),
        ("weekly limit; request refused by safety system", "refusal", "terminal"),
        ("segfault in plugin", "unknown", "terminal"),
    ],
)
def test_classification(text, reason, kind):
    failure = classify(text)
    assert (failure.reason, failure.kind) == (reason, kind)


def test_quota_falls_back_to_the_paid_route(make_rig):
    router = FakeRouter(memo("paid answer"))
    rig = make_rig(router=router)
    rig.codex.script(advisor=[{"error": "You've hit your usage limit", "code": 1}])
    out = rig.run(PACKET)
    assert out["status"] == "complete"
    assert out["result"]["memos"][0]["billing"] == "paid"
    assert len(router.requests) == 1
    body = router.requests[0]
    assert body["provider"]["max_price"]["completion"] > 0
    assert "models" not in body and body["max_tokens"] > 0 and body["stream"] is False
    assert [a["outcome"] for a in rig.attempts()] == ["quota", "ok"]
    assert [a["fallback"] for a in rig.attempts()] == [0, 1]


def test_claude_error_envelope_is_classified(make_rig):
    rig = make_rig(same_family=True)
    rig.claude.script(advisor=[{"claude_error": "You've reached your usage limit"}])
    rig.codex.script(advisor=[{"json": memo("from codex")}])
    # lead "other": both subscription families qualify, anthropic first
    out = rig.run(PACKET, lead="other")
    assert out["result"]["memos"][0]["family"] == "openai"
    assert rig.attempts()[0]["reason"] == "quota"


def test_refusal_is_terminal_and_never_falls_back(make_rig):
    router = FakeRouter(memo())
    rig = make_rig(router=router)
    rig.codex.script(advisor=[{"error": "refused: safety policy", "code": 1}])
    out = rig.run(PACKET)
    assert out["status"] == "failed" and out["reason"] == "refusal"
    assert out["cause"]["type"] == "Refusal" and "safety" in out["cause"]["message"]
    assert router.requests == []


def test_unknown_failure_is_terminal(make_rig):
    router = FakeRouter(memo())
    rig = make_rig(router=router)
    rig.codex.script(advisor=[{"error": "boom", "code": 2}])
    out = rig.run(PACKET)
    assert out["status"] == "failed" and out["reason"] == "unknown"
    assert router.requests == []


def test_no_paid_route_without_an_api_key(make_rig):
    rig = make_rig()  # no router, so no OPENROUTER_API_KEY
    rig.codex.script(advisor=[{"error": "usage limit", "code": 1}])
    out = rig.run(PACKET)
    assert out["status"] == "failed" and out["reason"] == "quota"


def test_schema_exhaustion_after_repair_is_terminal_and_never_falls_back(make_rig):
    router = FakeRouter(memo("must not be used"))
    rig = make_rig(router=router)
    rig.codex.script(advisor=[{"garbage": True}, {"json": {"nonsense": 1}}])
    out = rig.run(PACKET)
    assert out["status"] == "failed" and out["reason"] == "schema_exhausted"
    assert len(rig.codex.calls("advisor")) == 2
    assert "REPAIR" in rig.codex.calls("advisor")[1]["stdin"]
    assert router.requests == []
    # the last invalid candidate is kept, labelled as incomplete advice
    assert out["partial_advice"]["label"].startswith("incomplete")
    assert "nonsense" in out["partial_advice"]["raw"]
    assert rig.engine.status("req-1")["partial_advice_available"] is True


def test_repair_succeeds_without_fallback(rig):
    rig.codex.script(advisor=[{"garbage": True}, {"json": memo("second try")}])
    out = rig.run(PACKET)
    assert out["status"] == "complete" and len(rig.codex.calls("advisor")) == 2


def test_missing_binary_is_a_safe_fallback(make_rig):
    router = FakeRouter(memo("paid"))
    rig = make_rig({"binaries": {"codex": "/nonexistent/codex"}}, router)
    out = rig.run(PACKET)  # codex unavailable at plan time, so paid is the only route
    assert out["status"] == "complete" and out["result"]["memos"][0]["billing"] == "paid"


def test_paid_rejection_before_acceptance_falls_back_and_releases(make_rig):
    router = FakeRouter(httpx.Response(429, text="slow down"), memo("second paid"))
    rig = make_rig(router=router)
    rig.codex.script(advisor=[{"error": "usage limit", "code": 1}])
    out = rig.run(PACKET)
    assert out["status"] == "complete"
    with rig.engine.store.connect() as db:
        statuses = [r["status"] for r in db.execute("SELECT status FROM ledger ORDER BY rowid")]
    assert statuses == ["released", "committed"]
