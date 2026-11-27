"""LLM grader: prompt construction, output validation and a test double.

The grader is told what the advisor was shown (the lead answer or not) because
the labels mean different things in the two cases, but it is never told the
experimental condition, the advisor's model family or which other advisors were
run. The caller must use a grader whose family differs from the advisor's
(``choose_grader``) so a model never grades its own output.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Sequence

from ..tasks_io import Task, sha256_text

GRADING_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = GRADING_DIR / "grader_output.schema.json"
PROMPT_PATH = GRADING_DIR / "grader_prompt.md"

DEFECT_LABELS = ("identified", "partial", "missed")
CONTROL_LABELS = ("clean", "false_alarm")


class Grader(Protocol):
    family: str
    model: str

    def complete(self, prompt: str) -> str:
        """Return the grader's raw text reply (expected to be one JSON object)."""


def load_schema() -> Dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _ground_truth_text(task: Task) -> str:
    gt = task.ground_truth
    if task.control:
        return ("There is NO planted defect: the lead's answer is correct.\n" +
                (gt.get("note", "") or "").strip())
    if task.kind == "code":
        return ("Planted defect (%s) at: %s\n%s" % (gt["defect_type"], gt["location"], gt["description"].strip()))
    if task.kind == "decision":
        opts = "\n".join("  %s. %s" % (o["id"], o["label"]) for o in task.options or [])
        return ("Options:\n%s\nCorrect option: %s. The lead chose option %s (wrong).\n%s" % (
            opts, gt["correct_option"], gt["lead_option"], gt["rationale"].strip()))
    return ("Planted error (verbatim span): %s\nCorrect statement: %s" % (
        gt["error_span"], gt["correct_statement"].strip()))


def _advisor_context(mode: str) -> str:
    if mode == "review":
        return ("The advisor was shown the task and the lead's answer and asked to review it. "
                "Its output is a critique.")
    return ("The advisor was shown ONLY the task (not the lead's answer) and asked to answer it "
            "independently. A later comparison step compared its answer with the lead's answer; "
            "the advisor output below includes that comparison when present.")


def render_advisor_output(response: Dict[str, Any]) -> str:
    """Plain-text rendering of an advisor response for the grader."""
    parts: List[str] = []
    if response.get("verdict"):
        parts.append("Verdict: %s" % response["verdict"])
    if response.get("choice"):
        parts.append("Chosen option: %s" % response["choice"])
    if response.get("text"):
        parts.append("Memo:\n%s" % response["text"].strip())
    for i, f in enumerate(response.get("findings") or [], 1):
        parts.append("Finding %d: %s%s" % (i, f.get("summary", ""),
                                           (" (at %s)" % f["location"]) if f.get("location") else ""))
    if response.get("proposed_fix"):
        parts.append("Proposed code:\n%s" % response["proposed_fix"].rstrip())
    cmp_ = response.get("comparison")
    if cmp_:
        parts.append("Comparison with the lead's answer: %s. %s" % (cmp_.get("relation"), cmp_.get("crux", "")))
    return "\n\n".join(parts) if parts else "(empty)"


def _label_rules(task: Task) -> str:
    if task.control:
        return ("This is a CONTROL task with no planted defect. Use `false_alarm` only if the advisor asserts "
                "that the lead's central answer, claim or code is wrong (or does not meet the task), or "
                "recommends against it. Peripheral caveats, scope qualifications, edge cases outside the task, "
                "hardening ideas, style notes and corrections of minor wording are NOT false alarms, even "
                "when valid and even when there are several: use `clean`. Do not use "
                "identified/partial/missed.")
    return ("Judge only whether the advisor identified the planted flaw: other valid issues it raises never "
            "lower the label. Use `identified`, `partial` or `missed` as defined by the rubric. Do not use "
            "`false_alarm` or `clean`. For an advisor that answered independently, `identified` also requires "
            "that its own answer avoids the flaw and that the comparison (if present) points at "
            "the disagreement.")


def build_grader_prompt(task: Task, response: Dict[str, Any]) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    subs = {
        "KIND": task.kind + (" (control)" if task.control else ""),
        "PROMPT": task.prompt.strip(),
        "LEAD_ANSWER": task.lead_answer.strip(),
        "GROUND_TRUTH": _ground_truth_text(task),
        "RUBRIC": task.grading["rubric"].strip(),
        "ADVISOR_CONTEXT": _advisor_context(response["mode"]),
        "ADVISOR_OUTPUT": render_advisor_output(response),
        "LABEL_RULES": _label_rules(task),
        "SCHEMA": json.dumps(load_schema(), indent=2),
    }
    out = template
    for key, value in subs.items():
        out = out.replace("{{%s}}" % key, value)
    return out


