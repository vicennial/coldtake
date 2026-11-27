"""Route admission, pricing validation, paid-tail pruning and separate judge planning."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, replace
from typing import Any

from pydantic import BaseModel

from .budget import BudgetExceeded
from .config import Config
from .contracts import Contract, call_repair_prompt, contract_uses_judge
from .prompts import MAX_REPAIR_ERROR
from .providers import Provider, Route, worst_case_cost
from .routing import Ladder, Plan, RouteContext, as_ladder, plan_routes
from .schemas import MAX_MEMOS

# Planning estimate of one memo's size inside a judge prompt (bytes).
MEMO_ESTIMATE_BYTES = 64_000


class InvalidRequest(ValueError):
    """The request id or packet is not acceptable. Nothing was started."""


def plan_view(plan: Plan) -> dict[str, Any]:
    """JSON form of an admitted plan (stored on the run, readable by status shapers)."""
    return {
        "chain": [asdict(r) for r in plan.chain],
        "judge": [asdict(r) for r in (plan.chain if plan.judge is None else plan.judge)],
        "judge_dropped": [asdict(r) for r in plan.judge_dropped],
        "slots": [[asdict(r) for r in as_ladder(x)] for x in plan.slots],
        "reserve": [[asdict(r) for r in as_ladder(x)] for x in plan.reserve],
        "escalation": [[asdict(r) for r in as_ladder(x)] for x in plan.escalation],
        "escalate": plan.escalate is not None,
        "parallel": plan.parallel,
        "preflight_ladder_budget": plan.preflight_ladder_budget,
        "prune_paid_if_unaffordable": plan.prune_paid_if_unaffordable,
        "extra": dict(plan.extra),
    }


def worst_case(providers: Mapping[str, Provider], route: Route, prompt_bytes: int) -> float:
    override = getattr(providers[route.provider], "worst_case_cost", None)
    return override(route, prompt_bytes) if override else worst_case_cost(route, prompt_bytes)


def context_fits(providers: Mapping[str, Provider], route: Route, prompt_bytes: int) -> bool:
    override = getattr(providers[route.provider], "context_fits", None)
    if override:
        return bool(override(route, prompt_bytes))
    if route.context_tokens is None:
        return True
    needed = prompt_bytes + route.input_reserve_tokens + route.max_output_tokens
    return needed <= route.context_tokens


def price_problem(providers: Mapping[str, Provider], route: Route, prompt_bytes: int) -> str | None:
    """Why a paid route cannot be given an allowance, or None."""
    if route.billing != "paid":
        return None
    prices = (route.prompt_usd_per_mtok, route.completion_usd_per_mtok)
    if (
        not all(math.isfinite(p) for p in prices)
        or route.prompt_usd_per_mtok < 0
        or route.completion_usd_per_mtok <= 0
        or route.max_output_tokens <= 0
    ):
        return f"paid route {route.label} has unusable pricing"
    worst = worst_case(providers, route, prompt_bytes)
    if not math.isfinite(worst) or worst <= 0:
        return f"paid route {route.label} has an unusable worst-case cost"
    return None


def repair_bytes(handler: Contract, prompt: str, route: Route, role: str = "advisor") -> int:
    """Upper bound of what a repair prompt adds to the original."""
    if role != "judge":
        hook = handler.repair_overhead_bytes(prompt, route)
        if hook is not None:
            return hook
    if role == "judge":
        framing = len(
            handler.judge_repair_prompt(prompt, "", "x" * MAX_REPAIR_ERROR).encode()
        ) - len(prompt.encode())
    else:
        framing = len(
            call_repair_prompt(handler, prompt, "", "x" * MAX_REPAIR_ERROR, None).encode()
        ) - len(prompt.encode())
    return max(0, framing) + 4 * route.max_output_tokens


def admit_plan(
    config: Config,
    providers: Mapping[str, Provider],
    namespace: str | None,
    handler: Contract,
    packet: BaseModel,
    lead: str,
    prompt: str,
    sealed_bytes: int = 0,
) -> Plan:
    """The admitted route plan for a packet: availability, independence, pricing and budget."""
    prompt_bytes = len(prompt.encode())
    mode = handler.mode(packet)
    plan = plan_routes(
        RouteContext(lead, mode, packet, handler.name, namespace, prompt_bytes, config, providers)
    )
    plan = replace(plan, slots=plan.slots[:MAX_MEMOS])
    routes = [
        *plan.chain,
        *(plan.judge or ()),
        *(r for x in (*plan.slots, *plan.reserve, *plan.escalation) for r in as_ladder(x)),
    ]
    for route in routes:
        problem = price_problem(providers, route, prompt_bytes)
        if problem:
            raise BudgetExceeded(problem)
    if mode == "panel" and len(plan.slots) < 2:
        raise InvalidRequest("a panel needs at least two members and fewer are planned")
    cap = config.budget.per_request_usd
    judge_source = plan.chain if plan.judge is None else plan.judge

    def cost(
        items: Ladder | tuple[Route, ...], size: int = prompt_bytes, role: str = "advisor"
    ) -> float:
        repairs = 1 if role == "judge" else max(0, int(handler.max_repairs))
        total = 0.0
        for r in items:
            if r.billing != "paid":
                continue
            total += worst_case(providers, r, size)
            if repairs:
                total += repairs * worst_case(
                    providers, r, size + repair_bytes(handler, prompt, r, role)
                )
        return total

    checked = plan.preflight_ladder_budget or plan.prune_paid_if_unaffordable
    if mode == "panel":
        ladders = [as_ladder(x) for x in (*plan.slots, *plan.reserve, *plan.escalation)]
        advisor_bound = sum(cost(x) for x in ladders)
        if plan.preflight_ladder_budget and advisor_bound > cap:
            raise BudgetExceeded("worst-case paid ladder exceeds the per-request cap")
    else:
        if checked and cost(plan.chain) > cap:
            subscription = tuple(r for r in plan.chain if r.billing != "paid")
            if not (plan.prune_paid_if_unaffordable and subscription):
                raise BudgetExceeded("worst-case paid ladder exceeds the per-request cap")
            plan = replace(plan, chain=subscription)
        advisor_bound = cost(plan.chain)
    if contract_uses_judge(handler, packet, has_sealed=sealed_bytes > 0) and (
        plan.preflight_ladder_budget
    ):
        members = max(1, len(plan.slots) + len(plan.escalation))
        judge_bytes = prompt_bytes + sealed_bytes + MEMO_ESTIMATE_BYTES * members
        kept: list[Route] = []
        dropped: list[Route] = []
        spent = advisor_bound
        for route in judge_source:
            if route.billing != "paid":
                kept.append(route)
                continue
            price = cost((route,), judge_bytes, "judge")
            if spent + price <= cap:
                kept.append(route)
                spent += price
            else:
                dropped.append(route)
        plan = replace(plan, judge=tuple(kept), judge_dropped=tuple(dropped))
    return plan


__all__ = [
    "MEMO_ESTIMATE_BYTES",
    "InvalidRequest",
    "admit_plan",
    "context_fits",
    "plan_view",
    "price_problem",
    "repair_bytes",
    "worst_case",
]
