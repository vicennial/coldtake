"""Same-family tail, strict judge validation, label and member bounds, failed comparisons."""

from __future__ import annotations

import pytest

from coldtake.compare import judge_comparison, overall_relation
from coldtake.config import load_config
from coldtake.contracts import CoreContract
from coldtake.engine import InvalidRequest
from coldtake.prompts import LABELS, label_for
from coldtake.routing import Plan
from coldtake.schemas import JudgeOutput

from .conftest import PACKET
from .fakes import FakeRouter, memo
from .helpers import S, spy_rig

PANEL = {**PACKET, "mode": "panel"}
QUOTA = [{"error": "You've hit your usage limit", "code": 1}]


# -- M1: the lead's own family is the last resort ----------------------------------------


def test_the_leads_own_family_is_the_last_ladder_entry_and_is_labelled(make_rig):
    rig = make_rig(same_family=True)
    rig.codex.script(advisor=QUOTA)
    rig.claude.script(advisor=[{"json": memo("from my own family")}])
    out = rig.run(PACKET, lead="anthropic")
    (record,) = out["result"]["memos"]
    assert (record["family"], record["same_family"]) == ("anthropic", True)
    assert [(a["family"], a["outcome"]) for a in rig.attempts()] == [
        ("openai", "quota"),
        ("anthropic", "ok"),
    ]


def test_an_independent_family_is_never_replaced_by_the_leads_own(make_rig):
    rig = make_rig(same_family=True)
    rig.codex.script(advisor=[{"json": memo("independent")}])
    out = rig.run(PACKET, lead="anthropic")
    assert out["result"]["memos"][0]["same_family"] is False
    assert not rig.claude.calls()


def test_a_panel_that_would_have_one_member_is_refused_before_ack(make_rig):
    rig = make_rig(same_family=False)
    with pytest.raises(InvalidRequest, match="at least two"):
        rig.engine.submit("req-1", PANEL, lead_family="anthropic")
    assert rig.engine.store.get_run("req-1") is None


# -- M2: strict judge validation ---------------------------------------------------------


def _panel(make_rig, **judge):
    rig = make_rig({"panel": {"max_members": 3, "max_paid_members": 1}}, FakeRouter(memo("B")))
    rig.codex.script(advisor=[{"json": memo("A")}], judge=[{"judge_auto": judge}])
    return rig


@pytest.mark.parametrize(
    "defect",
    [
        {"drop_pair": True},
        {"dup_pair": True},
        {"drop_sealed_pair": True},
        {"repeat_label": True},
        {"drop_position": True},
    ],
)
def test_a_malformed_judge_output_never_becomes_a_comparison(make_rig, defect):
    rig = _panel(make_rig, sealed="agree", **defect)
    out = rig.run({**PANEL, "sealed_answer": "sealed text"})
    assert out["status"] == "complete" and out["result"]["advisor_count"] == 2
    comparison = out["result"]["comparison"]
    assert comparison["status"] == "unavailable" and "material" not in comparison
    # the single repair was used, then the comparison gave up
    assert len(rig.codex.calls("judge")) == 2
    assert "partial_advice" not in out and "partial_advice" not in out["result"]  # L8
    assert rig.engine.status("req-1")["partial_advice_available"] is False


def test_the_sealed_relation_is_recomputed_not_trusted(make_rig):
    rig = _panel(make_rig, sealed="agree", overall_sealed="conflict")
    out = rig.run({**PANEL, "sealed_answer": "sealed text"})
    sealed = out["result"]["comparison"]["sealed"]
    assert sealed["relation"] == "agree"  # the judge's own "conflict" is ignored
    assert set(sealed["per_memo"].values()) == {"agree"}


def _output(**override):
    base = {
        "positions": [{"label": "A", "stance": "x"}, {"label": "B", "stance": "y"}],
        "clusters": [["A", "B"]],
        "pairwise": [{"a": "A", "b": "B", "relation": "agree", "crux": "c"}],
        "sealed": None, "contradictions": [], "material": False,
    }  # fmt: skip
    return JudgeOutput.model_validate({**base, **override})