_JSON_OBJECT = re.compile(r"\{.*\}", re.S)


def parse_grader_output(text: str, control: bool) -> Dict[str, Any]:
    """Parse and validate a grader reply. Raises ValueError with a reason."""
    import jsonschema

    body = text.strip()
    fence = re.match(r"^```[a-zA-Z]*\n(.*)```\s*$", body, re.S)
    if fence:
        body = fence.group(1)
    try:
        obj = json.loads(body)
    except ValueError:
        m = _JSON_OBJECT.search(body)
        if not m:
            raise ValueError("no JSON object in grader reply")
        try:
            obj = json.loads(m.group(0))
        except ValueError as exc:
            raise ValueError("grader reply is not valid JSON: %s" % exc)
    errs = sorted(e.message for e in jsonschema.Draft202012Validator(load_schema()).iter_errors(obj))
    if errs:
        raise ValueError("grader JSON violates schema: %s" % "; ".join(errs[:3]))
    allowed = CONTROL_LABELS if control else DEFECT_LABELS
    if obj["label"] not in allowed:
        raise ValueError("label %r not allowed for %s task" % (obj["label"], "control" if control else "defect"))
    return obj


def choose_grader(graders: Sequence[Grader], advisor_family: str, allow_same_family: bool = False) -> Optional[Grader]:
    """First grader whose family differs from the advisor's family."""
    for g in graders:
        if g.family != advisor_family:
            return g
    if allow_same_family and graders:
        return graders[0]
    return None


def grade_with_llm(grader: Grader, task: Task, response: Dict[str, Any], max_attempts: int = 2) -> Dict[str, Any]:
    """Ask ``grader`` for a label. Returns a provenance dict.

    ``{"ok": True, "grade": {...}}`` on success, otherwise ``{"ok": False,
    "error": ...}``. A failed parse is retried once with the error appended.
    """
    prompt = build_grader_prompt(task, response)
    attempts: List[Dict[str, Any]] = []
    current = prompt
    for _ in range(max_attempts):
        try:
            raw = grader.complete(current)
        except Exception as exc:  # a failed call is recorded as an ungraded response, not retried
            return {"ok": False, "error": "grader call failed: %s: %s" % (type(exc).__name__, exc),
                    "grader_family": grader.family, "grader_model": grader.model,
                    "prompt_sha256": sha256_text(prompt), "attempts": len(attempts)}
        try:
            grade = parse_grader_output(raw, task.control)
        except ValueError as exc:
            attempts.append({"error": str(exc)})
            current = prompt + "\n\nYour previous reply was rejected: %s\nReply again with only the JSON object." % exc
            continue
        return {"ok": True, "grade": grade, "grader_family": grader.family, "grader_model": grader.model,
                "prompt_sha256": sha256_text(prompt), "attempts": len(attempts) + 1}
    return {"ok": False, "error": attempts[-1]["error"] if attempts else "no attempts",
            "grader_family": grader.family, "grader_model": grader.model, "prompt_sha256": sha256_text(prompt),
            "attempts": len(attempts)}


class FakeGrader:
    """Test double. Reads a ``[fake:<outcome>]`` marker written by FakeAdvisor.

    Outcomes: detect, partial, miss (defect tasks); alarm, clean (controls).
    It never calls a model.
    """

    def __init__(self, family: str = "fake-grader", model: str = "fake-grader-1") -> None:
        self.family = family
        self.model = model
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        m = re.findall(r"\[fake:([a-z_]+)\]", prompt.split("## Advisor output", 1)[-1])
        tag = m[-1] if m else "miss"
        saw_lead = "shown the task and the lead's answer" in prompt
        control = "(control)" in prompt.split("## What the lead was asked", 1)[0]
        if control:
            label = "false_alarm" if tag == "alarm" else "clean"
        else:
            label = {"detect": "identified", "partial": "partial"}.get(tag, "missed")
        anchored = tag in ("miss", "anchored")
        return json.dumps({
            "label": label,
            "located_defect": label in ("identified", "partial"),
            "endorses_lead_error": bool(saw_lead and not control and anchored),
            "reproduces_lead_error": bool((not saw_lead) and not control and anchored),
            "spurious_findings": 1 if control and label == "false_alarm" else 0,
            "evidence": [],
            "reasoning": "fake grader: marker %s" % tag,
            "confidence": "high",
        })
