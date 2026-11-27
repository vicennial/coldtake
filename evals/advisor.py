"""Advisor interface, request builder and a fake advisor for tests.

The harness never talks to a model directly. It builds an ``AdvisorRequest``
and hands it to something implementing ``Advisor``. The real implementation
(wired to coldtake) is added later under the root's control.
"""
from __future__ import annotations

import hashlib
import random
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Protocol

from .tasks_io import Task, canonical_json, normalize_ws, sha256_text

MODES = ("review", "alternate")
FRAMINGS = ("neutral", "own")

OUTPUT_CONTRACT = {
    ("review", "code"): "Return verdict (sound|defect_found|uncertain), findings, and proposed_fix: the complete corrected module if you found a defect, otherwise null.",
    ("review", "decision"): "Return verdict, findings, and choice: the option id (A-D) YOU would choose after reviewing, or 'none'.",
    ("review", "claim"): "Return verdict, findings (quote the exact words of any statement you believe is wrong and give the correct statement), and text.",
    ("alternate", "code"): "Return proposed_fix: your own complete solution module, and text: a short memo of the pitfalls you handled.",
    ("alternate", "decision"): "Return choice: the option id (A-D) you recommend, or 'none', and text: your reasoning.",
    ("alternate", "claim"): "Return text: your own explanation of at most 250 words.",
}


@dataclass
class AdvisorRequest:
    """One advisor call. ``sealed_*`` fields are router-only and never rendered."""

    request_id: str
    task_id: str
    kind: str
    mode: str                      # review | alternate
    question: str
    options: Optional[List[Dict[str, str]]]
    primary_answer: Optional[str]  # review only
    framing: str                   # neutral | own (review only)
    output_contract: str
    advisor_family: str
    model: Optional[str]
    effort: Optional[str]
    seed: int
    sample: int
    sealed_answer: Optional[str] = None   # alternate only
    sealed_choice: Optional[str] = None   # alternate + decision only

    def visible_payload(self) -> Dict[str, Any]:
        """Everything an advisor prompt may contain. Excludes ``sealed_*``."""
        payload: Dict[str, Any] = {
            "mode": self.mode,
            "question": self.question,
            "output_contract": self.output_contract,
        }
        if self.options:
            payload["options"] = self.options
        if self.primary_answer is not None:
            payload["primary_answer"] = self.primary_answer
            payload["framing"] = self.framing
        return payload

    def sha256(self) -> str:
        return sha256_text(canonical_json(asdict(self)))

    def provenance(self) -> Dict[str, Any]:
        """Record for the results file. The sealed text itself is not copied."""
        d = asdict(self)
        sealed = d.pop("sealed_answer")
        d["sealed_sha256"] = sha256_text(sealed) if sealed is not None else None
        d["sha256"] = self.sha256()
        return d


@dataclass
class AdvisorResponse:
    mode: str
    family: str
    model: Optional[str] = None
    text: str = ""
    verdict: Optional[str] = None
    choice: Optional[str] = None
    findings: List[Dict[str, Any]] = field(default_factory=list)
    proposed_fix: Optional[str] = None
    comparison: Optional[Dict[str, Any]] = None
    latency_s: Optional[float] = None
    cost_usd: Optional[float] = None
    raw: Optional[str] = None
    error: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class Advisor(Protocol):
    """Anything that can answer an ``AdvisorRequest``.

    Implementations must honour blindness: for ``mode == "alternate"`` only
    ``request.visible_payload()`` may reach the model. Failures are returned as
    ``AdvisorResponse(error=...)``, not raised, so one bad call does not stop a
    run.
    """

    def ask(self, request: AdvisorRequest) -> AdvisorResponse:
        ...


def longest_shared_word_span(a: str, b: str, cap: int = 60) -> int:
    """Length (in words) of the longest run of consecutive words common to a and b."""
    wa = re.findall(r"\S+", a.lower())
    wb = re.findall(r"\S+", b.lower())
    if not wa or not wb:
        return 0
    index: Dict[tuple, List[int]] = {}
    k = 6
    for i in range(len(wb) - k + 1):
        index.setdefault(tuple(wb[i:i + k]), []).append(i)
    best = 0
    for i in range(len(wa) - k + 1):
        for j in index.get(tuple(wa[i:i + k]), ()):
            n = k
            while i + n < len(wa) and j + n < len(wb) and wa[i + n] == wb[j + n] and n < cap:
                n += 1
            best = max(best, n)
    return best


class BlindnessViolation(AssertionError):
    pass


def assert_blind(request: AdvisorRequest, task: Task, max_shared_words: int = 12) -> None:
    """Fail closed if a blind request could leak the lead answer.

    Checks the rendered payload for the whole lead answer and for any span of
    ``max_shared_words`` or more consecutive words shared with it.
    """
    if request.mode != "alternate":
        return
    if request.primary_answer is not None:
        raise BlindnessViolation("blind request carries primary_answer")
    visible = canonical_json(request.visible_payload())
    if normalize_ws(task.lead_answer) and normalize_ws(task.lead_answer) in normalize_ws(visible):
        raise BlindnessViolation("blind payload contains the whole lead answer")
    span = longest_shared_word_span(visible, task.lead_answer)
    if span >= max_shared_words:
        raise BlindnessViolation("blind payload shares a %d-word span with the lead answer" % span)


