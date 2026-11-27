"""Evaluation runner skeleton.

    python -m evals.run --advisor fake --grader fake --out /tmp/oh-fake.jsonl

Real runs are NOT wired in this skeleton. ``--advisor`` and ``--grader`` accept
``fake`` or ``package.module:factory`` (the latter needs ``--allow-real``) so
the real adapter can be plugged in later under the owner's control. The
factories are called as ``factory(role, config)`` and return an ``Advisor`` /
a list of ``Grader`` objects.

Conditions (see README for the design):

==============  =========  ===========================
condition       mode       advisor family vs lead family
==============  =========  ===========================
self_review     review     same
cross_review    review     other
blind_same      alternate  same
blind_other     alternate  other
==============  =========  ===========================
"""
from __future__ import annotations

import argparse
import importlib
import json
import random
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

import yaml

from . import tasks_io
from .advisor import (Advisor, AdvisorRequest, AdvisorResponse, FakeAdvisor, assert_blind, build_request)
from .grading import deterministic
from .grading.combine import grade_response
from .grading.llm import FakeGrader, Grader, choose_grader, grade_with_llm
from .tasks_io import Task

HARNESS_VERSION = "0.1.0"
RESULT_SCHEMA = "coldtake-eval-result/1"

CONDITIONS = {
    "self_review": {"mode": "review", "relation": "same"},
    "cross_review": {"mode": "review", "relation": "other"},
    "blind_same": {"mode": "alternate", "relation": "same"},
    "blind_other": {"mode": "alternate", "relation": "other"},
}
REVIEW_CONDITIONS = ("self_review", "cross_review")
BLIND_CONDITIONS = ("blind_same", "blind_other")

DEFAULT_CONFIG: Dict[str, Any] = {
    "lead_families": ["family_a", "family_b"],
    "families": {"family_a": {"model": None, "effort": None}, "family_b": {"model": None, "effort": None}},
    "graders": {"family_a": {"model": None}, "family_b": {"model": None}},
    "self_framing": "neutral",
}


@dataclass(frozen=True)
class Cell:
    """One (lead family, condition) cell of the matrix."""

    lead_family: str
    condition: str
    mode: str
    advisor_family: str
    model: Optional[str]
    effort: Optional[str]

    @property
    def name(self) -> str:
        return "%s/%s" % (self.lead_family, self.condition)


def load_config(path: Optional[str]) -> Dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path:
        user = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        cfg.update(user)
    fams = list(cfg["families"])
    if len(fams) != 2:
        raise ValueError("config.families must list exactly two families (got %d)" % len(fams))
    for lf in cfg["lead_families"]:
        if lf not in cfg["families"]:
            raise ValueError("lead family %r missing from config.families" % lf)
    return cfg


def other_family(config: Dict[str, Any], family: str) -> str:
    (other,) = [f for f in config["families"] if f != family]
    return other


def build_matrix(config: Dict[str, Any], conditions: Optional[Sequence[str]] = None,
                 lead_families: Optional[Sequence[str]] = None) -> List[Cell]:
    cells = []
    for lead in lead_families or config["lead_families"]:
        for cond in conditions or CONDITIONS:
            spec = CONDITIONS[cond]
            fam = lead if spec["relation"] == "same" else other_family(config, lead)
            fcfg = config["families"][fam] or {}
            cells.append(Cell(lead, cond, spec["mode"], fam, fcfg.get("model"), fcfg.get("effort")))
    return cells


def _git_provenance() -> Dict[str, Any]:
    here = str(Path(__file__).resolve().parent)
    def run(*args: str) -> Optional[str]:
        try:
            return subprocess.run(["git", "-C", here] + list(args), capture_output=True, text=True,
                                  timeout=10, check=True).stdout.strip()
        except Exception:
            return None
    return {"git_sha": run("rev-parse", "HEAD"), "evals_dirty": bool(run("status", "--porcelain", "--", here))}


def harness_provenance() -> Dict[str, Any]:
    return {"version": HARNESS_VERSION, "python": sys.version.split()[0], **_git_provenance()}


