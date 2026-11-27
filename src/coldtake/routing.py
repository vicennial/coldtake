"""Lead-family detection and route planning. Advisors come from other families first."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .providers import Provider, Route, load_object

# MCP clientInfo.name fragments, checked in order.
_CLIENT_FAMILIES = (
    ("claude", "anthropic"),
    ("codex", "openai"),
    ("openai", "openai"),
    ("chatgpt", "openai"),
    ("gemini", "google"),
    ("antigravity", "google"),
)


def detect_lead_family(client_name: str | None, setting: str = "auto") -> str:
    """Family of the calling agent: configured value, else from the MCP client name."""
    if setting != "auto":
        return setting
    lowered = (client_name or "").casefold()
    for fragment, family in _CLIENT_FAMILIES:
        if fragment in lowered:
            return family
    return "other"


@dataclass(frozen=True)
class RouteContext:
    """What a route planner may base its ladder on."""

    lead_family: str
    mode: str
    packet: Any  # the contract's validated packet (stakes, reversibility, ...)
    contract: str
    namespace: str | None
    prompt_bytes: int
    config: Config
    providers: Mapping[str, Provider]


Ladder = tuple[Route, ...]


@dataclass(frozen=True)
class Plan:
    """The route plan for one job.

    chain:       review/alternate ladder, also the judge ladder.
    slots:       panel members, one ladder each (a ladder may hold same-family continuity
                 routes: they are tried in order inside the one member).
    reserve:     replacement pool; each entry (a Route or a ladder) replaces a member whose
                 ladder failed safely.
    escalation:  extra members (Routes or ladders) added when `escalate(memos)` is true.
    parallel:    how many members run at once (None: all).
    preflight_ladder_budget:  reject the request before ACK (BudgetExceeded) when the whole
                 paid ladder, initial plus repair attempt of every paid route, cannot fit
                 the per-request cap.
    prune_paid_if_unaffordable:  in single-advisor modes, drop the paid routes instead.
    judge:       the comparison (judge) ladder; None means `chain`. The judge is planned
                 apart from the advisors: its cost never removes an advisor route. With
                 `preflight_ladder_budget` on (an explicit judge ladder is left as it is when
                 only `prune_paid_if_unaffordable` is set; with no judge ladder the judge
                 uses the possibly pruned chain) the engine keeps only the paid judge
                 routes that fit beside the advisors' worst case and lists the others in
                 `judge_dropped`; with no judge route left the comparison is unavailable.
    judge_dropped:  paid judge routes the engine removed as unaffordable (set by the engine).
    """

    chain: tuple[Route, ...] = ()
    slots: tuple[Ladder, ...] = ()
    reserve: tuple[Route | Ladder, ...] = ()
    escalation: tuple[Route | Ladder, ...] = ()
    escalate: Callable[[list[dict[str, Any]]], bool] | None = None
    parallel: int | None = None
    preflight_ladder_budget: bool = False
    prune_paid_if_unaffordable: bool = False
    extra: dict[str, Any] = field(default_factory=dict, compare=False)
    judge: tuple[Route, ...] | None = None
    judge_dropped: tuple[Route, ...] = ()


def as_ladder(item: Route | Sequence[Route]) -> Ladder:
    return (item,) if isinstance(item, Route) else tuple(item)


def _subscription_routes(
    config: Config, providers: Mapping[str, Provider], lead: str
) -> list[Route]:
    routes = []
    for family, spec in config.subscription.items():
        provider = providers.get(spec.provider)
        if family != lead and provider is not None and provider.available():
            routes.append(Route(spec.provider, family, spec.model, spec.effort))
    return routes


def _paid_routes(config: Config, providers: Mapping[str, Provider], lead: str) -> list[Route]:
    provider = providers.get("openrouter")
    if provider is None or not provider.available():
        return []
    return [Route.paid(m) for m in config.openrouter.models if m.family != lead]


def _split_panel(config: Config, candidates: list[Route]) -> tuple[Ladder, tuple[Route, ...]]:
    """First max_members candidates become members (at most max_paid_members paid)."""
    members: list[Route] = []
    reserve: list[Route] = []
    paid = 0
    for route in candidates:
        if route.billing == "paid" and paid >= config.panel.max_paid_members:
            continue
        if len(members) < config.panel.max_members:
            members.append(route)
            paid += route.billing == "paid"
        else:
            reserve.append(route)
    return tuple(members), tuple(reserve)


def _same_family_route(
    config: Config, providers: Mapping[str, Provider], lead: str
) -> Route | None:
    """The lead family's own subscription route. It is allowed only as the LAST ladder
    entry or LAST panel member, when other families are not enough; its memos are
    labelled `same_family: true` in the result."""
    spec = config.subscription.get(lead)
    provider = providers.get(spec.provider) if spec else None
    if spec and provider is not None and provider.available():
        return Route(spec.provider, lead, spec.model, spec.effort)
    return None


def _check_plan(ctx: RouteContext) -> Plan:
    """Default plan for `coldtake_check_v1`: prefer Google/Gemini subscription first when the
    design author isn't Gemini, else cross-family subscription routes; never same-family."""
    inner = getattr(ctx.packet, "packet", ctx.packet)
    author = getattr(inner, "author_family", None)
    if author == "gemini":
        author = "google"
    excluded = author or ctx.lead_family
    subs = _subscription_routes(ctx.config, ctx.providers, excluded)
    google = [r for r in subs if r.family == "google"]
    others = [r for r in subs if r.family != "google"]
    chain = tuple([*google, *others])
    if not chain:
        same = _same_family_route(ctx.config, ctx.providers, excluded)
        chain = (same,) if same is not None else ()
    return Plan(chain=chain)


