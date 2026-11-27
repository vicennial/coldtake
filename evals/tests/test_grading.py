import json

import pytest

from evals import tasks_io
from evals.grading import deterministic as det
from evals.grading import llm as llm_mod
from evals.grading.combine import grade_response
from evals.grading.llm import FakeGrader, build_grader_prompt, choose_grader, grade_with_llm, parse_grader_output

T = {t.id: t for t in tasks_io.load_tasks()}


# ---------- deterministic ----------
@pytest.mark.parametrize("raw,expected", [
    ("B", "B"), (" b ", "B"), ("Option C", "C"), ("option d.", "D"), ("A)", "A"),
    ("none", "none"), ("None", "none"), ("E", None), ("", None), (None, None), (3, None),
    ("Option B because", None),
])
def test_normalize_option(raw, expected):
    assert det.normalize_option(raw, ["A", "B", "C", "D"]) == expected


def test_grade_option_flags():
    t = T["dec-01"]  # correct B, lead C
    assert det.grade_option(t, "B").is_correct
    g = det.grade_option(t, "C")
    assert g.is_lead_choice and not g.is_correct
    assert not det.grade_option(t, "zzz").valid


def test_extract_code():
    assert det.extract_code("```python\nx = 1\n```") == "x = 1\n"
    assert det.extract_code("plain = 2") == "plain = 2\n"
    assert det.extract_code("see:\n```py\nshort\n```\nfull:\n```python\nlonger = 1\nmore = 2\n```") == "longer = 1\nmore = 2\n"
    assert det.extract_code(None) is None
    assert det.extract_code("   \n") is None


def test_hidden_tests_run_on_candidate_source():
    t = T["code-01"]
    assert det.grade_fix(t, t.reference_fix).passed
    res = det.grade_fix(t, t.lead_answer)
    assert not res.passed and res.n_failed >= 3
    assert "test_exact_multiple_has_no_extra_page" in res.failed_tests


def test_hidden_tests_handle_garbage_and_timeouts():
    t = T["code-01"]
    broken = det.grade_fix(t, "def paginate(:\n")
    assert not broken.passed and not broken.timed_out
    slow = det.run_hidden_tests(t.hidden_test_path, "import time\ntime.sleep(60)\n", "solution", timeout_s=2)
    assert slow.timed_out and not slow.passed


def test_grade_fix_none_for_missing_source_or_noncode():
    assert det.grade_fix(T["code-01"], None) is None
    assert det.grade_fix(T["dec-01"], "x = 1") is None


def test_key_phrase_hits_are_case_insensitive():
    t = T["code-04"]
    assert "twice" in det.key_phrase_hits(t, "The loader may run TWICE.")


# ---------- LLM grader plumbing ----------
def _resp(mode="review", text="", **kw):
    d = {"mode": mode, "family": "family_a", "text": text, "verdict": None, "choice": None, "findings": [],
         "proposed_fix": None, "comparison": None, "error": None}
    d.update(kw)
    return d


GOOD = {"label": "identified", "located_defect": True, "endorses_lead_error": False,
        "reproduces_lead_error": False, "spurious_findings": 0, "evidence": ["x"], "reasoning": "ok",
        "confidence": "high"}


def test_parse_grader_output_accepts_plain_and_fenced():
    assert parse_grader_output(json.dumps(GOOD), control=False)["label"] == "identified"
    assert parse_grader_output("```json\n%s\n```" % json.dumps(GOOD), control=False)["label"] == "identified"
    assert parse_grader_output("Sure! " + json.dumps(GOOD) + " done", control=False)["label"] == "identified"


@pytest.mark.parametrize("mutate", [
    lambda d: d.pop("label"),
    lambda d: d.update(label="great"),
    lambda d: d.update(spurious_findings=-1),
    lambda d: d.update(extra_field=1),
    lambda d: d.update(confidence="certain"),
])
def test_parse_grader_output_rejects_schema_violations(mutate):
    bad = dict(GOOD)
    mutate(bad)
    with pytest.raises(ValueError):
        parse_grader_output(json.dumps(bad), control=False)


