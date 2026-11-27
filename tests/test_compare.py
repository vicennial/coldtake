"""Agreement map: deterministic options path and the judge path."""

from __future__ import annotations

import pytest

from coldtake.engine import InvalidRequest

from .conftest import OPTIONS, PACKET
from .fakes import FakeRouter, choice_memo, memo

PANEL = {**PACKET, "mode": "panel"}


def _panel_rig(make_rig, paid_choice=None, **router_memo):
    router = FakeRouter(paid_choice if paid_choice is not None else memo("paid view"))
    return make_rig({"panel": {"max_members": 3, "max_paid_members": 1}}, router)


def test_options_path_clusters_by_choice(make_rig):
    rig = _panel_rig(make_rig, choice_memo("lite"))
    rig.codex.script(advisor=[{"json": choice_memo("pg")}])
    out = rig.run({**PANEL, "options": OPTIONS})["result"]
    comparison = out["comparison"]
    assert comparison["method"] == "options" and comparison["status"] == "ok"
    assert sorted(c["choice"] for c in comparison["clusters"]) == ["lite", "pg"]
    assert comparison["material"] is True
    assert comparison["pairwise"][0]["relation"] == "conflict"
    assert not rig.codex.calls("judge")  # deterministic: no judge call


def test_options_path_same_choice_is_one_cluster_and_not_material(make_rig):
    rig = _panel_rig(make_rig, choice_memo("pg", "different wording entirely"))
    rig.codex.script(advisor=[{"json": choice_memo("pg", "other prose")}])
    comparison = rig.run({**PANEL, "options": OPTIONS})["result"]["comparison"]
    assert len(comparison["clusters"]) == 1 and comparison["material"] is False
    assert comparison["clusters"][0]["label"] == "Postgres"


def test_options_path_sealed_choice_comparison_per_memo(make_rig):
    rig = _panel_rig(make_rig, choice_memo("lite"))
    rig.codex.script(advisor=[{"json": choice_memo("pg")}])
    packet = {**PANEL, "options": OPTIONS, "sealed_choice": "pg"}
    sealed = rig.run(packet)["result"]["comparison"]["sealed"]
    assert sorted(sealed["per_memo"].values()) == ["agree", "conflict"]
    assert sealed["relation"] == "partial"


def test_options_alternate_with_sealed_choice(rig):
    rig.codex.script(advisor=[{"json": choice_memo("pg")}])
    sealed = rig.run({**PACKET, "options": OPTIONS, "sealed_choice": "pg"})["result"]["comparison"][
        "sealed"
    ]
    assert sealed["relation"] == "agree"


def test_none_choice_is_valid_with_a_reason(rig):
    rig.codex.script(advisor=[{"json": choice_memo("none", reason="neither fits")}])
    out = rig.run({**PACKET, "options": OPTIONS})
    assert out["result"]["memos"][0]["memo"]["choice"] == "none"


def test_unknown_option_id_gets_one_repair_then_fails_over(rig):
    rig.codex.script(advisor=[{"json": choice_memo("mongo")}, {"json": choice_memo("none", " ")}])
    out = rig.run({**PACKET, "options": OPTIONS})
    assert out["status"] == "failed" and out["reason"] == "schema_exhausted"
    assert len(rig.codex.calls("advisor")) == 2
    rig.codex.script(advisor=[{"json": choice_memo("mongo")}, {"json": choice_memo("lite")}])
    out = rig.run({**PACKET, "options": OPTIONS}, request_id="req-2")
    assert out["result"]["memos"][0]["memo"]["choice"] == "lite"