@dataclass
class Job:
    task: Task
    cell: Cell
    sample: int

    @property
    def key(self) -> str:
        return "%s|%s|%d" % (self.task.id, self.cell.name, self.sample)


def build_jobs(tasks: Sequence[Task], cells: Sequence[Cell], samples: int, seed: int) -> List[Job]:
    jobs = [Job(t, c, s) for t in tasks for c in cells for s in range(samples)]
    random.Random(seed).shuffle(jobs)
    return jobs


def execute_job(job: Job, advisor: Advisor, graders: Sequence[Grader], *, run_id: str, seed: int,
                framing: str, allow_same_family_grader: bool = False) -> Dict[str, Any]:
    """Run one advisor call plus grading and return the full result record."""
    task, cell = job.task, job.cell
    framing_used = framing if cell.condition == "self_review" else "neutral"
    req = build_request(task, cell.mode, cell.advisor_family, cell.model, cell.effort,
                        framing=framing_used, seed=seed, sample=job.sample, run_id=run_id)
    assert_blind(req, task)  # fail closed before anything leaves the harness
    t0 = time.monotonic()
    try:
        resp: AdvisorResponse = advisor.ask(req)
    except Exception as exc:  # an adapter bug must not kill the run
        resp = AdvisorResponse(mode=req.mode, family=req.advisor_family, error="%s: %s" % (type(exc).__name__, exc))
    if resp.latency_s is None:
        resp.latency_s = time.monotonic() - t0
    rd = resp.to_dict()

    fix_result = None
    if task.kind == "code" and rd.get("proposed_fix") and not rd.get("error"):
        src = deterministic.extract_code(rd["proposed_fix"])
        res = deterministic.grade_fix(task, src)
        fix_result = res.to_dict() if res else None

    llm = None
    needs_llm = task.kind in ("code", "claim") and not rd.get("error")
    grader_info = None
    if needs_llm:
        g = choose_grader(graders, resp.family, allow_same_family_grader)
        if g is not None:
            llm = grade_with_llm(g, task, rd)
            grader_info = {"family": g.family, "model": g.model}
    outcome = grade_response(task, rd, fix_result, llm)

    return {
        "schema": RESULT_SCHEMA,
        "run_id": run_id,
        "key": job.key,
        "task": {"id": task.id, "kind": task.kind, "control": task.control, "difficulty": task.difficulty,
                 "sha256": task.sha256},
        "cell": {"name": cell.name, "lead_family": cell.lead_family, "condition": cell.condition,
                 "mode": cell.mode, "advisor_family": cell.advisor_family, "model": cell.model,
                 "effort": cell.effort, "framing": framing_used},
        "sample": job.sample,
        "seed": seed,
        "request": req.provenance(),
        "visible_payload": req.visible_payload(),
        "response": rd,
        "grades": {
            "fix_test": fix_result,
            "key_phrase_hits": deterministic.key_phrase_hits(task, "%s %s" % (rd.get("text", ""), json.dumps(rd.get("findings")))),
            "llm": llm,
            "grader": grader_info,
        },
        "outcome": outcome,
        "finished_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _import_factory(spec: str) -> Callable[..., Any]:
    module, _, attr = spec.partition(":")
    if not module or not attr:
        raise SystemExit("expected 'package.module:factory', got %r" % spec)
    return getattr(importlib.import_module(module), attr)


def make_advisor_factory(spec: str, tasks: Sequence[Task], allow_real: bool,
                         fake_profile: Optional[Dict[str, Dict[str, float]]] = None, seed: int = 0
                         ) -> Callable[[str, Dict[str, Any]], Advisor]:
    if spec == "fake":
        return lambda lead_family, cfg: FakeAdvisor(list(tasks), lead_family=lead_family, profile=fake_profile, seed=seed)
    if not allow_real:
        raise SystemExit("refusing to load a non-fake advisor without --allow-real")
    factory = _import_factory(spec)
    return lambda lead_family, cfg: factory(lead_family, cfg)


def make_graders(spec: Optional[str], config: Dict[str, Any], allow_real: bool) -> List[Grader]:
    if not spec or spec == "none":
        return []
    if spec == "fake":
        return [FakeGrader(family=f, model="fake-grader-%s" % f) for f in config["families"]]
    if not allow_real:
        raise SystemExit("refusing to load a non-fake grader without --allow-real")
    return list(_import_factory(spec)("graders", config))


def read_done_keys(out_path: Optional[Path], retry_errors: bool = False) -> set:
    """Keys of the records already in ``out_path`` (a record with an advisor error is not
    done when ``retry_errors`` is set, so a resumed run asks for it again)."""
    done = set()
    if out_path and out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if str(rec.get("schema", "")).endswith("/header"):
                continue
            if retry_errors and (rec.get("response") or {}).get("error"):
                continue
            done.add(rec.get("key"))
    return done


def check_resume_compatible(out_path: Optional[Path], tasks: Sequence[Task]) -> None:
    """Refuse to resume into a results file written for a different corpus.

    Raises ``ValueError`` when the header names another corpus version, or when a recorded
    task hash differs from the task as it is now (the task or its hidden test changed).
    """
    if not out_path or not out_path.exists():
        return
    current = {t.id: t.sha256 for t in tasks}
    stale = set()
    for line in out_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if str(rec.get("schema", "")).endswith("/header"):
            if rec.get("corpus_version") != tasks_io.CORPUS_VERSION:
                raise ValueError("%s was written for corpus version %r, this is %r; start a new results file"
                                 % (out_path, rec.get("corpus_version"), tasks_io.CORPUS_VERSION))
            continue
        task = rec.get("task") or {}
        if task.get("id") in current and task.get("sha256") != current[task["id"]]:
            stale.add(task["id"])
    if stale:
        raise ValueError("%s holds results for changed tasks: %s; start a new results file"
                         % (out_path, ", ".join(sorted(stale))))


def header_run_id(out_path: Optional[Path]) -> Optional[str]:
    """Run id in the header line of an existing results file, so a resumed run keeps it."""
    if out_path and out_path.exists():
        with open(out_path, encoding="utf-8") as fh:
            first = fh.readline()
        try:
            rec = json.loads(first)
        except ValueError:
            return None
        if str(rec.get("schema", "")).endswith("/header"):
            return rec.get("run_id")
    return None


def run_matrix(tasks: Sequence[Task], cells: Sequence[Cell], advisor_factory: Callable[[str, Dict[str, Any]], Advisor],
               graders: Sequence[Grader], config: Dict[str, Any], out_path: Optional[Path], *, samples: int = 1,
               seed: int = 0, run_id: Optional[str] = None, resume: bool = False,
               max_cost_usd: Optional[float] = None, workers: int = 1, retry_errors: bool = False,
               log: Callable[[str], None] = print) -> List[Dict[str, Any]]:
    """Run every (task, cell, sample) job once and append a record per job to ``out_path``.

    ``resume`` skips jobs whose key is already in ``out_path``. ``workers`` > 1 runs jobs in
    threads (the advisor and graders must then be thread-safe); records are written by the
    calling thread in completion order.
    """
    run_id = run_id or (resume and header_run_id(out_path)) or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-s%d" % seed
    jobs = build_jobs(tasks, cells, samples, seed)
    if resume:
        check_resume_compatible(out_path, tasks)
    done = read_done_keys(out_path, retry_errors) if resume else set()
    pending = [j for j in jobs if j.key not in done]
    advisors: Dict[str, Advisor] = {}
    for job in pending:
        if job.cell.lead_family not in advisors:
            advisors[job.cell.lead_family] = advisor_factory(job.cell.lead_family, config)
    records: List[Dict[str, Any]] = []
    spent = 0.0
    stop = threading.Event()
    prov = {"run_id": run_id, "harness": harness_provenance(), "corpus_sha256": tasks_io.corpus_sha256(tasks),
            "corpus_version": tasks_io.CORPUS_VERSION,
            "n_tasks": len(tasks), "n_cells": len(cells), "samples": samples, "seed": seed, "workers": workers}
    fh = open(out_path, "a", encoding="utf-8") if out_path else None

    def work(job: Job) -> Optional[Dict[str, Any]]:
        if stop.is_set():
            return None
        return execute_job(job, advisors[job.cell.lead_family], graders, run_id=run_id, seed=seed,
                           framing=config.get("self_framing", "neutral"))

    def accept(rec: Dict[str, Any], n_done: int) -> bool:
        nonlocal spent
        rec["provenance"] = prov
        records.append(rec)
        cost = rec["response"].get("cost_usd")
        if cost:
            spent += cost
        if fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
            fh.flush()
        if max_cost_usd is not None and spent > max_cost_usd:
            log("stopping: spend %.2f USD exceeds cap %.2f USD after %d/%d jobs" % (spent, max_cost_usd, n_done, len(pending)))
            stop.set()
            return False
        return True

    try:
        if fh and not (resume and done):
            fh.write(json.dumps({"schema": RESULT_SCHEMA + "/header", **prov}, sort_keys=True) + "\n")
        if workers <= 1:
            for i, job in enumerate(pending, 1):
                rec = work(job)
                if rec is None or not accept(rec, i):
                    break
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(work, j) for j in pending]
                for i, fut in enumerate(as_completed(futures), 1):
                    rec = fut.result()
                    if rec is not None:
                        accept(rec, i)
    finally:
        if fh:
            fh.close()
    return records


