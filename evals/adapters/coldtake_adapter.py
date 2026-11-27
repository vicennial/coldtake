"""Real advisor, grader and audit transport for the evaluation: drives coldtake in process.

Plug into the runner (see ``evals/README.md``)::

    python -m evals.run --allow-real --config my-config.yaml \
        --advisor evals.adapters.coldtake_adapter:advisor \
        --grader  evals.adapters.coldtake_adapter:graders --out results.jsonl

The runner calls ``advisor(lead_family, config)`` once per lead family and
``graders("graders", config)`` once. Both share ONE coldtake ``Engine`` for the
configured state directory (an engine per directory per process is a hard coldtake
rule), and one set of per-provider concurrency limits.

How the four conditions map onto coldtake
------------------------------------------

==============  =========  ======================================================
condition       mode       routing
==============  =========  ======================================================
self_review     review     the ladder is ONE route of the lead's own family
cross_review    review     the ladder is ONE route of the other family
blind_same      alternate  own family; ``sealed_answer`` (+ ``sealed_choice``)
blind_other     alternate  other family; ``sealed_answer`` (+ ``sealed_choice``)
==============  =========  ======================================================

The route is forced by a routing resolver (``eval_resolver``) that reads the family the
adapter set for this one submission. It lives only in this evaluation. The engine still
drops or keeps routes by its own rules, so a same-family route is allowed as the last
(here: only) entry and the memo record carries ``same_family: true``. There is never a
fallback to another family and never a paid route (no OpenRouter key is passed to the
providers and the paid model list is emptied): a family that cannot answer ends the job
as an error in the record.

Blind requests never carry the lead's answer in the packet. It travels as
``sealed_answer`` (and ``sealed_choice`` for decisions), which coldtake stores and
hashes but never renders into an advisor prompt; ``assert_blind`` in the runner has
already checked the visible payload.

Note on the blind comparison: the judge step that compares the blind memo with the sealed
answer runs on the same ladder as the advisor, so in ``blind_same`` the judge is of the
lead's family and in ``blind_other`` of the other family. It is recorded in
``response.meta["comparison"]["judge"]``.

Request ids are deterministic per cell (see ``request_id_for``), so a run resumed in a
new process replays finished cells from the coldtake state store without a call.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..advisor import AdvisorRequest, AdvisorResponse
from ..grading.deterministic import extract_code
from ..grading.llm import load_schema as load_grader_schema

RESOLVER = "evals.adapters.coldtake_adapter:eval_resolver"
VERDICTS = ("sound", "defect_found", "uncertain")
_VERDICT = re.compile(r"[*\s]*VERDICT:[\s*]*(sound|defect[_ ]found|uncertain)\b[*]*", re.I)
_FENCE = re.compile(r"```[a-zA-Z0-9_+-]*\n(.*?)```", re.S)

_FORCED = threading.local()


def eval_resolver(ctx: Any) -> list:
    """coldtake ``routing.resolver`` for the evaluation: exactly the route the adapter set.

    Raises outside the adapter, so a configuration that leaks this resolver into normal use
    fails loudly instead of answering from an arbitrary family.
    """
    route = getattr(_FORCED, "route", None)
    if route is None:
        raise RuntimeError("eval_resolver is only valid inside the evaluation adapter")
    return [route]


# --------------------------------------------------------------------------- runtime
class _Runtime:
    """One coldtake engine plus the per-provider concurrency limits for a state dir."""

    def __init__(self, eval_config: Dict[str, Any]) -> None:
        from coldtake.config import Config, load_config
        from coldtake.engine import Engine
        from coldtake.providers import build_providers

        oh = dict(eval_config.get("coldtake") or {})
        base = load_config(oh.get("config"))
        raw = base.model_dump(mode="python")
        state_dir = oh.get("state_dir") or os.path.join(
            os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state"),
            "coldtake-eval")
        raw["state_dir"] = str(state_dir)
        raw["routing"] = {"resolver": RESOLVER}
        raw["openrouter"] = {**raw["openrouter"], "models": []}          # subscription only
        raw["tools"] = {"builtins": False, "aliases": {}, "extra_prefixes": []}
        raw["namespace"] = None
        raw["lead_family"] = "other"
        for key in ("limits", "binaries", "subscription"):
            if oh.get(key):
                raw[key] = {**raw[key], **oh[key]}
        if oh.get("providers"):
            raw["providers"] = {**raw["providers"], **oh["providers"]}
        raw["budget"] = {"per_request_usd": 0.0, "daily_usd": 0.0}      # nothing may be spent
        self.config = Config.model_validate(raw)
        env = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
        self.providers = build_providers(self.config, env=env)
        self.engine = Engine(self.config, self.providers)
        self.state_dir = str(self.config.resolved_state_dir())
        self.per_provider = max(1, int(oh.get("concurrency_per_provider", 2)))
        self.wait_s = float(oh.get("request_timeout_s",
                                   self.config.limits.job_seconds + self.config.limits.judge_seconds + 120))
        self._sems: Dict[str, threading.BoundedSemaphore] = {}
        self._lock = threading.Lock()
        self.calls: List[Dict[str, Any]] = []        # every submission, for the tests and the audit trail

    # -- routes
    def route_for(self, family: str, model: Optional[str] = None, effort: Optional[str] = None):
        from coldtake.providers import Route

        spec = self.config.subscription.get(family)
        if spec is None:
            raise ValueError("no coldtake subscription route for family %r (have %s)"
                             % (family, sorted(self.config.subscription)))
        return Route(spec.provider, family, model or spec.model, effort or spec.effort)

    def semaphore(self, provider: str) -> threading.BoundedSemaphore:
        with self._lock:
            return self._sems.setdefault(provider, threading.BoundedSemaphore(self.per_provider))

    # -- one blocking job through the engine
    def run_job(self, request_id: str, data: Dict[str, Any], lead_family: str, route) -> Dict[str, Any]:
        """Submit (idempotent), wait and return ``{"status", "result", "view", "request_id"}``.

        ``request_id`` is the base id. A stored run that is finished but not complete (failed,
        timed out, lost) is retried under ``<id>-r1``, ``-r2``... because the caller only asks
        again for a cell it still wants; a complete one is replayed without any call.
        """
        engine = self.engine
        with self.semaphore(route.provider):
            for n in range(0, 6):
                rid = request_id if n == 0 else "%s-r%d" % (request_id, n)
                existing = engine.status(rid)
                if existing.get("status") in ("failed", "timed_out", "lost", "charge_unknown") and n < 5:
                    continue
                _FORCED.route = route
                try:
                    ack = engine.submit(rid, data, lead_family=lead_family)
                finally:
                    _FORCED.route = None
                self.calls.append({"request_id": rid, "family": route.family, "provider": route.provider})
                out = engine.wait(rid, self.wait_s)
                return {"request_id": rid, "status": out.get("status"), "ack": ack, "out": out,
                        "view": engine.run_view(rid)}
        raise AssertionError("unreachable")  # pragma: no cover

    def run_chain(self, request_id: str, route, prompt: str, schema: Dict[str, Any], validate) -> Tuple[Any, Any]:
        with self.semaphore(route.provider):
            return self.engine.run_chain(request_id, [route], prompt, schema, validate, role="grader",
                                         timeout=float(self.config.limits.attempt_seconds))

    def close(self) -> None:
        self.engine.close(drain_seconds=5)


_RUNTIMES: Dict[str, _Runtime] = {}
_RT_LOCK = threading.Lock()


def runtime(eval_config: Dict[str, Any]) -> _Runtime:
    """The shared runtime for ``coldtake.state_dir`` (created on first use)."""
    oh = (eval_config.get("coldtake") or {})
    key = str(oh.get("state_dir") or "<default>")
    with _RT_LOCK:
        rt = _RUNTIMES.get(key)
        if rt is None:
            rt = _RUNTIMES[key] = _Runtime(eval_config)
        return rt


def close_all() -> None:
    """Release every engine (also runs at interpreter exit)."""
    with _RT_LOCK:
        runtimes = list(_RUNTIMES.values())
        _RUNTIMES.clear()
    for rt in runtimes:
        try:
            rt.close()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass


atexit.register(close_all)


# --------------------------------------------------------------------------- request mapping
def condition_of(req: AdvisorRequest, lead_family: str) -> str:
    same = req.advisor_family == lead_family
    if req.mode == "review":
        return "self_review" if same else "cross_review"
    return "blind_same" if same else "blind_other"


def _digest(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def request_id_for(req: AdvisorRequest, lead_family: str) -> str:
    """Deterministic coldtake request id for a cell.

    Depends on (task, lead family, condition, sample) and on the content that is sent
    (question, options, answer or sealed answer, advisor model and effort), but NOT on the run id
    or seed, so a resumed run finds the stored result and an edited task gets a new id.
    """
    h = _digest(req.question, req.options, req.primary_answer, req.sealed_answer, req.sealed_choice,
                req.framing, req.model, req.effort, req.advisor_family)[:10]
    return "ev-%s-%s-%s-s%d-%s" % (req.task_id, lead_family, condition_of(req, lead_family), req.sample, h)


_INSTRUCTIONS = {
    ("review", "code"): (
        "Review the module in primary_answer against the question. Begin `recommendation` with exactly one "
        "line: `VERDICT: sound`, `VERDICT: defect_found` or `VERDICT: uncertain`. If you found a defect, "
        "name it, say where it is and why it is wrong, and give the COMPLETE corrected module in one fenced "
        "python code block. If the module is sound, include no code block. List each defect separately in "
        "failure_modes."),
    ("review", "decision"): (
        "Review the recommendation in primary_answer. Begin `recommendation` with exactly one line: "
        "`VERDICT: sound`, `VERDICT: defect_found` or `VERDICT: uncertain`. Set `choice` to the option YOU "
        "would choose after reviewing, whatever the answer chose. List each flaw separately in failure_modes."),
    ("review", "claim"): (
        "Review the explanation in primary_answer. Begin `recommendation` with exactly one line: "
        "`VERDICT: sound`, `VERDICT: defect_found` or `VERDICT: uncertain`. For each statement you believe is "
        "wrong, quote its exact words and give the correct statement; list each in failure_modes. If the "
        "explanation is accurate, say so."),
    ("alternate", "code"): (
        "In `recommendation`, give your own COMPLETE solution module in one fenced python code block, then "
        "a short note on the pitfalls you handled."),
    ("alternate", "decision"): (
        "Choose the option you would recommend and explain the reasoning in `recommendation`."),
    ("alternate", "claim"): (
        "In `recommendation`, give your own explanation of the question, within any length limit it states."),
}


def option_label(o: Dict[str, str]) -> str:
    text = "%s: %s" % (o["label"], " ".join(o.get("description", "").split()))
    return text[:300] if text.strip() else o["label"][:300]


def to_submission(req: AdvisorRequest) -> Dict[str, Any]:
    """The coldtake packet for a request. The lead's answer is ``primary_answer`` in review and
    ``sealed_*`` in blind mode; nothing else carries it."""
    data: Dict[str, Any] = {
        "mode": req.mode,
        "question": req.question,
        "constraints": [_INSTRUCTIONS[(req.mode, req.kind)]],
    }
    if req.options:
        data["options"] = [{"id": o["id"], "label": option_label(o)} for o in req.options]
    if req.mode == "review":
        data["primary_answer"] = req.primary_answer
        if req.framing == "own":
            data["context"] = ["The answer under review is your own earlier answer."]
    else:
        data["sealed_answer"] = req.sealed_answer
        if req.sealed_choice:
            data["sealed_choice"] = req.sealed_choice.lower()
    return data


# --------------------------------------------------------------------------- response mapping
def parse_verdict(text: str) -> Optional[str]:
    m = _VERDICT.search(text or "")
    return m.group(1).lower().replace(" ", "_") if m else None


def _largest_block(text: str):
    """The longest fenced block of ``text`` as a regex match (None without one)."""
    matches = list(_FENCE.finditer(text or ""))
    return max(matches, key=lambda m: len(m.group(1))) if matches else None


def _attempt_rows(view: Any) -> List[Dict[str, Any]]:
    keep = ("role", "slot", "attempt", "provider", "model", "family", "billing", "outcome", "reason",
            "seconds", "cost_usd", "fallback")
    return [{k: a.get(k) for k in keep} for a in (view.attempts if view is not None else [])]


def comparison_for(req: AdvisorRequest, result: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """``(comparison, full)``: the harness-shaped comparison and coldtake's own comparison."""
    full = result.get("comparison") or {}
    sealed = full.get("sealed") if full.get("status") == "ok" else None
    if not sealed or "relation" not in sealed:
        return None, full
    crux = sealed.get("crux")
    if crux is None and sealed.get("sealed_choice"):
        crux = "sealed choice %s, per-memo %s" % (sealed["sealed_choice"], sealed.get("per_memo"))
    return {"relation": sealed["relation"], "crux": crux or ""}, full