def test_judge_comparison_checks_every_structural_rule():
    memos = [{"id": "m1", "memo": memo()}, {"id": "m2", "memo": memo()}]
    order = [0, 1]
    ok = judge_comparison(_output(), order, memos, False)
    assert ok["status"] == "ok" and ok["material"] is False
    bad = [
        _output(positions=[{"label": "A", "stance": "x"}]),
        _output(positions=[{"label": "A", "stance": "x"}, {"label": "A", "stance": "y"}]),
        _output(pairwise=[]),
        _output(pairwise=[{"a": "A", "b": "A", "relation": "agree", "crux": "c"}]),
        _output(contradictions=[{"claim": "c", "labels": ["A", "A"]}]),
        _output(clusters=[["A"], ["A", "B"]]),
    ]
    for output in bad:
        with pytest.raises(ValueError):
            judge_comparison(output, order, memos, False)


def test_overall_relation():
    assert overall_relation(["agree", "agree"]) == "agree"
    assert overall_relation(["conflict", "conflict"]) == "conflict"
    assert overall_relation(["agree", "conflict"]) == "partial"


# -- M3: more labels, bounded members, failed compare ------------------------------------


def test_labels_run_past_three_and_skip_the_sealed_marker():
    assert len(LABELS) >= 9 and "S" not in LABELS
    assert [label_for(i) for i in range(4)] == ["A", "B", "C", "D"]


def test_a_panel_of_five_members_is_compared_with_five_labels(make_rig):
    plan = Plan(
        chain=(S("judge", "fj"),), slots=tuple((S(f"m{i}", f"f{i}"),) for i in range(5))
    )  # fmt: skip
    rig, spy = spy_rig(make_rig, lambda ctx: plan)
    out = rig.run({**PANEL, "sealed_answer": "sealed text"})["result"]
    assert out["advisor_count"] == 5
    comparison = out["comparison"]
    assert comparison["status"] == "ok" and len(comparison["positions"]) == 6  # five memos and S
    assert len(comparison["pairwise"]) == 15  # C(6, 2): five memos plus the sealed answer
    assert comparison["sealed"]["relation"] == "agree"


def test_a_panel_is_capped_at_eight_members(make_rig):
    plan = Plan(
        chain=(S("judge", "fj"),), slots=tuple((S(f"m{i}", f"f{i}"),) for i in range(11))
    )  # fmt: skip
    rig, spy = spy_rig(make_rig, lambda ctx: plan)
    out = rig.run(PANEL)["result"]
    assert out["advisor_count"] == 8 and out["comparison"]["status"] == "ok"
    assert "m8" not in spy.counts and "m7" in spy.counts


def test_the_configured_member_limit_cannot_exceed_eight(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("panel: {max_members: 9, max_paid_members: 1}\n")
    with pytest.raises(ValueError):
        load_config(path)


def test_an_exception_in_compare_leaves_the_job_complete(make_rig):
    def explode(cx):
        raise RuntimeError("compare blew up")

    rig = make_rig()
    rig.engine.register_contract(CoreContract(rig.config, compare=explode))
    rig.codex.script(advisor=[{"json": memo("fine")}])
    out = rig.run(PACKET)
    assert out["status"] == "complete" and out["result"]["advisor_count"] == 1
    comparison = out["result"]["comparison"]
    assert comparison["status"] == "unavailable" and comparison["cause"] == "compare_error"
    assert comparison["cause_detail"]["type"] == "RuntimeError"


def test_an_unusable_comparison_value_also_leaves_the_job_complete(make_rig):
    rig = make_rig()
    rig.engine.register_contract(CoreContract(rig.config, compare=lambda cx: {"sealed": 5}))
    rig.codex.script(advisor=[{"json": memo("fine")}])
    assert rig.run(PACKET)["status"] == "complete"


# -- L10: empty template fields ----------------------------------------------------------


@pytest.mark.parametrize("bad", ["{request_json} {}", "{request_json} {0}", "{request_json} {x}"])
def test_empty_and_unknown_template_fields_are_rejected_at_load(make_rig, bad):
    from coldtake.prompts import load_templates

    rig = make_rig()
    cfg = rig.config.model_copy(update={"prompts": {"alternate": bad}})
    with pytest.raises(ValueError):
        load_templates(cfg)