def provider_of(config: Dict[str, Any], family: str) -> str:
    """Provider that serves ``family`` (``families.<f>.provider``, else the family name)."""
    return ((config.get("families") or {}).get(family) or {}).get("provider") or family


def estimate_calls(tasks: Sequence[Task], cells: Sequence[Cell], samples: int,
                   config: Dict[str, Any]) -> Dict[str, Any]:
    """Subscription calls per provider for the matrix, by role.

    ``advisor``: one call per job. ``judge``: the coldtake comparison step, one call per
    blind code or claim job, on the advisor's own provider (decisions are compared by option
    id, no call). ``grader``: one call per code or claim job, on the other family's provider.
    ``expected`` assumes every call is valid on the first try. ``max`` allows the single
    repair of an invalid reply for advisor and judge calls (2x) and, for the grader, one
    reply rejected by the harness's parser with a repair each (4x).
    """
    roles = ("advisor", "judge", "grader")
    per: Dict[str, Dict[str, int]] = {}

    def add(provider: str, role: str, n: int) -> None:
        per.setdefault(provider, {r: 0 for r in roles})[role] += n

    for cell in cells:
        for t in tasks:
            add(provider_of(config, cell.advisor_family), "advisor", samples)
            textual = t.kind in ("code", "claim")
            if textual and cell.mode == "alternate":
                add(provider_of(config, cell.advisor_family), "judge", samples)
            if textual:
                add(provider_of(config, other_family(config, cell.advisor_family)), "grader", samples)
    mult = {"advisor": 2, "judge": 2, "grader": 4}
    for counts in per.values():
        counts["expected"] = sum(counts[r] for r in roles)
        counts["max"] = sum(counts[r] * mult[r] for r in roles)
    return {"per_provider": per,
            "expected": sum(c["expected"] for c in per.values()),
            "max": sum(c["max"] for c in per.values())}