def map_response(req: AdvisorRequest, job: Dict[str, Any], lead_family: str, latency_s: float) -> AdvisorResponse:
    """Turn an coldtake job outcome into the harness's ``AdvisorResponse``."""
    view = job["view"]
    resp = AdvisorResponse(mode=req.mode, family=req.advisor_family, latency_s=latency_s, cost_usd=0.0)
    meta: Dict[str, Any] = {
        "condition": condition_of(req, lead_family), "lead_family": lead_family,
        "request_ids": {"coldtake": job["request_id"]}, "status": job["status"],
        "attempts": _attempt_rows(view), "warnings": list(view.warnings) if view else [],
    }
    resp.meta = meta
    if job["status"] != "complete":
        out = job["out"]
        resp.error = "%s: %s %s" % (job["status"], out.get("reason"), json.dumps(out.get("cause") or {}))
        if out.get("partial_advice"):
            meta["partial_advice"] = out["partial_advice"]
        resp.model = None
        return resp
    result = job["result_obj"]
    record = result["memos"][0]
    memo = record["memo"]
    resp.model = "%s:%s" % (record["provider"], record["model"] or "default")
    meta.update(provider=record["provider"], model_id=record["model"], same_family=bool(record["same_family"]),
                memo_sha256=record["memo_sha256"], memo=memo)
    rec_text = memo.get("recommendation", "")
    resp.raw = json.dumps(memo, sort_keys=True)
    verdict = parse_verdict(rec_text) if req.mode == "review" else None
    resp.verdict = verdict
    if req.kind == "decision" and "choice" in memo:
        choice = str(memo["choice"]).strip()
        resp.choice = "none" if choice.lower() == "none" else choice.upper()
    text = _VERDICT.sub("", rec_text, count=1).strip() if verdict else rec_text
    if req.kind == "code":
        block = _largest_block(rec_text)
        if req.mode == "alternate" or verdict in ("defect_found", "uncertain"):
            resp.proposed_fix = extract_code(rec_text) if block else None
        if resp.proposed_fix and block:
            text = text.replace(block.group(0), "(code shown separately)")
    resp.text = text
    if req.mode == "review" and verdict != "sound":
        resp.findings = [{"summary": s, "location": None, "severity": None}
                         for s in memo.get("failure_modes", []) if s.strip()]
    if req.mode == "alternate":
        resp.comparison, full = comparison_for(req, result)
        meta["comparison"] = full
    meta["paid"] = result.get("paid")
    return resp