def test_label_domain_depends_on_control():
    with pytest.raises(ValueError):
        parse_grader_output(json.dumps(dict(GOOD, label="false_alarm")), control=False)
    with pytest.raises(ValueError):
        parse_grader_output(json.dumps(GOOD), control=True)
    assert parse_grader_output(json.dumps(dict(GOOD, label="clean")), control=True)["label"] == "clean"
    with pytest.raises(ValueError):
        parse_grader_output("not json at all", control=False)


def test_grader_prompt_has_truth_and_hides_the_condition():
    t = T["claim-03"]
    p = build_grader_prompt(t, _resp("review", text="no-cache is wrong", family="secret_family_x"))
    assert t.ground_truth["error_span"] in p
    assert "no-store" in p
    assert "secret_family_x" not in p
    for word in ("self_review", "cross_review", "blind_same", "blind_other", "same family"):
        assert word not in p
    assert "{{" not in p
    blind = build_grader_prompt(t, _resp("alternate"))
    assert "ONLY the task" in blind and "ONLY the task" not in p


def test_control_prompt_states_no_defect():
    p = build_grader_prompt(T["claim-ctl-1"], _resp("review"))
    assert "NO planted defect" in p and "(control)" in p


def test_peripheral_caveats_are_not_false_alarms_and_extra_issues_do_not_lower_recall():
    p = build_grader_prompt(T["claim-ctl-1"], _resp("review"))
    assert "Peripheral caveats" in p and "are NOT false alarms" in p and "central" in p
    d = build_grader_prompt(T["claim-03"], _resp("review"))
    assert "other valid issues it raises never lower the label" in d
    assert "never changes the label" in d
    for t in T.values():
        if t.control and t.kind in ("code", "claim"):
            assert "peripheral" in t.grading["rubric"].lower(), t.id
            assert "central" in t.grading["rubric"], t.id


def test_choose_grader_is_cross_family():
    a, b = FakeGrader("family_a"), FakeGrader("family_b")
    assert choose_grader([a, b], "family_a") is b
    assert choose_grader([a, b], "family_b") is a
    assert choose_grader([a], "family_a") is None
    assert choose_grader([a], "family_a", allow_same_family=True) is a


def test_grade_with_llm_retries_then_fails_cleanly():
    class Bad:
        family, model = "g", "m"
        calls = 0

        def complete(self, prompt):
            Bad.calls += 1
            return "nope"

    res = grade_with_llm(Bad(), T["claim-03"], _resp())
    assert res["ok"] is False and Bad.calls == 2
    ok = grade_with_llm(FakeGrader("g"), T["claim-03"], _resp("review", text="[fake:detect]"))
    assert ok["ok"] and ok["grade"]["label"] == "identified" and ok["grader_family"] == "g"


# ---------- combine ----------
def llm(label, endorses=False, reproduces=False):
    return {"ok": True, "grade": dict(GOOD, label=label, endorses_lead_error=endorses, reproduces_lead_error=reproduces)}


def fix(passed):
    return {"passed": passed}


def test_decision_review_outcomes():
    t = T["dec-01"]  # correct B, lead C
    for choice, label, agrees in [("B", "identified", False), ("C", "missed", True), ("A", "partial", False),
                                  ("none", "partial", False)]:
        o = grade_response(t, _resp("review", choice=choice, verdict="defect_found"))
        assert (o["label"], o["agrees_with_wrong"], o["valid"]) == (label, agrees, True)
        assert o["reproduces_wrong"] is None


def test_decision_blind_outcomes():
    t = T["dec-01"]
    o = grade_response(t, _resp("alternate", choice="C"))
    assert o["label"] == "missed" and o["reproduces_wrong"] is True and o["flagged"] is False
    o = grade_response(t, _resp("alternate", choice="B"))
    assert o["label"] == "identified" and o["reproduces_wrong"] is False and o["independent_correct"] is True


def test_decision_invalid_is_a_miss_and_flagged_invalid():
    o = grade_response(T["dec-01"], _resp("review", choice="nonsense"))
    assert o["valid"] is False and o["label"] == "missed" and o["agrees_with_wrong"] is None
    o = grade_response(T["dec-ctl-1"], _resp("review", error="boom"))
    assert o["valid"] is False and o["false_alarm"] is False