def test_provider_schema_pins_choice_to_the_option_ids(rig):
    rig.codex.script(advisor=[{"json": choice_memo("pg")}])
    rig.run({**PACKET, "options": OPTIONS})
    schema = rig.codex.calls("advisor")[0]["schema"]
    assert schema["properties"]["choice"]["enum"] == ["pg", "lite", "none"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


def test_judge_path_success_with_sealed_answer(rig):
    rig.codex.script(
        advisor=[{"json": memo("Choose SQLite for simplicity")}],
        judge=[{"judge_auto": {"sealed": "partial"}}],
    )
    out = rig.run({**PACKET, "sealed_answer": "Choose Postgres"})["result"]
    comparison = out["comparison"]
    assert comparison["status"] == "ok" and comparison["method"] == "judge"
    assert comparison["sealed"]["relation"] == "partial"
    assert comparison["sealed"]["per_memo"] == {"m1": "partial"}
    assert comparison["judge"]["family"] == "openai"
    judge_stdin = rig.codex.calls("judge")[0]["stdin"]
    assert "codex" not in judge_stdin and "gpt" not in judge_stdin  # model names hidden


def test_judge_path_panel_decides_materiality_not_the_wording(make_rig):
    # Different wording, but the judge says the same action: not material.
    rig = _panel_rig(make_rig, memo("Adopt PG, in full prose of a different length"))
    rig.codex.script(
        advisor=[{"json": memo("Use Postgres.")}], judge=[{"judge_auto": {"clusters": "one"}}]
    )
    comparison = rig.run(PANEL)["result"]["comparison"]
    assert comparison["status"] == "ok" and comparison["material"] is False
    assert len(comparison["clusters"]) == 1


def test_judge_path_panel_split_is_material(make_rig):
    rig = _panel_rig(make_rig)
    rig.codex.script(advisor=[{"json": memo("A")}], judge=[{"judge_auto": {"clusters": "each"}}])
    comparison = rig.run(PANEL)["result"]["comparison"]
    assert comparison["material"] is True and len(comparison["clusters"]) == 2
    assert comparison["pairwise"][0]["relation"] == "conflict"


@pytest.mark.parametrize(
    "judge",
    [
        [{"garbage": True}],
        [{"error": "You've hit your usage limit", "code": 1}],
        [{"error": "refused: safety", "code": 1}],
        [{"json": {"clusters": [["A"]], "positions": [], "pairwise": [], "sealed": None,
                   "contradictions": [], "material": False}}],
    ],
)  # fmt: skip
def test_judge_failure_is_unavailable_never_a_lexical_material(make_rig, judge):
    rig = _panel_rig(make_rig)
    rig.codex.script(advisor=[{"json": memo("A")}], judge=judge)
    out = rig.run(PANEL)
    assert out["status"] == "complete" and out["result"]["advisor_count"] == 2
    comparison = out["result"]["comparison"]
    assert comparison["status"] == "unavailable" and comparison["cause"]
    assert "material" not in comparison and "clusters" not in comparison


def test_heuristic_diffs_are_labelled_and_supplementary(make_rig):
    rig = _panel_rig(make_rig, memo("B", assumptions=["only the paid one assumes this"]))
    rig.codex.script(advisor=[{"json": memo("A")}], judge=[{"judge_auto": {}}])
    heuristic = rig.run(PANEL)["result"]["comparison"]["heuristic"]
    assert heuristic["labelled"] == "heuristic"
    assert heuristic["unique_assumptions"]


def test_single_advisor_without_sealed_has_nothing_to_compare(rig):
    rig.codex.script(advisor=[{"json": memo()}])
    comparison = rig.run(PACKET)["result"]["comparison"]
    assert comparison["status"] == "not_applicable"


def test_panel_needs_two_members_before_ack(make_rig):
    rig = make_rig()  # lead anthropic, no paid route, no same-family route
    rig.codex.script(advisor=[{"json": memo()}])
    with pytest.raises(InvalidRequest, match="at least two"):
        rig.engine.submit("req-1", PANEL, lead_family="anthropic")
    assert rig.engine.store.get_run("req-1") is None


def test_the_leads_own_family_is_the_last_panel_member_and_is_labelled(make_rig):
    rig = make_rig(same_family=True)
    rig.codex.script(advisor=[{"json": memo("x")}], judge=[{"judge_auto": {}}])
    rig.claude.script(advisor=[{"json": memo("y")}])
    out = rig.run(PANEL)["result"]
    assert [(m["family"], m["same_family"]) for m in out["memos"]] == [
        ("openai", False),
        ("anthropic", True),
    ]


def test_panel_reports_the_actual_family_count(make_rig):
    rig2 = make_rig(same_family=True)
    rig2.codex.script(advisor=[{"json": memo("x")}], judge=[{"judge_auto": {}}])
    rig2.claude.script(advisor=[{"json": memo("y")}])
    out = rig2.run(PANEL, request_id="req-2", lead="other")
    assert out["result"]["advisor_count"] == 2
    assert sorted(m["family"] for m in out["result"]["memos"]) == ["anthropic", "openai"]
    assert not any(m["same_family"] for m in out["result"]["memos"])