def summarize_plan(tasks: Sequence[Task], cells: Sequence[Cell], samples: int,
                   config: Optional[Dict[str, Any]] = None) -> str:
    n_ctl = sum(t.control for t in tasks)
    kinds = {k: sum(1 for t in tasks if t.kind == k) for k in tasks_io.KINDS}
    calls = len(tasks) * len(cells) * samples
    llm_graded = sum(1 for t in tasks if t.kind in ("code", "claim")) * len(cells) * samples
    lines = ["tasks: %d (%d controls) by kind %s" % (len(tasks), n_ctl, kinds),
             "cells: %d" % len(cells)]
    lines += ["  %s: mode=%s advisor=%s" % (c.name, c.mode, c.advisor_family) for c in cells]
    lines += ["samples per cell: %d" % samples, "advisor calls: %d" % calls,
              "grader calls (upper bound, code+claim tasks): %d" % llm_graded]
    if config is not None:
        est = estimate_calls(tasks, cells, samples, config)
        lines.append("estimated subscription calls per provider (advisor + judge + grader):")
        for prov, c in sorted(est["per_provider"].items()):
            lines.append("  %s: advisor %d, judge %d, grader %d = %d expected, up to %d with repairs"
                         % (prov, c["advisor"], c["judge"], c["grader"], c["expected"], c["max"]))
        lines.append("  total: %d expected, up to %d with repairs (no paid calls)" % (est["expected"], est["max"]))
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--advisor", default="fake", help="'fake' or 'package.module:factory'")
    ap.add_argument("--grader", default="fake", help="'fake', 'none' or 'package.module:factory'")
    ap.add_argument("--allow-real", action="store_true", help="required to load a non-fake advisor/grader")
    ap.add_argument("--config", help="YAML config (see config.example.yaml)")
    ap.add_argument("--out", help="JSONL results path (appended)")
    ap.add_argument("--tasks-dir", default=str(tasks_io.TASKS_DIR))
    ap.add_argument("--ids", help="comma-separated task ids")
    ap.add_argument("--kinds", help="comma-separated kinds: code,decision,claim")
    ap.add_argument(
        "--smoke",
        action="store_true",
        help="run a deterministic 6-task stratified slice (2 code, 2 decision, 2 claim)",
    )
    ap.add_argument("--conditions", help="comma-separated subset of %s" % ",".join(CONDITIONS))
    ap.add_argument("--lead-families", help="comma-separated subset of config lead_families")
    ap.add_argument("--samples", type=int, default=1)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--run-id")
    ap.add_argument("--resume", action="store_true",
                    help="skip jobs already in --out (the run id is taken from its header)")
    ap.add_argument("--retry-errors", action="store_true",
                    help="with --resume, ask again for jobs whose recorded advisor response is an error")
    ap.add_argument("--concurrency", type=int,
                    help="simultaneous calls per provider (default: config coldtake.concurrency_per_provider, "
                         "2; always 1 for the fake advisor)")
    ap.add_argument("--max-cost-usd", type=float)
    ap.add_argument("--dry-run", action="store_true", help="print the plan and exit; makes no calls")
    args = ap.parse_args(argv)

    split = lambda s: [x for x in s.split(",") if x] if s else None
    config = load_config(args.config)
    tasks = tasks_io.load_tasks(Path(args.tasks_dir), ids=split(args.ids), kinds=split(args.kinds))
    if args.smoke:
        picked: List[Task] = []
        for kind in tasks_io.KINDS:
            of_kind = [t for t in tasks if t.kind == kind]
            non_ctl = [t for t in of_kind if not t.control]
            ctl = [t for t in of_kind if t.control]
            picked.extend((non_ctl[:1] + ctl[:1]) if (non_ctl and ctl) else of_kind[:2])
        tasks = picked
    if not tasks:
        raise SystemExit("no tasks selected")
    conds = split(args.conditions)
    for c in conds or ():
        if c not in CONDITIONS:
            raise SystemExit("unknown condition %r" % c)
    cells = build_matrix(config, conds, split(args.lead_families))
    print(summarize_plan(tasks, cells, args.samples, config))
    if args.dry_run:
        return 0
    if not args.out:
        raise SystemExit("--out is required unless --dry-run")
    factory = make_advisor_factory(args.advisor, tasks, args.allow_real, seed=args.seed)
    graders = make_graders(args.grader, config, args.allow_real)
    per_provider = 1 if args.advisor == "fake" else (
        args.concurrency or int((config.get("coldtake") or {}).get("concurrency_per_provider", 2)))
    if args.concurrency:
        config.setdefault("coldtake", {})["concurrency_per_provider"] = args.concurrency
    n_providers = len({provider_of(config, c.advisor_family) for c in cells})
    records = run_matrix(tasks, cells, factory, graders, config, Path(args.out), samples=args.samples,
                         seed=args.seed, run_id=args.run_id, resume=args.resume, max_cost_usd=args.max_cost_usd,
                         workers=max(1, per_provider * n_providers), retry_errors=args.retry_errors)
    print("wrote %d records to %s" % (len(records), args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