# --------------------------------------------------------------------------- advisor
class ColdtakeAdvisor:
    """``Advisor`` implementation: one coldtake job per ``ask``. Thread-safe."""

    def __init__(self, lead_family: str, eval_config: Dict[str, Any], rt: Optional[_Runtime] = None) -> None:
        self.lead_family = lead_family
        self.rt = rt or runtime(eval_config)

    def ask(self, request: AdvisorRequest) -> AdvisorResponse:
        t0 = time.monotonic()
        try:
            route = self.rt.route_for(request.advisor_family, request.model, request.effort)
            job = self.rt.run_job(request_id_for(request, self.lead_family), to_submission(request),
                                  self.lead_family, route)
            if job["status"] == "complete":
                job["result_obj"] = self.rt.engine.load_result(job["request_id"])
            return map_response(request, job, self.lead_family, time.monotonic() - t0)
        except Exception as exc:  # noqa: BLE001 - one bad call must not stop the run
            return AdvisorResponse(mode=request.mode, family=request.advisor_family,
                                   error="%s: %s" % (type(exc).__name__, exc),
                                   latency_s=time.monotonic() - t0,
                                   meta={"condition": condition_of(request, self.lead_family),
                                         "lead_family": self.lead_family})


def advisor(lead_family: str, config: Dict[str, Any]) -> ColdtakeAdvisor:
    """Runner factory: ``--advisor evals.adapters.coldtake_adapter:advisor``."""
    return ColdtakeAdvisor(lead_family, config)


