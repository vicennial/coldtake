"""Compute the headline numbers from one or more JSONL result files.

    python -m evals.report results.jsonl [more.jsonl ...] [--json out.json] [--markdown out.md]

Metrics (all per lead family and condition, plus pooled over lead families):

recall_strict    share of defect tasks where the advisor ``identified`` the
                 planted defect (Q1).
recall_lenient   identified or partial.
false_alarm      share of control tasks where the advisor asserted a material
                 defect or disagreed with a correct lead answer.
wrong_persist    on defect tasks, the share where the wrong lead answer
                 survived: review conditions, the reviewer who SAW it agreed
                 with the planted flaw (anchoring); blind conditions, the
                 advisor who never saw it made the same mistake on its own
                 (the independent base rate) (Q2).

Every proportion has a Wilson 95% interval. Paired comparisons use the exact
McNemar test on tasks present in both conditions. Pooling over lead families
treats (task, lead family) pairs as units; those units share tasks, so pooled
p-values are optimistic and are labelled as such.

When a cell has several samples per task they are collapsed by majority vote
(a tie counts as False) before any interval or test.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .stats import cohens_kappa, mcnemar_exact, paired_counts, wilson_interval

CONDITION_ORDER = ["self_review", "cross_review", "blind_same", "blind_other"]
REVIEW = ("self_review", "cross_review")
POOLED = "pooled"

# (metric, condition a, condition b, question)
PAIRS = [
    ("recall_strict", "self_review", "cross_review", "Q1: self-review vs cross-family review"),
    ("recall_strict", "blind_same", "blind_other", "Q1: blind same family vs blind other family"),
    ("recall_strict", "self_review", "blind_same", "Q1: self-review vs blind same family"),
    ("recall_strict", "cross_review", "blind_other", "Q1: cross-family review vs blind other family"),
    ("false_alarm", "self_review", "cross_review", "false alarms: self vs cross review"),
    ("false_alarm", "blind_same", "blind_other", "false alarms: blind same vs other"),
    ("wrong_persist", "self_review", "blind_same", "Q2: reviewer agrees with wrong lead (self) vs blind same family reproduces it"),
    ("wrong_persist", "cross_review", "blind_other", "Q2: reviewer agrees with wrong lead (cross) vs blind other family reproduces it"),
    ("wrong_persist", "self_review", "cross_review", "Q2: anchoring, self vs cross reviewer"),
    ("wrong_persist", "blind_same", "blind_other", "Q2: blind same vs blind other reproduce the wrong answer"),
]


def load_records(paths: Iterable[Path]) -> List[Dict[str, Any]]:
    by_key: Dict[str, Dict[str, Any]] = {}
    for p in paths:
        for line in Path(p).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if str(rec.get("schema", "")).endswith("/header"):
                continue
            by_key[(rec.get("run_id"), rec["key"])] = rec  # later lines win
    return list(by_key.values())


def _metric_values(rec: Dict[str, Any]) -> Dict[str, Optional[bool]]:
    """Per-record booleans; a metric that does not apply or is ungraded is absent/None."""
    task, cond, out = rec["task"], rec["cell"]["condition"], rec["outcome"]
    vals: Dict[str, Optional[bool]] = {}
    if not out.get("graded"):
        return vals
    if task["control"]:
        vals["false_alarm"] = bool(out.get("false_alarm"))
        return vals
    label = out["label"]
    vals["recall_strict"] = label == "identified"
    vals["recall_lenient"] = label in ("identified", "partial")
    key = "agrees_with_wrong" if cond in REVIEW else "reproduces_wrong"
    if out.get("valid", True) and out.get(key) is not None:
        vals["wrong_persist"] = bool(out[key])
    return vals


def _majority(flags: Sequence[bool]) -> bool:
    return sum(flags) * 2 > len(flags)


def collapse(records: Sequence[Dict[str, Any]]) -> Dict[Tuple[str, str, str, str], Dict[Tuple[str, str], bool]]:
    """{(metric, lead_family, condition, kind): {(task_id, lead_family): bool}} after majority vote."""
    raw: Dict[Tuple[str, str, str, str, str], List[bool]] = defaultdict(list)
    for rec in records:
        lead, cond, kind = rec["cell"]["lead_family"], rec["cell"]["condition"], rec["task"]["kind"]
        for metric, val in _metric_values(rec).items():
            if val is None:
                continue
            raw[(metric, lead, cond, kind, rec["task"]["id"])].append(val)
    out: Dict[Tuple[str, str, str, str], Dict[Tuple[str, str], bool]] = defaultdict(dict)
    for (metric, lead, cond, kind, tid), flags in raw.items():
        out[(metric, lead, cond, kind)][(tid, lead)] = _majority(flags)
    return out


def _units(collapsed, metric: str, lead: str, cond: str, kind: Optional[str] = None) -> Dict[Tuple[str, str], bool]:
    """Units for a (metric, lead family or pooled, condition[, kind])."""
    out: Dict[Tuple[str, str], bool] = {}
    for (m, l, c, k), units in collapsed.items():
        if m != metric or c != cond:
            continue
        if lead != POOLED and l != lead:
            continue
        if kind is not None and k != kind:
            continue
        out.update(units)
    return out


def proportion(units: Dict[Any, bool]) -> Dict[str, Any]:
    n = len(units)
    k = sum(units.values())
    lo, hi = wilson_interval(k, n)
    return {"k": k, "n": n, "rate": (k / n) if n else None, "ci_low": lo, "ci_high": hi}


def build_report(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    collapsed = collapse(records)
    leads = sorted({r["cell"]["lead_family"] for r in records})
    scopes = leads + ([POOLED] if len(leads) > 1 else [])
    conds = [c for c in CONDITION_ORDER if any(r["cell"]["condition"] == c for r in records)]
    kinds = sorted({r["task"]["kind"] for r in records})

    n_invalid = sum(1 for r in records if not r["outcome"].get("valid", True))
    n_ungraded = sum(1 for r in records if not r["outcome"].get("graded"))
    n_fix_disagree = sum(1 for r in records
                         if "downgraded" in " ".join(r["outcome"].get("notes", [])))
    meta = {
        "n_records": len(records),
        "n_invalid_responses": n_invalid,
        "n_ungraded": n_ungraded,
        "n_llm_downgraded_by_tests": n_fix_disagree,
        "n_tasks": len({r["task"]["id"] for r in records}),
        "lead_families": leads,
        "conditions": conds,
        "samples_collapse": "majority, tie -> False",
        "run_ids": sorted({r["run_id"] for r in records}),
    }

    cells = []
    for metric in ("recall_strict", "recall_lenient", "false_alarm", "wrong_persist"):
        for scope in scopes:
            for cond in conds:
                row = {"metric": metric, "scope": scope, "condition": cond, "kind": "all"}
                row.update(proportion(_units(collapsed, metric, scope, cond)))
                cells.append(row)
                if metric in ("recall_strict", "wrong_persist"):
                    for kind in kinds:
                        row = {"metric": metric, "scope": scope, "condition": cond, "kind": kind}
                        row.update(proportion(_units(collapsed, metric, scope, cond, kind)))
                        if row["n"]:
                            cells.append(row)

    paired = []
    for metric, a, b, label in PAIRS:
        if a not in conds or b not in conds:
            continue
        for scope in scopes:
            ua, ub = _units(collapsed, metric, scope, a), _units(collapsed, metric, scope, b)
            pc = paired_counts(ua, ub)
            n = sum(pc.values())
            if not n:
                continue
            ra, rb = (pc["both"] + pc["only_a"]) / n, (pc["both"] + pc["only_b"]) / n
            paired.append({
                "question": label, "metric": metric, "scope": scope, "a": a, "b": b, "n_pairs": n,
                "rate_a": ra, "rate_b": rb, "diff_a_minus_b": ra - rb,
                "only_a": pc["only_a"], "only_b": pc["only_b"], "both": pc["both"], "neither": pc["neither"],
                "p_mcnemar_exact": mcnemar_exact(pc["only_a"], pc["only_b"]),
                "non_independent_units": scope == POOLED,
            })
    return {"meta": meta, "cells": cells, "paired": paired}


def _pct(row: Dict[str, Any]) -> str:
    if not row["n"]:
        return "n/a (0)"
    return "%.1f%% [%.1f-%.1f] (%d/%d)" % (100 * row["rate"], 100 * row["ci_low"], 100 * row["ci_high"], row["k"], row["n"])


METRIC_TITLES = {
    "recall_strict": "Error recall, strict (identified)",
    "recall_lenient": "Error recall, lenient (identified + partial)",
    "false_alarm": "False-alarm rate (control tasks)",
    "wrong_persist": "Wrong lead answer survives (review: agrees = anchoring; blind: reproduces it independently)",
}


def render_markdown(rep: Dict[str, Any]) -> str:
    m = rep["meta"]
    lines = ["# coldtake eval report", "",
             "Records: %d; tasks: %d; invalid advisor responses: %d; ungraded: %d; LLM labels downgraded by tests: %d."
             % (m["n_records"], m["n_tasks"], m["n_invalid_responses"], m["n_ungraded"], m["n_llm_downgraded_by_tests"]),
             "Samples collapsed by %s. Proportions show Wilson 95%% intervals." % m["samples_collapse"], ""]
    conds = m["conditions"]
    for metric, title in METRIC_TITLES.items():
        lines += ["## " + title, "", "| scope | kind | " + " | ".join(conds) + " |",
                  "|---|---|" + "---|" * len(conds)]
        seen = []
        for row in rep["cells"]:
            if row["metric"] == metric and (row["scope"], row["kind"]) not in seen:
                seen.append((row["scope"], row["kind"]))
        for scope, kind in seen:
            vals = {r["condition"]: r for r in rep["cells"]
                    if r["metric"] == metric and r["scope"] == scope and r["kind"] == kind}
            lines.append("| %s | %s | %s |" % (scope, kind, " | ".join(_pct(vals[c]) if c in vals else "-" for c in conds)))
        lines.append("")
    lines += ["## Paired comparisons (exact McNemar)", "",
              "| question | scope | A vs B | pairs | A | B | A only | B only | p |", "|---|---|---|---|---|---|---|---|---|"]
    for p in rep["paired"]:
        flag = " *" if p["non_independent_units"] else ""
        lines.append("| %s | %s | %s vs %s | %d | %.1f%% | %.1f%% | %d | %d | %.4f%s |" % (
            p["question"], p["scope"], p["a"], p["b"], p["n_pairs"], 100 * p["rate_a"], 100 * p["rate_b"],
            p["only_a"], p["only_b"], p["p_mcnemar_exact"], flag))
    lines += ["", "`*` pooled over lead families: units share tasks, so the p-value is optimistic.", "",
              "Read with the threats to validity in evals/README.md: planted errors are not natural bugs, n is small, "
              "no multiple-comparison correction is applied, and a p-value here is a screening signal, not a result."]
    return "\n".join(lines) + "\n"


def grader_agreement(a: Sequence[Dict[str, Any]], b: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Label agreement between two result sets graded by different graders (same keys)."""
    ka = {(r["run_id"], r["key"]): r["outcome"].get("label") for r in a if r["outcome"].get("graded")}
    kb = {r["key"]: r["outcome"].get("label") for r in b if r["outcome"].get("graded")}
    keys = [k for k in ka if k[1] in kb]
    la = [ka[k] for k in keys]
    lb = [kb[k[1]] for k in keys]
    agree = sum(1 for x, y in zip(la, lb) if x == y)
    return {"n": len(keys), "raw_agreement": (agree / len(keys)) if keys else None, "cohens_kappa": cohens_kappa(la, lb)}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", nargs="+", type=Path)
    ap.add_argument("--json", type=Path, help="write the report as JSON")
    ap.add_argument("--markdown", type=Path, help="write Markdown here (default: stdout)")
    ap.add_argument("--agreement", type=Path, help="second results file (other grader) for label agreement/kappa")
    args = ap.parse_args(argv)
    records = load_records(args.results)
    if not records:
        raise SystemExit("no result records found")
    rep = build_report(records)
    if args.agreement:
        rep["grader_agreement"] = grader_agreement(records, load_records([args.agreement]))
    md = render_markdown(rep)
    if "grader_agreement" in rep:
        md += "\nGrader agreement: %s\n" % json.dumps(rep["grader_agreement"])
    if args.json:
        args.json.write_text(json.dumps(rep, indent=2, sort_keys=True), encoding="utf-8")
    if args.markdown:
        args.markdown.write_text(md, encoding="utf-8")
    else:
        sys.stdout.write(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
