"""Report math on synthetic (fake) result records with known answers."""
import json

import pytest

from evals import report
from evals.stats import mcnemar_exact, wilson_interval


def rec(task_id, lead, cond, label=None, *, control=False, kind="decision", false_alarm=None,
        agrees=None, reproduces=None, graded=True, valid=True, sample=0, run_id="r"):
    return {
        "run_id": run_id,
        "key": "%s|%s/%s|%d" % (task_id, lead, cond, sample),
        "task": {"id": task_id, "kind": kind, "control": control},
        "cell": {"lead_family": lead, "condition": cond},
        "outcome": {"graded": graded, "valid": valid, "label": label, "false_alarm": false_alarm,
                    "agrees_with_wrong": agrees, "reproduces_wrong": reproduces, "notes": []},
    }


def cell(rep, metric, scope, cond, kind="all"):
    (row,) = [r for r in rep["cells"] if (r["metric"], r["scope"], r["condition"], r["kind"]) == (metric, scope, cond, kind)]
    return row


def paired(rep, metric, a, b, scope="A"):
    (row,) = [p for p in rep["paired"] if (p["metric"], p["a"], p["b"], p["scope"]) == (metric, a, b, scope)]
    return row


@pytest.fixture
def records():
    """10 defect tasks and 4 controls, one lead family 'A'.

    self_review identifies t1-t4; cross_review identifies t1-t8; blind_same t1-t6;
    blind_other t3-t10. Anchoring: self_review agrees with the wrong answer on
    t5-t10 (6/10), cross_review on t9-t10 (2/10); blind advisors reproduce it on
    t7-t10 (blind_same) and t1-t2 (blind_other).
    Controls c1-c4: self_review false-alarms on c1 only; cross_review on none;
    blind_same on c1 and c2; blind_other on none.
    """
    out = []
    ids = ["t%d" % i for i in range(1, 11)]
    ident = {"self_review": set(ids[:4]), "cross_review": set(ids[:8]), "blind_same": set(ids[:6]),
             "blind_other": set(ids[2:])}
    persist = {"self_review": set(ids[4:]), "cross_review": set(ids[8:]), "blind_same": set(ids[6:]),
               "blind_other": set(ids[:2])}
    for cond in report.CONDITION_ORDER:
        for t in ids:
            review = cond in report.REVIEW
            out.append(rec(t, "A", cond, "identified" if t in ident[cond] else "missed",
                           agrees=(t in persist[cond]) if review else None,
                           reproduces=None if review else (t in persist[cond])))
    alarms = {"self_review": {"c1"}, "cross_review": set(), "blind_same": {"c1", "c2"}, "blind_other": set()}
    for cond in report.CONDITION_ORDER:
        for c in ["c1", "c2", "c3", "c4"]:
            fa = c in alarms[cond]
            out.append(rec(c, "A", cond, "false_alarm" if fa else "clean", control=True, false_alarm=fa))
    return out


def test_proportions_and_wilson(records):
    rep = report.build_report(records)
    row = cell(rep, "recall_strict", "A", "self_review")
    assert (row["k"], row["n"]) == (4, 10)
    lo, hi = wilson_interval(4, 10)
    assert row["ci_low"] == pytest.approx(lo) and row["ci_high"] == pytest.approx(hi)
    assert (row["ci_low"], row["ci_high"]) == pytest.approx((0.1682, 0.6873), abs=5e-4)
    assert cell(rep, "recall_strict", "A", "cross_review")["k"] == 8
    assert cell(rep, "recall_strict", "A", "blind_other")["k"] == 8
    assert cell(rep, "false_alarm", "A", "blind_same")["k"] == 2
    assert cell(rep, "false_alarm", "A", "blind_same")["n"] == 4
    assert cell(rep, "wrong_persist", "A", "self_review")["k"] == 6
    assert cell(rep, "wrong_persist", "A", "blind_same")["k"] == 4
    assert cell(rep, "recall_lenient", "A", "self_review")["k"] == 4


def test_paired_mcnemar_q1(records):
    rep = report.build_report(records)
    p = paired(rep, "recall_strict", "self_review", "cross_review")
    # self hits t1-4, cross hits t1-8: A only = 0, B only = 4 -> p = 2 * 0.5**4
    assert (p["only_a"], p["only_b"], p["both"], p["neither"]) == (0, 4, 4, 2)
    assert p["p_mcnemar_exact"] == pytest.approx(0.125)
    assert p["diff_a_minus_b"] == pytest.approx(-0.4)
    p = paired(rep, "recall_strict", "blind_same", "blind_other")
    # blind_same t1-6, blind_other t3-10: A only {t1,t2}, B only {t7..t10}
    assert (p["only_a"], p["only_b"]) == (2, 4)
    assert p["p_mcnemar_exact"] == pytest.approx(mcnemar_exact(2, 4)) == pytest.approx(0.6875)


