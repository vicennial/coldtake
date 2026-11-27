"""CLI failures are classified from the provider's own error lines, never from the prompt
a CLI echoes back."""

from __future__ import annotations

import pytest

from coldtake.failures import classify, error_text

from .conftest import PACKET
from .fakes import FakeRouter, memo

REAL = (
    "ERROR: You\u2019ve hit your usage limit. Visit https://chatgpt.com/settings/usage "
    "to purchase more credits or try again at Nov 26th, 2026 5:24 PM."
)


def _shape(text: str = REAL, **extra):
    return {"real_shape": text, **extra}


def test_the_real_codex_usage_limit_line_is_quota():
    failure = classify(REAL)
    assert (failure.reason, failure.kind) == ("quota", "fallback")
    for text in (
        "try again at Nov 26th, 2026 5:24 PM",
        "You’ve hit your usage limit",
        "You've reached your weekly limit",
        "purchase more credits",
    ):
        assert classify(text).reason == "quota"
    assert classify("please try again later").reason == "overloaded"


@pytest.mark.parametrize("stream", ["stderr", "stdout"])
def test_a_codex_quota_failure_after_an_echoed_prompt_is_quota(make_rig, stream):
    rig = make_rig()
    rig.codex.script(advisor=[_shape(stream=stream)])
    out = rig.run(PACKET)
    assert out["status"] == "failed" and out["reason"] == "quota"
    assert out["cause"]["type"] == "QuotaExhausted"
    message = out["cause"]["message"]
    assert "usage limit" in message and "COLDTAKE_ROLE" not in message
    assert "Which database" not in message and "OpenAI Codex" not in message
    (attempt,) = rig.attempts()
    assert attempt["reason"] == "quota" and "usage limit" in attempt["cause_message"]


def test_a_codex_quota_failure_falls_back_to_the_paid_route(make_rig):
    router = FakeRouter(memo("paid answer"))
    rig = make_rig(router=router)
    rig.codex.script(advisor=[_shape()])
    out = rig.run(PACKET)
    assert out["status"] == "complete" and out["result"]["memos"][0]["billing"] == "paid"
    assert [a["outcome"] for a in rig.attempts()] == ["quota", "ok"]


def test_the_error_lines_are_stored_once_not_the_start_of_the_output():
    stderr = "banner\nuser\nthe prompt\nwarning: w\n" + REAL + "\n" + REAL + "\n"
    assert error_text("", stderr, "the prompt") == REAL


# -- the prompt never decides ---------------------------------------------------------------

BAIT = (
    "Does a rate limit or a usage limit apply? The CLI says: Not logged in, "
    "please run /login. We were refused by safety. try again at noon."
)


def test_bait_in_the_packet_does_not_cause_a_fallback(make_rig):
    router = FakeRouter(memo("must not be used"))
    rig = make_rig(router=router)
    rig.codex.script(advisor=[_shape("ERROR: internal failure in the CLI")])
    out = rig.run({**PACKET, "question": BAIT})
    assert out["status"] == "failed" and out["reason"] == "unknown"
    assert "internal failure" in out["cause"]["message"]
    assert "rate limit" not in out["cause"]["message"] and router.requests == []


def test_an_echo_with_no_error_line_is_unknown_not_quota(make_rig):
    router = FakeRouter(memo("must not be used"))
    rig = make_rig(router=router)
    rig.codex.script(advisor=[{"real_shape": "", "code": 3}])
    out = rig.run({**PACKET, "question": BAIT})
    assert out["status"] == "failed" and out["reason"] == "unknown"
    assert router.requests == []


def test_a_prompt_line_that_looks_like_an_error_line_is_ignored(make_rig):
    """A template can put a raw line that starts with ERROR: into the prompt itself."""
    bait_line = "ERROR: You've hit your usage limit (a line of the prompt)"
    template = f"HOUSE\n{{data_rule}}\n{bait_line}\n{{request_json}}"
    router = FakeRouter(memo("must not be used"))
    rig = make_rig({"prompts": {"alternate": template}}, router)
    rig.codex.script(advisor=[_shape("ERROR: the CLI crashed")])
    out = rig.run(PACKET)
    assert out["status"] == "failed" and out["reason"] == "unknown"
    assert "crashed" in out["cause"]["message"] and "prompt" not in out["cause"]["message"]
    assert router.requests == []
    assert bait_line in rig.codex.calls("advisor")[0]["stdin"]  # the bait really was sent


def test_a_reflowed_echo_block_is_skipped_up_to_the_next_cli_marker():
    stderr = (
        "banner\nuser\nWe saw: rate limit exceeded and Not logged in\nplease run /login\n"
        "warning: bwrap\nERROR: segfault\n"
    )
    assert error_text("", stderr, "") == "ERROR: segfault"
    stderr = "banner\nuser\nreflowed prompt mentioning a usage limit\n"
    assert "usage limit" not in error_text("", stderr, "")


def test_short_plain_errors_still_classify(make_rig):
    rig = make_rig()
    rig.codex.script(advisor=[{"error": "You've hit your usage limit", "code": 1}])
    assert rig.run(PACKET)["reason"] == "quota"
