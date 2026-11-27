"""The sealed answer is stored and hashed, and never reaches an advisor."""

from __future__ import annotations

import hashlib
import json

import pytest

from coldtake.prompts import render_advisor_prompt
from coldtake.schemas import Packet, Submission

from .conftest import OPTIONS, PACKET
from .fakes import FakeRouter, choice_memo, memo

CANARY = "CANARY-7f3a9c-sealed-text"


def _all_stdin(rig, role=None):
    return [c["stdin"] for fake in (rig.claude, rig.codex) for c in fake.calls(role)]


def test_packet_type_has_no_sealed_fields():
    assert not [f for f in Packet.model_fields if "sealed" in f]
    assert {"sealed_answer", "sealed_choice"} <= set(Submission.model_fields)


def test_canary_never_in_advisor_stdin_judge_path(rig):
    rig.codex.script(
        advisor=[{"json": memo("Use Postgres")}], judge=[{"judge_auto": {"sealed": "agree"}}]
    )
    out = rig.run({**PACKET, "sealed_answer": f"We should pick SQLite. {CANARY}"})
    assert out["status"] == "complete"
    assert (
        out["result"]["sealed_sha256"]
        == hashlib.sha256(f"We should pick SQLite. {CANARY}".encode()).hexdigest()
    )
    advisor_stdin = _all_stdin(rig, "advisor")
    assert advisor_stdin and all(CANARY not in text for text in advisor_stdin)
    # The test is not vacuous: the judge is the one place the sealed text is rendered.
    judge_stdin = _all_stdin(rig, "judge")
    assert judge_stdin and all(CANARY in text for text in judge_stdin)
    # And it is not in the stored advisor-visible packet, nor the result.
    row = rig.engine.store.get_run("req-1")
    assert CANARY not in row["packet_json"]
    assert row["sealed_answer"].endswith(CANARY)
    assert CANARY not in json.dumps(out["result"])


def test_canary_never_reaches_any_provider_on_options_path(rig):
    rig.codex.script(advisor=[{"json": choice_memo("pg")}])
    out = rig.run({**PACKET, "options": OPTIONS, "sealed_answer": CANARY, "sealed_choice": "lite"})
    assert out["result"]["comparison"]["method"] == "options"
    assert all(CANARY not in text for text in _all_stdin(rig))
    assert not rig.codex.calls("judge") and not rig.claude.calls("judge")


def test_canary_never_in_paid_request_body(make_rig):
    router = FakeRouter({**memo("paid view")})
    rig = make_rig({"panel": {"max_members": 3, "max_paid_members": 1}}, router)
    rig.codex.script(
        advisor=[{"json": memo("codex view")}], judge=[{"judge_auto": {"sealed": "partial"}}]
    )
    out = rig.run({**PACKET, "mode": "panel", "sealed_answer": CANARY})
    assert out["status"] == "complete" and out["result"]["advisor_count"] == 2
    assert router.requests, "the paid advisor must have been called"
    assert all(CANARY not in json.dumps(body) for body in router.requests)
    assert all(CANARY not in text for text in _all_stdin(rig, "advisor"))


def test_prompt_is_built_from_the_packet_only():
    sub = Submission.model_validate({**PACKET, "sealed_answer": CANARY})
    packet, sealed = sub.split()
    assert CANARY not in render_advisor_prompt(packet)
    assert sealed.answer == CANARY


def test_overlap_warning_is_returned_in_the_ack(rig):
    sealed = "we should adopt postgres because it handles concurrent writers and migrations well"
    packet = {**PACKET, "question": f"Context: {sealed}. Which database?", "sealed_answer": sealed}
    rig.codex.script(advisor=[{"json": memo()}], judge=[{"judge_auto": {}}])
    ack = rig.engine.submit("req-w", packet, lead_family="anthropic")
    assert any(w.startswith("sealed_answer_overlap") for w in ack["warnings"])
    assert ack["sealed_sha256"]


@pytest.mark.parametrize(
    "text",
    ["My answer is Postgres.", "I think we should use SQLite", "the consensus is B",
     "The lead's recommendation was A"],
)  # fmt: skip
def test_contamination_is_rejected_in_blind_modes(text):
    with pytest.raises(ValueError):
        Submission.model_validate({**PACKET, "question": text})
    # review is not blind, so the same wording is fine there
    Submission.model_validate({**PACKET, "mode": "review", "question": text, "primary_answer": "x"})
