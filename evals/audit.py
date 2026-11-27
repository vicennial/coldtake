"""Corpus audit: ask a reviewer of another family to check each task's ground truth.

The corpus was drafted by one model family (see "Threats to validity" in the README). This tool sends
every task, with its lead answer, ground truth, hidden test, reference fix and rubric, to an coldtake
``review`` request, and asks the reviewer to look for mistakes and ambiguity in the MATERIALS. It writes

* a JSONL file, one record per task (verdict, issues, the reviewer's text, attempts, request id), and
* a Markdown summary listing the flagged tasks.

It changes nothing in ``tasks/``. A human reads the summary and decides.

    python -m evals.audit --dry-run
    python -m evals.audit --allow-real --config my-config.yaml --out audit.jsonl --summary audit.md

Reviewer family defaults to ``openai`` and the corpus author family to ``anthropic`` (config keys
``audit.reviewer_family`` / ``audit.author_family``, or the flags). The adapter forces the reviewer's route;
if it is the author's own family the record says ``same_family: true``. The request is a normal coldtake
review: nothing is sealed, because the reviewer is meant to see the answer key.
"""
from __future__ import annotations

import argparse
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import yaml

from . import run as run_mod
from . import tasks_io
from .adapters import coldtake_adapter as adapter
from .grading import deterministic
from .tasks_io import Task

AUDIT_SCHEMA = "coldtake-eval-audit/1"
_VERDICT = re.compile(r"[*\s]*AUDIT:[\s*]*(ok|flag|uncertain)\b[*]*", re.I)

INSTRUCTIONS = (
    "You are auditing one task of an evaluation corpus, not solving it. The material under primary_answer holds "
    "the task as the lead model was given it, the lead's answer, and the answer key: ground truth, hidden test, "
    "reference fix and grading rubric. Look for mistakes and ambiguity in the MATERIALS. Check each of these:\n"
    "1. The planted error (or, for a control task, the claim that nothing is wrong) is really correct. Look for "
    "other genuine errors in the lead answer that the key does not mention, or a flaw the key calls an error "
    "that is not one.\n"
    "2. Code tasks: the hidden test rejects the lead code for the planted defect, accepts the reference fix, and "
    "accepts every other solution the prompt allows (is it too strict or too lenient, does it depend on "
    "unspecified behaviour?). For a control, it accepts a correct solution.\n"
    "3. Decision tasks: the stated correct option is clearly best on the facts in the prompt, and no other option "
    "is also defensible; the facts used by the rationale are in the prompt.\n"
    "4. Claim tasks: the error span is wrong, and the correct statement is right.\n"
    "5. The prompt is unambiguous and the rubric and key phrases would let a grader decide consistently.\n"
    "Begin `recommendation` with exactly one line: `AUDIT: ok` (no problem found), `AUDIT: flag` (a problem that "
    "would make a grade wrong) or `AUDIT: uncertain`. Then explain. Put each concrete problem as a separate item "
    "in failure_modes, naming the part (ground_truth, hidden_test, rubric, prompt, lead_answer, options, "
    "key_phrases, reference_fix) and why. Do not list style remarks as problems."
)


REPAIR_NOTE = (
    "\n\nYour previous reply to this audit had no verdict line. Audit the same material again. The first line of "
    "`recommendation` MUST be exactly one of `AUDIT: ok`, `AUDIT: flag` or `AUDIT: uncertain`; do not answer or "
    "solve the task itself, and list each concrete problem in failure_modes."
)


def local_checks(task: Task) -> Optional[Dict[str, Any]]:
    """Deterministic evidence for code tasks: hidden-test results on the lead code and the reference fix."""
    if task.kind != "code":
        return None
    out: Dict[str, Any] = {}
    for label, src in (("lead_answer", task.lead_answer), ("reference_fix", task.reference_fix)):
        if src is None:
            continue
        res = deterministic.grade_fix(task, src)
        out[label] = {"passed": res.passed, "n_passed": res.n_passed, "n_failed": res.n_failed,
                      "failed_tests": res.failed_tests[:12]}
    return out


def render_material(task: Task, checks: Optional[Dict[str, Any]] = None) -> str:
    """The audit packet body: everything a human auditor would be given."""
    raw = task.raw
    parts = ["TASK %s (kind: %s, %s)" % (task.id, task.kind, "control: the lead answer is meant to be correct"
                                          if task.control else "one planted error")]
    parts.append("## What the lead was asked\n" + task.prompt.strip())
    if task.options:
        parts.append("## Options\n" + "\n".join("%s. %s: %s" % (o["id"], o["label"], " ".join(o["description"].split()))
                                                 for o in task.options))
    parts.append("## The lead's answer\n" + task.lead_answer.strip())
    parts.append("## Ground truth\n" + yaml.safe_dump(task.ground_truth, sort_keys=True, width=100).strip())
    if task.kind == "code":
        if task.reference_fix:
            parts.append("## Reference fix\n```python\n" + task.reference_fix.strip() + "\n```")
        hidden = task.hidden_test_path
        if hidden is not None and hidden.exists():
            parts.append("## Hidden test (run against the candidate saved as `%s.py`)\n```python\n%s\n```"
                         % (task.grading["deterministic"].get("module", "solution"), hidden.read_text("utf-8").strip()))
        if checks:
            parts.append("## Local check (hidden test run by the harness, not by you)\n"
                         + yaml.safe_dump(checks, sort_keys=True, width=100).strip())
    g = raw["grading"]
    parts.append("## Grading\nmethod: %s\nkey_phrases: %s\nrubric:\n%s" % (
        g["deterministic"].get("type"), json.dumps(g.get("key_phrases", [])), g["rubric"].strip()))
    return "\n\n".join(parts)