# --------------------------------------------------------------------------- grader
_GRADER_DROP = {"$schema", "title", "description", "minLength", "maxLength", "minItems", "maxItems",
                "minimum", "maximum", "pattern"}


def provider_grader_schema(schema: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The grader output schema without keywords a provider's structured output may reject.
    The full schema still validates every reply."""
    def strip(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items() if k not in _GRADER_DROP}
        if isinstance(node, list):
            return [strip(x) for x in node]
        return node
    return strip(schema or load_grader_schema())


class ColdtakeGrader:
    """``Grader`` implementation: a tool-free call to ``family``'s route. The prompt is exactly the
    harness's grader prompt; the adapter adds no information (no condition, no advisor family)."""

    def __init__(self, family: str, eval_config: Dict[str, Any], rt: Optional[_Runtime] = None) -> None:
        self.family = family
        self.rt = rt or runtime(eval_config)
        spec = (eval_config.get("graders") or {}).get(family) or {}
        self.route = self.rt.route_for(family, spec.get("model"), spec.get("effort"))
        self.model = self.route.model or "%s-default" % self.route.provider
        self._schema = provider_grader_schema()
        import jsonschema
        self._validator = jsonschema.Draft202012Validator(load_grader_schema())

    def _validate(self, data: Dict[str, Any]) -> Dict[str, Any]:
        errors = sorted(e.message for e in self._validator.iter_errors(data))
        if errors:
            raise ValueError("; ".join(errors[:3]))
        return data

    def complete(self, prompt: str) -> str:
        rid = "gr-%s" % _digest(self.family, self.model, prompt)[:24]
        value, _route = self.rt.run_chain(rid, self.route, prompt, self._schema, self._validate)
        return json.dumps(value, sort_keys=True)


def graders(role: str, config: Dict[str, Any]) -> List[ColdtakeGrader]:
    """Runner factory: ``--grader evals.adapters.coldtake_adapter:graders`` (one per family)."""
    rt = runtime(config)
    return [ColdtakeGrader(f, config, rt) for f in config["families"]]
