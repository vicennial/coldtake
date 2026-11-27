"""Mode boundaries and idempotency."""

from __future__ import annotations

from pathlib import Path

import pytest

from coldtake.engine import InvalidRequest
from coldtake.schemas import Submission

from .conftest import OPTIONS, PACKET
from .fakes import memo


def test_review_requires_primary_answer_and_shows_it_to_the_advisor(rig):
    with pytest.raises(ValueError):
        Submission.model_validate({"mode": "review", "question": "q"})
    rig.codex.script(advisor=[{"json": memo("Looks wrong")}])
    out = rig.run(
        {"mode": "review", "question": "Is this right?", "primary_answer": "PRIMARY-TEXT-1"}
    )
    assert out["status"] == "complete"
    assert "PRIMARY-TEXT-1" in rig.codex.calls("advisor")[0]["stdin"]
    assert out["result"]["comparison"] is None


@pytest.mark.parametrize("mode", ["alternate", "panel"])
def test_blind_modes_reject_primary_answer(mode):
    with pytest.raises(ValueError, match="primary_answer"):
        Submission.model_validate({**PACKET, "mode": mode, "primary_answer": "x"})


def test_review_rejects_sealed_answer():
    with pytest.raises(ValueError, match="sealed"):
        Submission.model_validate(
            {"mode": "review", "question": "q", "primary_answer": "a", "sealed_answer": "b"}
        )


def test_sealed_choice_needs_options_and_a_known_id():
    with pytest.raises(ValueError, match="requires options"):
        Submission.model_validate({**PACKET, "sealed_choice": "pg"})
    with pytest.raises(ValueError, match="option id"):
        Submission.model_validate({**PACKET, "options": OPTIONS, "sealed_choice": "mongo"})
    Submission.model_validate({**PACKET, "options": OPTIONS, "sealed_choice": "none"})


def test_tool_mode_must_match_the_packet_mode(rig):
    with pytest.raises(InvalidRequest, match="mode"):
        rig.engine.submit(
            "r", {**PACKET, "mode": "panel"}, lead_family="anthropic", mode="alternate"
        )


def test_bad_request_ids_and_unknown_fields_are_rejected(rig):
    with pytest.raises(InvalidRequest):
        rig.engine.submit("../escape", PACKET, lead_family="anthropic")
    with pytest.raises(InvalidRequest):
        rig.engine.submit("ok", {**PACKET, "surprise": 1}, lead_family="anthropic")


def test_same_id_same_packet_is_one_job_and_one_provider_call(rig):
    rig.codex.script(advisor=[{"json": memo()}])
    first = rig.engine.submit("req-1", PACKET, lead_family="anthropic")
    again = rig.engine.submit("req-1", PACKET, lead_family="anthropic")
    assert first["request_id"] == again["request_id"] == "req-1"
    rig.engine.wait("req-1", 30)
    rig.engine.submit("req-1", PACKET, lead_family="anthropic")  # after completion too
    assert len(rig.codex.calls("advisor")) == 1
    assert rig.engine.result("req-1")["status"] == "complete"


def test_same_id_different_packet_is_rejected(rig):
    rig.codex.script(advisor=[{"json": memo()}])
    rig.run(PACKET)
    with pytest.raises(InvalidRequest, match="different packet"):
        rig.engine.submit("req-1", {**PACKET, "question": "something else"}, lead_family="x")


def test_a_terminal_id_stays_terminal(rig):
    rig.codex.script(advisor=[{"error": "content policy refused", "code": 1}])
    assert rig.run(PACKET)["status"] == "failed"
    rig.codex.script(advisor=[{"json": memo()}])
    assert rig.engine.submit("req-1", PACKET, lead_family="anthropic")["status"] == "failed"
    assert len(rig.codex.calls("advisor")) == 1  # only the first attempt ever ran


def test_namespace_scopes_request_ids(make_rig):
    rig = make_rig({"namespace": "teamx"})
    rig.codex.script(advisor=[{"json": memo()}])
    out = rig.run(PACKET)
    assert out["result"]["request_id"] == "req-1"
    assert rig.engine.store.get_run("teamx:req-1") is not None
    assert rig.engine.store.get_run("req-1") is None


def test_large_results_are_written_to_a_file_with_a_hash(rig):
    import hashlib

    big = memo("big", evidence=["x" * 9000] * 9)
    rig.codex.script(advisor=[{"json": big}])
    out = rig.run(PACKET)
    assert "result" not in out and out["result_bytes"] > 64_000
    data = Path(out["result_path"]).read_bytes()
    assert hashlib.sha256(data).hexdigest() == out["result_sha256"]
    assert oct(__import__("os").stat(out["result_path"]).st_mode & 0o777) == "0o600"