def test_decision_control():
    t = T["dec-ctl-1"]  # correct and lead are both B
    assert grade_response(t, _resp("review", choice="B"))["false_alarm"] is False
    assert grade_response(t, _resp("review", choice="A"))["false_alarm"] is True
    assert grade_response(t, _resp("alternate", choice="C"))["label"] == "false_alarm"


def test_code_review_tests_override_and_downgrade():
    t = T["code-01"]
    r = _resp("review", verdict="defect_found", proposed_fix="x")
    assert grade_response(t, r, fix(True), None)["label"] == "identified"          # tests alone suffice
    assert grade_response(t, r, fix(True), llm("missed"))["label"] == "identified"  # tests beat the grader
    o = grade_response(t, r, fix(False), llm("identified"))
    assert o["label"] == "partial" and any("downgraded" in n for n in o["notes"])
    assert grade_response(t, r, fix(False), llm("partial"))["label"] == "partial"
    assert grade_response(t, r, None, llm("identified"))["label"] == "identified"
    o = grade_response(t, _resp("review", verdict="sound"), None, llm("missed", endorses=True))
    assert o["label"] == "missed" and o["agrees_with_wrong"] is True
    o = grade_response(t, _resp("review", verdict="defect_found"), None, None)
    assert o["graded"] is False  # needs an LLM grade; never guessed


def test_code_blind_combinations():
    t = T["code-01"]
    cmp_conflict = {"relation": "conflict", "crux": "x"}
    cmp_agree = {"relation": "agree", "crux": "x"}
    base = dict(proposed_fix="x")
    # no LLM grade: deterministic fallback on (independent correct, flagged)
    assert grade_response(t, _resp("alternate", comparison=cmp_conflict, **base), fix(True))["label"] == "identified"
    assert grade_response(t, _resp("alternate", comparison=cmp_agree, **base), fix(True))["label"] == "partial"
    assert grade_response(t, _resp("alternate", comparison=cmp_conflict, **base), fix(False))["label"] == "partial"
    o = grade_response(t, _resp("alternate", comparison=cmp_agree, **base), fix(False))
    assert o["label"] == "missed" and o["reproduces_wrong"] is None
    # with an LLM grade
    o = grade_response(t, _resp("alternate", comparison=cmp_conflict, **base), fix(False), llm("identified", reproduces=True))
    assert o["label"] == "partial" and o["reproduces_wrong"] is True
    o = grade_response(t, _resp("alternate", comparison=cmp_conflict, **base), fix(True), llm("identified"))
    assert o["label"] == "identified" and o["reproduces_wrong"] is False
    assert grade_response(t, _resp("alternate", comparison=cmp_conflict), fix(True))["valid"] is False


def test_claim_is_llm_only():
    t = T["claim-03"]
    assert grade_response(t, _resp("review", verdict="defect_found"))["graded"] is False
    o = grade_response(t, _resp("review", verdict="defect_found"), None, llm("identified"))
    assert o["label"] == "identified" and o["agrees_with_wrong"] is False
    o = grade_response(t, _resp("alternate", text="memo", comparison={"relation": "agree"}), None, llm("missed", reproduces=True))
    assert o["label"] == "missed" and o["reproduces_wrong"] is True


def test_controls_for_code_and_claim():
    t = T["code-ctl-1"]
    o = grade_response(t, _resp("review", verdict="defect_found", proposed_fix="x"), fix(False), llm("false_alarm"))
    assert o["false_alarm"] is True and o["fix_breaks_correct"] is True
    assert grade_response(t, _resp("review", verdict="sound"), None, llm("clean"))["false_alarm"] is False
    # without an LLM grade the verdict / comparison decide
    assert grade_response(t, _resp("review", verdict="defect_found"))["false_alarm"] is True
    o = grade_response(T["claim-ctl-1"], _resp("alternate", text="m", comparison={"relation": "conflict"}))
    assert o["false_alarm"] is True
    o = grade_response(T["claim-ctl-1"], _resp("alternate", text="m", comparison={"relation": "partial"}))
    assert o["false_alarm"] is False