def audit_submission(task: Task, checks: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The coldtake ``review`` packet for a task."""
    return {"mode": "review", "question": INSTRUCTIONS, "primary_answer": render_material(task, checks),
            "stakes": "material", "reversibility": "easy"}


def audit_request_id(task: Task, reviewer_family: str, data: Dict[str, Any]) -> str:
    digest = adapter._digest(data, reviewer_family)[:10]
    return "au-%s-%s-%s" % (task.id, reviewer_family, digest)


def parse_audit_verdict(text: str) -> Optional[str]:
    m = _VERDICT.search(text or "")
    return m.group(1).lower() if m else None


def _ask(rt: Any, task: Task, reviewer_family: str, author_family: str, data: Dict[str, Any], model: Optional[str],
         effort: Optional[str]) -> Dict[str, Any]:
    route = rt.route_for(reviewer_family, model, effort)
    job = rt.run_job(audit_request_id(task, reviewer_family, data), data, author_family, route)
    return job


def audit_task(rt: Any, task: Task, reviewer_family: str, author_family: str, *, model: Optional[str] = None,
               effort: Optional[str] = None, with_checks: bool = True) -> Dict[str, Any]:
    t0 = time.monotonic()
    rec: Dict[str, Any] = {"schema": AUDIT_SCHEMA, "task": {"id": task.id, "kind": task.kind, "control": task.control,
                                                              "sha256": task.sha256},
                           "reviewer": {"family": reviewer_family, "author_family": author_family}}
    try:
        checks = local_checks(task) if with_checks else None
        data = audit_submission(task, checks)
        job = _ask(rt, task, reviewer_family, author_family, data, model, effort)
        rec["request_id"] = job["request_id"]
        rec["status"] = job["status"]
        rec["local_checks"] = checks
        rec["attempts"] = adapter._attempt_rows(job["view"])
        if job["status"] != "complete":
            rec["error"] = "%s: %s %s" % (job["status"], job["out"].get("reason"), json.dumps(job["out"].get("cause") or {}))
        else:
            result = rt.engine.load_result(job["request_id"])
            record = result["memos"][0]
            memo = record["memo"]
            verdict = parse_audit_verdict(memo["recommendation"])
            rec["repaired"] = False
            if verdict is None:
                # the reviewer answered without the AUDIT line: ask once more, with the requirement spelled out
                rec["first_request_id"] = job["request_id"]
                repair = dict(data, question=data["question"] + REPAIR_NOTE)
                job2 = _ask(rt, task, reviewer_family, author_family, repair, model, effort)
                rec["repaired"] = True
                rec["request_id"] = job2["request_id"]
                rec["attempts"] = rec["attempts"] + adapter._attempt_rows(job2["view"])
                if job2["status"] == "complete":
                    record = rt.engine.load_result(job2["request_id"])["memos"][0]
                    memo = record["memo"]
                    verdict = parse_audit_verdict(memo["recommendation"])
            rec["reviewer"].update(provider=record["provider"], model=record["model"],
                                   same_family=bool(record["same_family"]))
            # "none" = the reviewer answered twice without a verdict line (not a failed call; status stays complete)
            rec["verdict"] = verdict or "none"
            rec["issues"] = [s for s in memo.get("failure_modes", []) if s.strip()]
            rec["text"] = _VERDICT.sub("", memo["recommendation"], count=1).strip()
            rec["confidence"] = memo.get("confidence")
            rec["error"] = None
    except Exception as exc:  # noqa: BLE001 - one task must not stop the audit
        rec["status"] = "error"
        rec["error"] = "%s: %s" % (type(exc).__name__, exc)
    rec["latency_s"] = round(time.monotonic() - t0, 3)
    rec["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return rec


def render_summary(records: Sequence[Dict[str, Any]], reviewer_family: str, author_family: str) -> str:
    flagged = [r for r in records if r.get("verdict") == "flag"]
    unsure = [r for r in records if r.get("verdict") == "uncertain"]
    failed = [r for r in records if r.get("status") != "complete"]
    nonev = [r for r in records if r.get("status") == "complete" and r.get("verdict") in (None, "none")]
    ok = [r for r in records if r.get("verdict") == "ok"]
    lines = ["# Corpus audit", "",
             "Reviewer family: `%s` (corpus author family: `%s`). Tasks audited: %d." % (reviewer_family, author_family, len(records)),
             "This is a model's reading of the answer key, not a verdict. **Nothing was changed in `tasks/`.** "
             "Check every item below by hand before editing a task.", "",
             "| result | tasks |", "|---|---|",
             "| flagged | %d |" % len(flagged), "| uncertain | %d |" % len(unsure),
             "| ok | %d |" % len(ok), "| no verdict line (after one repair) | %d |" % len(nonev),
             "| failed call | %d |" % len(failed), ""]

    def block(title: str, rows: Sequence[Dict[str, Any]]) -> None:
        if not rows:
            return
        lines.append("## %s" % title)
        lines.append("")
        for r in rows:
            t = r["task"]
            lines.append("### %s (%s%s)" % (t["id"], t["kind"], ", control" if t["control"] else ""))
            lines.append("")
            if r.get("issues"):
                lines.extend("- %s" % " ".join(i.split()) for i in r["issues"])
            elif r.get("error"):
                lines.append("- error: %s" % r["error"])
            if r.get("text"):
                lines.append("")
                lines.append("> " + " ".join(r["text"].split())[:600])
            lines.append("")
    block("Flagged", flagged)
    block("Uncertain", unsure)
    block("No verdict line", nonev)
    block("Failed call", failed)
    if ok:
        lines.append("## No problem found")
        lines.append("")
        lines.append(", ".join(r["task"]["id"] for r in ok))
        lines.append("")
    return "\n".join(lines)


def load_records(path: Path) -> List[Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                out[rec["task"]["id"]] = rec                  # later lines win
    return list(out.values())


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="evaluation YAML (see config.coldtake.example.yaml); needs an `coldtake:` section")
    ap.add_argument("--allow-real", action="store_true", help="required to make real calls")
    ap.add_argument("--reviewer-family", help="default: config audit.reviewer_family, else openai")
    ap.add_argument("--author-family", help="default: config audit.author_family, else anthropic")
    ap.add_argument("--tasks-dir", default=str(tasks_io.TASKS_DIR))
    ap.add_argument("--ids")
    ap.add_argument("--kinds")
    ap.add_argument("--out", help="JSONL records (appended)")
    ap.add_argument("--summary", help="Markdown summary (rewritten from all records in --out)")
    ap.add_argument("--resume", action="store_true", help="skip tasks already answered in --out")
    ap.add_argument("--concurrency", type=int, help="simultaneous calls (default: coldtake.concurrency_per_provider, 2)")
    ap.add_argument("--no-local-checks", action="store_true", help="do not run the hidden tests to give the reviewer evidence")
    ap.add_argument("--dry-run", action="store_true", help="print the call counts and exit")
    args = ap.parse_args(argv)

    split = lambda s: [x for x in s.split(",") if x] if s else None  # noqa: E731
    config = run_mod.load_config(args.config) if args.config else json.loads(json.dumps(run_mod.DEFAULT_CONFIG))
    audit_cfg = config.get("audit") or {}
    reviewer = args.reviewer_family or audit_cfg.get("reviewer_family") or "openai"
    author = args.author_family or audit_cfg.get("author_family") or "anthropic"
    tasks = tasks_io.load_tasks(Path(args.tasks_dir), ids=split(args.ids), kinds=split(args.kinds))
    if not tasks:
        raise SystemExit("no tasks selected")
    provider = run_mod.provider_of(config, reviewer)
    print("audit: %d tasks, reviewer family %s (provider %s), author family %s%s" % (
        len(tasks), reviewer, provider, author, "  [same family: independence not achieved]" if reviewer == author else ""))
    print("calls: %d review calls expected on %s, up to %d with one repair each (no paid calls)" % (
        len(tasks), provider, 2 * len(tasks)))
    if args.dry_run:
        return 0
    if not args.allow_real:
        raise SystemExit("refusing to call a real reviewer without --allow-real")
    if not args.out:
        raise SystemExit("--out is required unless --dry-run")
    out = Path(args.out)
    done = {r["task"]["id"] for r in load_records(out) if r.get("status") == "complete" and r.get("verdict") not in (None, "none")} if args.resume else set()
    pending = [t for t in tasks if t.id not in done]
    rt = adapter.runtime(config)
    workers = max(1, args.concurrency or rt.per_provider)
    lock = threading.Lock()

    def one(task: Task) -> None:
        rec = audit_task(rt, task, reviewer, author, model=audit_cfg.get("model"), effort=audit_cfg.get("effort"),
                         with_checks=not args.no_local_checks)
        with lock, open(out, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, pending))
    records = [r for r in load_records(out) if r["task"]["id"] in {t.id for t in tasks}]
    if args.summary:
        Path(args.summary).write_text(render_summary(records, reviewer, author), encoding="utf-8")
    print("wrote %d records to %s; flagged: %s" % (len(pending), out, [r["task"]["id"] for r in records if r.get("verdict") == "flag"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