def default_plan(ctx: RouteContext) -> Plan:
    if ctx.mode == "check":
        return _check_plan(ctx)
    config, lead = ctx.config, ctx.lead_family
    subscription = _subscription_routes(config, ctx.providers, lead)
    paid = _paid_routes(config, ctx.providers, lead)
    same = _same_family_route(config, ctx.providers, lead)
    one_paid_per_family: dict[str, Route] = {}
    for route in paid:
        one_paid_per_family.setdefault(route.family, route)
    taken = {route.family for route in subscription}
    candidates = [*subscription, *(r for f, r in one_paid_per_family.items() if f not in taken)]
    members, reserve = _split_panel(config, candidates)
    if same is not None and len(members) + len(reserve) < 2:
        members = (*members, same)
    return Plan(
        chain=tuple([*subscription, *paid, *([same] if same else [])]),
        slots=tuple((m,) for m in members),
        reserve=reserve,
        parallel=len(members) or None,
    )


def _from_routes(ctx: RouteContext, routes: Sequence[Route]) -> Plan:
    """A plain route list: it is the chain; panel members are its families, in order."""
    by_family: dict[str, list[Route]] = {}
    for route in routes:
        by_family.setdefault(route.family, []).append(route)
    ladders = list(by_family.values())
    cap = ctx.config.panel.max_members
    return Plan(
        chain=tuple(routes),
        slots=tuple(tuple(x) for x in ladders[:cap]),
        reserve=tuple(tuple(x) for x in ladders[cap:]),
        parallel=min(len(ladders), cap) or None,
    )


def plan_routes(ctx: RouteContext) -> Plan:
    """The route plan for one job: the configured resolver if any, else the default.

    A resolver is a callable (or a class, instantiated with the config) named by
    `routing.resolver`, called as `resolver(ctx: RouteContext)`; it returns a `Plan` or a
    plain sequence of routes. Whatever it returns, routes of the lead family and routes
    whose provider is not registered or not available are dropped, except that lead-family
    routes are kept when they come last (the labelled same-family tail), so independence
    and availability are enforced for resolvers too.
    """
    if not ctx.config.routing.resolver:
        return default_plan(ctx)
    target = load_object(ctx.config.routing.resolver)
    resolver = target(ctx.config) if isinstance(target, type) else target
    resolved = resolver(ctx)
    plan = resolved if isinstance(resolved, Plan) else _from_routes(ctx, list(resolved))

    excluded = ctx.lead_family
    if ctx.mode == "check":
        inner = getattr(ctx.packet, "packet", ctx.packet)
        author = getattr(inner, "author_family", None)
        if author:
            excluded = "google" if author == "gemini" else author

    def usable(route: Route) -> bool:
        return route.provider in ctx.providers and ctx.providers[route.provider].available()

    def tail_only(routes: Sequence[Route]) -> Ladder:
        """Independent routes, then lead-family routes only if they come last."""
        live = [r for r in routes if usable(r)]
        last_other = max((i for i, r in enumerate(live) if r.family != excluded), default=-1)
        return tuple(r for i, r in enumerate(live) if r.family != excluded or i > last_other)

    def ladders(items: Sequence[Route | Ladder]) -> tuple[Ladder, ...]:
        cleaned = [tail_only(as_ladder(item)) for item in items]
        cleaned = [ladder for ladder in cleaned if ladder]
        # a ladder made only of lead-family routes must come after every independent one
        independent = [x for x in cleaned if any(r.family != excluded for r in x)]
        own = [x for x in cleaned if all(r.family == excluded for r in x)]
        return tuple([*independent, *own[:1]])

    return Plan(
        chain=tail_only(plan.chain),
        slots=ladders(plan.slots),
        reserve=ladders(plan.reserve),
        escalation=ladders(plan.escalation),
        escalate=plan.escalate,
        parallel=plan.parallel,
        preflight_ladder_budget=plan.preflight_ladder_budget,
        prune_paid_if_unaffordable=plan.prune_paid_if_unaffordable,
        judge=tail_only(plan.judge) if plan.judge is not None else None,
        extra=plan.extra,
    )
