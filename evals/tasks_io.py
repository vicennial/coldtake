"""Load and validate evaluation tasks.

A task is one YAML file under ``evals/tasks``. Code tasks reference a hidden
pytest file under ``evals/tasks/hidden``. See ``evals/README.md`` for the
schema in prose and ``evals/schemas/task.schema.json`` for the machine form.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import yaml

EVALS_DIR = Path(__file__).resolve().parent
TASKS_DIR = EVALS_DIR / "tasks"
TASK_SCHEMA_PATH = EVALS_DIR / "schemas" / "task.schema.json"

KINDS = ("code", "decision", "claim")
# Bumped whenever the task files, hidden tests or grading rubrics change in a way that makes
# earlier results incomparable. It is part of corpus_sha256 and of every run header; a resumed
# run refuses a results file written for another version (see run.check_resume_compatible).
#   1: first corpus
#   2: audit fixes (see evals/AUDIT-FIXES.md)
#   3: second audit round and the grader rule on peripheral caveats (same file)
#   4: final pass: hidden-test gaps, claim-10 wording, code-06 prompt (same file)
CORPUS_VERSION = "4"
MAX_LEAD_CODE_LINES = 80
MAX_CLAIM_WORDS = 250


def normalize_ws(text: str) -> str:
    """Collapse all whitespace runs to single spaces (for span matching)."""
    return re.sub(r"\s+", " ", text).strip()


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Task:
    """A loaded task. ``raw`` is the parsed YAML mapping."""

    raw: Dict[str, Any]
    path: Path

    @property
    def id(self) -> str:
        return self.raw["id"]

    @property
    def kind(self) -> str:
        return self.raw["kind"]

    @property
    def control(self) -> bool:
        return bool(self.raw["control"])

    @property
    def difficulty(self) -> str:
        return self.raw["difficulty"]

    @property
    def prompt(self) -> str:
        return self.raw["prompt"]

    @property
    def lead_answer(self) -> str:
        return self.raw["lead_answer"]

    @property
    def ground_truth(self) -> Dict[str, Any]:
        return self.raw["ground_truth"]

    @property
    def grading(self) -> Dict[str, Any]:
        return self.raw["grading"]

    @property
    def options(self) -> Optional[List[Dict[str, str]]]:
        return self.raw.get("options")

    @property
    def reference_fix(self) -> Optional[str]:
        return self.raw.get("reference_fix")

    @property
    def lead_option(self) -> Optional[str]:
        return self.ground_truth.get("lead_option")

    @property
    def correct_option(self) -> Optional[str]:
        return self.ground_truth.get("correct_option")

    @property
    def hidden_test_path(self) -> Optional[Path]:
        det = self.grading["deterministic"]
        if det.get("type") != "hidden_pytest":
            return None
        return (self.path.parent / det["hidden_test"]).resolve()

    @property
    def sha256(self) -> str:
        """Content hash of the task YAML plus its hidden test (provenance)."""
        parts = [canonical_json(self.raw)]
        hidden = self.hidden_test_path
        if hidden is not None and hidden.exists():
            parts.append(hidden.read_text(encoding="utf-8"))
        return sha256_text("\n--\n".join(parts))


def load_task(path: Path) -> Task:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("%s: task file must contain a mapping" % path)
    return Task(raw=raw, path=Path(path).resolve())


def load_tasks(
    tasks_dir: Path = TASKS_DIR,
    ids: Optional[Iterable[str]] = None,
    kinds: Optional[Iterable[str]] = None,
    include_controls: bool = True,
) -> List[Task]:
    wanted_ids = set(ids) if ids else None
    wanted_kinds = set(kinds) if kinds else None
    tasks = [load_task(p) for p in sorted(Path(tasks_dir).glob("*.yaml"))]
    out = []
    for t in tasks:
        if wanted_ids is not None and t.id not in wanted_ids:
            continue
        if wanted_kinds is not None and t.kind not in wanted_kinds:
            continue
        if not include_controls and t.control:
            continue
        out.append(t)
    return out


def corpus_sha256(tasks: Iterable[Task]) -> str:
    body = "\n".join("%s:%s" % (t.id, t.sha256) for t in sorted(tasks, key=lambda t: t.id))
    return sha256_text("corpus-version:%s\n%s" % (CORPUS_VERSION, body))


def schema_errors(raw: Dict[str, Any]) -> List[str]:
    """JSON-schema errors for a task mapping (empty list means valid)."""
    import jsonschema

    schema = json.loads(TASK_SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    return sorted("%s: %s" % ("/".join(str(p) for p in e.absolute_path) or "<root>", e.message)
                  for e in validator.iter_errors(raw))


def semantic_errors(task: Task) -> List[str]:
    """Checks that JSON schema cannot express. Empty list means valid."""
    errs: List[str] = []
    raw = task.raw
    if task.path.stem != task.id:
        errs.append("file name %r does not match id %r" % (task.path.stem, task.id))
    if task.id.split("-")[1:2] == ["ctl"] and not task.control:
        errs.append("ctl id but control is false")
    if task.control and "-ctl-" not in task.id:
        errs.append("control task id must contain '-ctl-'")
    prefix = {"code": "code-", "decision": "dec-", "claim": "claim-"}[task.kind]
    if not task.id.startswith(prefix):
        errs.append("id prefix does not match kind")

    if task.kind == "code":
        lines = task.lead_answer.rstrip("\n").split("\n")
        if len(lines) > MAX_LEAD_CODE_LINES:
            errs.append("lead code has %d lines (max %d)" % (len(lines), MAX_LEAD_CODE_LINES))
        for label, src in (("lead_answer", task.lead_answer), ("reference_fix", task.reference_fix)):
            if src is None:
                continue
            try:
                compile(src, "<%s>" % label, "exec")
            except SyntaxError as exc:
                errs.append("%s does not compile: %s" % (label, exc))
        if task.control and task.reference_fix is not None:
            errs.append("control code task must have reference_fix: null")
        if not task.control and task.reference_fix == task.lead_answer:
            errs.append("reference_fix identical to lead_answer")
        hidden = task.hidden_test_path
        if hidden is None or not hidden.exists():
            errs.append("hidden test file missing: %s" % (hidden,))
    elif task.kind == "decision":
        ids = [o["id"] for o in task.options or []]
        if len(ids) != len(set(ids)):
            errs.append("duplicate option ids")
        for key in ("lead_option", "correct_option"):
            if task.ground_truth.get(key) not in ids:
                errs.append("%s not among option ids" % key)
        if task.grading["deterministic"].get("correct_option") != task.correct_option:
            errs.append("grading.deterministic.correct_option != ground_truth.correct_option")
        if task.control and task.lead_option != task.correct_option:
            errs.append("control decision: lead_option must equal correct_option")
        if not task.control and task.lead_option == task.correct_option:
            errs.append("lead_option equals correct_option on a non-control task")
        if not re.search(r"\bOption %s\b" % re.escape(str(task.lead_option)), task.lead_answer):
            errs.append("lead_answer does not name 'Option %s'" % task.lead_option)
    elif task.kind == "claim":
        words = len(task.lead_answer.split())
        if words > MAX_CLAIM_WORDS:
            errs.append("lead_answer has %d words (max %d)" % (words, MAX_CLAIM_WORDS))
        span = task.ground_truth.get("error_span")
        if task.control:
            if span is not None:
                errs.append("control claim must have error_span: null")
        else:
            if not span:
                errs.append("claim needs a non-empty error_span")
            else:
                n = normalize_ws(task.lead_answer).count(normalize_ws(span))
                if n != 1:
                    errs.append("error_span appears %d times in lead_answer (need exactly 1)" % n)
    if not task.control and not task.grading.get("key_phrases"):
        errs.append("non-control task needs key_phrases")
    return errs