def test_paired_anchoring_q2(records):
    rep = report.build_report(records)
    p = paired(rep, "wrong_persist", "self_review", "blind_same")
    # self persists t5-10, blind_same persists t7-10: A only {t5,t6}, B only {}
    assert (p["only_a"], p["only_b"]) == (2, 0)
    assert p["p_mcnemar_exact"] == pytest.approx(0.5)
    p = paired(rep, "wrong_persist", "cross_review", "blind_other")
    assert (p["only_a"], p["only_b"]) == (2, 2)  # a: t9,t10  b: t1,t2 (t9,t10 not in blind_other)
    assert p["p_mcnemar_exact"] == pytest.approx(1.0)


def test_false_alarm_pair(records):
    rep = report.build_report(records)
    p = paired(rep, "false_alarm", "blind_same", "blind_other")
    assert (p["only_a"], p["only_b"], p["n_pairs"]) == (2, 0, 4)


def test_ungraded_and_invalid_handling():
    recs = [rec("t1", "A", "self_review", "identified", agrees=False),
            rec("t2", "A", "self_review", None, graded=False),
            rec("t3", "A", "self_review", "missed", valid=False, agrees=None),
            rec("t4", "A", "self_review", "missed", agrees=True)]
    rep = report.build_report(recs)
    row = cell(rep, "recall_strict", "A", "self_review")
    assert (row["k"], row["n"]) == (1, 3)                 # ungraded excluded, invalid counted as a miss
    assert rep["meta"]["n_ungraded"] == 1 and rep["meta"]["n_invalid_responses"] == 1
    row = cell(rep, "wrong_persist", "A", "self_review")
    assert (row["k"], row["n"]) == (1, 2)                 # invalid response excluded from anchoring


def test_majority_collapse_of_samples():
    recs = []
    for s, lab in enumerate(["identified", "identified", "missed"]):
        recs.append(rec("t1", "A", "self_review", lab, sample=s))
    for s, lab in enumerate(["identified", "missed"]):          # tie -> False
        recs.append(rec("t2", "A", "self_review", lab, sample=s))
    rep = report.build_report(recs)
    row = cell(rep, "recall_strict", "A", "self_review")
    assert (row["k"], row["n"]) == (1, 2)


def test_pooled_scope_and_kind_rows():
    recs = []
    for lead in ("A", "B"):
        for i in range(5):
            for cond in ("self_review", "cross_review"):
                kind = "code" if i < 2 else "claim"
                recs.append(rec("t%d" % i, lead, cond, "identified" if (cond == "cross_review" or i == 0) else "missed",
                                kind=kind, agrees=False))
    rep = report.build_report(recs)
    assert cell(rep, "recall_strict", "pooled", "cross_review")["n"] == 10
    assert cell(rep, "recall_strict", "pooled", "self_review")["k"] == 2
    assert cell(rep, "recall_strict", "A", "self_review", "code")["n"] == 2
    p = paired(rep, "recall_strict", "self_review", "cross_review", scope="pooled")
    assert p["non_independent_units"] is True and p["n_pairs"] == 10 and p["only_b"] == 8
    assert paired(rep, "recall_strict", "self_review", "cross_review", scope="A")["non_independent_units"] is False


def test_empty_and_markdown(records, tmp_path):
    rep = report.build_report(records)
    md = report.render_markdown(rep)
    assert "Wilson" in md and "McNemar" in md and "self_review" in md
    assert "40.0% [16.8-68.7] (4/10)" in md
    path = tmp_path / "r.jsonl"
    path.write_text("\n".join(["{\"schema\": \"coldtake-eval-result/1/header\"}"] + [json.dumps(r) for r in records]))
    loaded = report.load_records([path])
    assert len(loaded) == len(records)               # header skipped
    out_json = tmp_path / "rep.json"
    assert report.main([str(path), "--json", str(out_json), "--markdown", str(tmp_path / "r.md")]) == 0
    assert json.loads(out_json.read_text())["meta"]["n_records"] == len(records)


def test_grader_agreement():
    a = [rec("t%d" % i, "A", "self_review", lab) for i, lab in enumerate(["identified", "missed", "partial", "identified"])]
    b = [rec("t%d" % i, "A", "self_review", lab) for i, lab in enumerate(["identified", "missed", "missed", "identified"])]
    for r in b:
        r["run_id"] = "other"
    ag = report.grader_agreement(a, b)
    assert ag["n"] == 4 and ag["raw_agreement"] == pytest.approx(0.75)
    assert 0 < ag["cohens_kappa"] < 1