def build_request(
    task: Task,
    mode: str,
    advisor_family: str,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    framing: str = "neutral",
    seed: int = 0,
    sample: int = 0,
    run_id: str = "run",
) -> AdvisorRequest:
    if mode not in MODES:
        raise ValueError("unknown mode %r" % mode)
    if framing not in FRAMINGS:
        raise ValueError("unknown framing %r" % framing)
    key = "%s|%s|%s|%s|%d|%d" % (run_id, task.id, mode, advisor_family, seed, sample)
    request_id = "eval-" + hashlib.sha256(key.encode()).hexdigest()[:20]
    review = mode == "review"
    return AdvisorRequest(
        request_id=request_id,
        task_id=task.id,
        kind=task.kind,
        mode=mode,
        question=task.prompt.strip(),
        options=task.options,
        primary_answer=task.lead_answer.strip() if review else None,
        framing=framing if review else "neutral",
        output_contract=OUTPUT_CONTRACT[(mode, task.kind)],
        advisor_family=advisor_family,
        model=model,
        effort=effort,
        seed=seed,
        sample=sample,
        sealed_answer=None if review else task.lead_answer.strip(),
        sealed_choice=None if (review or task.kind != "decision") else task.lead_option,
    )


class FakeAdvisor:
    """Deterministic test double. Calls no model.

    ``profile`` maps a condition name (``self_review``, ``cross_review``,
    ``blind_same``, ``blind_other``) or ``"*"`` to probabilities:

    * ``p_detect``: on a defect task, chance the advisor finds the planted defect
    * ``p_partial``: of the remaining mass, chance of a vague "partial" answer
    * ``p_alarm``: on a control task, chance of a false alarm

    It must be told the task set (it cheats by reading ground truth, which is
    the point of a test double) and the condition of each request, which it
    infers from ``(mode, advisor_family == lead_family)`` via ``lead_family``.
    Responses carry a ``[fake:<tag>]`` marker that ``FakeGrader`` reads.
    """

    def __init__(self, tasks: List[Task], lead_family: str = "anthropic",
                 profile: Optional[Dict[str, Dict[str, float]]] = None, seed: int = 0) -> None:
        self.tasks = {t.id: t for t in tasks}
        self.lead_family = lead_family
        self.profile = profile or {"*": {"p_detect": 1.0, "p_partial": 0.0, "p_alarm": 0.0}}
        self.seed = seed
        self.calls: List[AdvisorRequest] = []
        self.visible_seen: List[Dict[str, Any]] = []

    def _condition(self, req: AdvisorRequest) -> str:
        same = req.advisor_family == self.lead_family
        if req.mode == "review":
            return "self_review" if same else "cross_review"
        return "blind_same" if same else "blind_other"

    def _params(self, cond: str) -> Dict[str, float]:
        base = {"p_detect": 0.0, "p_partial": 0.0, "p_alarm": 0.0}
        base.update(self.profile.get("*", {}))
        base.update(self.profile.get(cond, {}))
        return base

    def ask(self, request: AdvisorRequest) -> AdvisorResponse:
        self.calls.append(request)
        self.visible_seen.append(request.visible_payload())  # all the "model" may use
        task = self.tasks[request.task_id]
        cond = self._condition(request)
        p = self._params(cond)
        rng = random.Random("%s|%s|%s|%s" % (self.seed, request.task_id, cond, request.sample))
        u = rng.random()
        r = AdvisorResponse(mode=request.mode, family=request.advisor_family,
                            model=request.model or ("fake-" + request.advisor_family), latency_s=0.0, cost_usd=0.0)
        if task.control:
            tag = "alarm" if u < p["p_alarm"] else "clean"
        elif u < p["p_detect"]:
            tag = "detect"
        elif u < p["p_detect"] + p["p_partial"]:
            tag = "partial"
        else:
            tag = "miss"
        r.meta = {"fake_tag": tag, "condition": cond}
        r.text = "[fake:%s] simulated advisor memo for %s." % (tag, task.id)
        self._fill(task, request, r, tag)
        r.raw = r.text
        return r

    def _fill(self, task: Task, req: AdvisorRequest, r: AdvisorResponse, tag: str) -> None:
        review = req.mode == "review"
        right = tag in ("detect", "clean")      # advisor's own answer is correct
        if review:
            r.verdict = "defect_found" if tag in ("detect", "partial", "alarm") else "sound"
        if task.kind == "decision":
            if right and not task.control:
                r.choice = task.correct_option
            elif tag == "partial":
                r.choice = next(o["id"] for o in task.options if o["id"] not in (task.lead_option, task.correct_option))
            elif task.control:
                r.choice = task.lead_option if tag == "clean" else next(
                    o["id"] for o in task.options if o["id"] != task.lead_option)
            else:
                r.choice = task.lead_option
        elif task.kind == "code":
            fixed = right and not task.control and task.reference_fix
            r.proposed_fix = task.reference_fix if fixed else task.lead_answer
            if review and r.verdict == "sound":
                r.proposed_fix = None
        if not review and task.kind != "decision":
            relation = {"detect": "conflict", "partial": "partial", "miss": "agree",
                        "clean": "agree", "alarm": "conflict"}[tag]
            r.comparison = {"relation": relation, "crux": "simulated crux (%s)" % tag}
        if review and tag in ("detect", "partial", "alarm"):
            r.findings = [{"summary": "simulated finding (%s)" % tag, "location": None, "severity": "major"}]
