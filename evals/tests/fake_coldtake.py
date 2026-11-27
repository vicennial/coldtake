"""In-process fake providers for the adapter tests. They stand in for the ``claude`` and ``codex``
CLIs by replacing the built-in providers through coldtake's ``providers`` config mapping, so the
real engine, planner, store, prompts and validation all run. Nothing here calls a model.

Module state (``LOG``, ``BEHAVIOUR``) is process-wide: tests reset it with ``reset()``.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from coldtake.failures import ProviderFailure
from coldtake.providers import Completion

LOCK = threading.Lock()
LOG: List[Dict[str, Any]] = []
# optional hooks; each returns reply data, or None for the default, or raises ProviderFailure
BEHAVIOUR: Dict[str, Optional[Callable[..., Any]]] = {"advisor": None, "judge": None, "grader": None}
DELAY = {"seconds": 0.0}
INFLIGHT: Dict[str, int] = {}
PEAK: Dict[str, int] = {}


def reset() -> None:
    with LOCK:
        LOG.clear()
        INFLIGHT.clear()
        PEAK.clear()
        DELAY["seconds"] = 0.0
        for k in BEHAVIOUR:
            BEHAVIOUR[k] = None


def calls(role: Optional[str] = None, provider: Optional[str] = None) -> List[Dict[str, Any]]:
    with LOCK:
        return [c for c in LOG if (role is None or c["role"] == role) and (provider is None or c["provider"] == provider)]


def role_of(prompt: str) -> str:
    if "COLDTAKE_ROLE: judge" in prompt:
        return "judge"
    if "You are grading the output of an advisor" in prompt:
        return "grader"
    return "advisor"


def _request(prompt: str) -> Dict[str, Any]:
    body = prompt.split('<REQUEST_JSON untrusted="true">\n')[1].split("\n</REQUEST_JSON>")[0]
    return json.loads(body)


def memo(recommendation: str, **extra: Any) -> Dict[str, Any]:
    base = {"framing": "f", "recommendation": recommendation, "best_alternative": "alt",
            "evidence": ["e"], "assumptions": ["a"], "value_tradeoffs": ["v"], "forecasts": ["fc"],
            "feasibility": ["fe"], "missing_information": [], "failure_modes": ["fm"],
            "reversal_evidence": ["r"], "confidence": 0.5}
    base.update(extra)
    return base


def default_advisor(prompt: str) -> Dict[str, Any]:
    req = _request(prompt)
    review = req["mode"] == "review"
    head = "VERDICT: defect_found\n" if review else ""
    # No provider or family name in the text: the grader prompt for the same answer must not differ.
    body = head + "simulated advisor reasoning\n```python\nx = 1\n```\n"
    if req.get("options"):
        return memo(body, choice=req["options"][0]["id"], choice_reason="because")
    return memo(body)


def default_judge(prompt: str, sealed_relation: str = "conflict") -> Dict[str, Any]:
    body = prompt.split('<COMPARE_JSON untrusted="true">')[1].split("</COMPARE_JSON>")[0]
    positions = json.loads(body)["positions"]
    labels = sorted(k for k in positions if k != "S")
    pairs = [{"a": a, "b": b, "relation": "agree", "crux": "c"} for i, a in enumerate(labels) for b in labels[i + 1:]]
    stances = [{"label": x, "stance": "stance " + x} for x in labels]
    sealed = None
    if "S" in positions:
        pairs += [{"a": x, "b": "S", "relation": sealed_relation, "crux": "simulated crux"} for x in labels]
        sealed = {"relation": sealed_relation, "crux": "simulated crux"}
        stances.append({"label": "S", "stance": "sealed"})
    return {"positions": stances, "clusters": [labels], "pairwise": pairs, "sealed": sealed,
            "contradictions": [], "material": False}


def default_grader(prompt: str) -> Dict[str, Any]:
    control = "(control)" in prompt.split("## What the lead was asked", 1)[0]
    return {"label": "clean" if control else "identified", "located_defect": not control,
            "endorses_lead_error": False, "reproduces_lead_error": False, "spurious_findings": 0,
            "evidence": [], "reasoning": "fake grader", "confidence": "high"}


class _Fake:
    name = ""

    def __init__(self, config: Any, env: Any) -> None:
        pass

    def available(self) -> bool:
        return True

    def complete(self, prompt: str, schema: Dict[str, Any], route: Any, timeout: float) -> Completion:
        role = role_of(prompt)
        key = self.name
        with LOCK:
            LOG.append({"role": role, "provider": self.name, "family": route.family, "model": route.model,
                        "effort": route.effort, "prompt": prompt, "schema": schema})
            INFLIGHT[key] = INFLIGHT.get(key, 0) + 1
            PEAK[key] = max(PEAK.get(key, 0), INFLIGHT[key])
        try:
            if DELAY["seconds"]:
                time.sleep(DELAY["seconds"])
            hook = BEHAVIOUR.get(role)
            data = hook(prompt, route) if hook else None
            if data is None:
                data = {"advisor": default_advisor, "judge": default_judge, "grader": default_grader}[role](prompt)
            return Completion(data)
        finally:
            with LOCK:
                INFLIGHT[key] -= 1


class FakeClaude(_Fake):
    name = "claude"


class FakeCodex(_Fake):
    name = "codex"


__all__ = ["FakeClaude", "FakeCodex", "ProviderFailure", "reset", "calls", "memo", "BEHAVIOUR", "DELAY", "PEAK"]
