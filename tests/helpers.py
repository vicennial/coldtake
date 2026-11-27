"""In-process providers and a resolver hook for tests that need to drive the engine's
route plans directly. Nothing here touches a real CLI, network or home directory."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any

from coldtake.failures import ProviderFailure
from coldtake.providers import Completion, Route, worst_case_cost

from .fakes import memo

PLAN: Callable[[Any], Any] | None = None
SEEN: list[Any] = []


def resolve(ctx: Any) -> Any:
    """Config `routing.resolver` entry point (`tests.helpers:resolve`)."""
    SEEN.append(ctx)
    assert PLAN is not None  # noqa: S101
    return PLAN(ctx)


def judge_reply(prompt: str, sealed_relation: str = "agree") -> dict[str, Any]:
    """A valid judge output for the memos named in a judge prompt."""
    body = prompt.split('<COMPARE_JSON untrusted="true">')[1].split("</COMPARE_JSON>")[0]
    positions = json.loads(body)["positions"]
    labels = sorted(k for k in positions if k != "S")
    pairs = [
        {"a": a, "b": b, "relation": "agree", "crux": "c"}
        for i, a in enumerate(labels)
        for b in labels[i + 1 :]
    ]
    sealed = None
    if "S" in positions:
        pairs += [{"a": x, "b": "S", "relation": sealed_relation, "crux": "c"} for x in labels]
        sealed = {"relation": sealed_relation, "crux": "c"}
    stances = [{"label": x, "stance": "stance " + x} for x in labels]
    if "S" in positions:
        stances.append({"label": "S", "stance": "the sealed answer"})
    return {
        "positions": stances,
        "clusters": [labels], "pairwise": pairs, "sealed": sealed,
        "contradictions": [], "material": False,
    }  # fmt: skip


class Scripted:
    """An in-process provider. `behave(prompt, route, n)` gets the prompt, the route and the
    1-based call number for that route's model, and returns reply data (or a Completion) or
    raises a ProviderFailure. By default it answers advisor prompts with a valid memo and
    judge prompts with a valid agreement map."""

    name = "spy"

    def __init__(self, behave: Callable[[str, Route, int], Any] | None = None):
        self.behave = behave
        self.prompts: list[tuple[str, str]] = []
        self.counts: dict[str, int] = {}
        self.cost: float | None = None
        self.lock = threading.Lock()

    def available(self) -> bool:
        return True

    def complete(
        self, prompt: str, schema: dict[str, Any], route: Route, timeout: float
    ) -> Completion:
        key = route.model or ""
        with self.lock:
            self.prompts.append((key, prompt))
            self.counts[key] = n = self.counts.get(key, 0) + 1
        if self.behave is not None:
            reply = self.behave(prompt, route, n)
        elif "COLDTAKE_ROLE: judge" in prompt:
            reply = judge_reply(prompt)
        else:
            reply = memo(f"via {key}")
        if isinstance(reply, Completion):
            return reply
        return Completion(reply, cost_usd=0.001 if route.billing == "paid" else None)

    def worst_case_cost(self, route: Route, prompt_bytes: int) -> float:
        return self.cost if self.cost is not None else worst_case_cost(route, prompt_bytes)

    def calls(self, role: str | None = None) -> list[str]:
        marker = "COLDTAKE_ROLE: judge"
        return [p for _, p in self.prompts if role is None or (role == "judge") == (marker in p)]


def spy_rig(make_rig, plan: Callable[[Any], Any], provider: Scripted | None = None, **extra):
    """A rig whose route plan comes from `plan(ctx)` and whose provider is `Scripted`."""
    global PLAN
    PLAN = plan
    SEEN.clear()
    provider = provider or Scripted()
    rig = make_rig({"routing": {"resolver": "tests.helpers:resolve"}, **extra})
    rig.engine.providers["spy"] = provider
    return rig, provider


def S(model: str, family: str = "fam", **kw: Any) -> Route:
    """A subscription-billed route of the spy provider."""
    return Route("spy", family, model, **kw)


def P(model: str, family: str = "fam", **kw: Any) -> Route:
    """A paid route of the spy provider with sane prices."""
    base = dict(
        billing="paid", prompt_usd_per_mtok=1.0, completion_usd_per_mtok=1.0,
        max_output_tokens=1000,
    )  # fmt: skip
    return Route("spy", family, model, None, **{**base, **kw})


def invalid(error: str = "bad", **kw: Any) -> ProviderFailure:
    return ProviderFailure("invalid_output", "repair", error, **kw)
