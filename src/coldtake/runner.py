"""Attempt execution, route fallback, panel orchestration and ledger settlement."""

from __future__ import annotations

import itertools
import json
import re
import threading
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from .budget import BudgetExceeded, LeaseLost, Ledger, clean_cost
from .compare import sealed_rows, unavailable
from .config import Config
from .contracts import (
    AttemptContext,
    CompareContext,
    Contract,
    call_repair_prompt,
    call_validate_memo,
    contract_uses_judge,
)
from .failures import ProviderFailure, redact
from .planner import context_fits, price_problem, worst_case
from .providers import Provider, Route
from .routing import Ladder, Plan, as_ladder
from .schemas import MAX_MEMOS, Sealed, canonical_json, sha256_hex
from .store import Store

PARTIAL_RAW_BYTES = 200_000


class JobFailure(Exception):
    """A job's terminal failure. `failed_route_billing` is the billing of the route whose
    failure decided it (the job's own billing is read from the ledger and can differ)."""

    STATUSES = frozenset({"failed", "timed_out", "lost", "charge_unknown"})

    def __init__(
        self,
        status: str,
        reason: str,
        cause: dict[str, str],
        failed_route_billing: str | None = None,
    ):
        if status not in self.STATUSES:
            raise ValueError(f"JobFailure status must be one of {sorted(self.STATUSES)}")
        super().__init__(reason)
        self.status, self.reason, self.cause = status, reason, cause
        self.failed_route_billing = failed_route_billing


@dataclass(frozen=True)
class AttemptId:
    """The parts of an attempt id (the ledger key is `<request key>:<attempt id>`)."""

    role: str
    slot: int
    token: str  # per-context random tag; attempts of one job share it
    seq: int  # unique within the context
    route_index: int  # position of the route in its ladder
    attempt: int  # 0 first try, 1..N repair


_ATTEMPT_ID = re.compile(
    r"(?P<role>.+?)(?P<slot>\d+)-(?P<token>[0-9a-f]+)-(?P<seq>\d+)-r(?P<route>\d+)-a(?P<attempt>\d+)"
)


def format_attempt_id(
    role: str, slot: int, token: str, seq: int, route_index: int, attempt: int
) -> str:
    return f"{role}{slot}-{token}-{seq}-r{route_index}-a{attempt}"


def parse_attempt_id(attempt_id: str) -> AttemptId:
    """Inverse of `format_attempt_id`. Raises ValueError for anything else."""
    found = _ATTEMPT_ID.fullmatch(attempt_id)
    if found is None:
        raise ValueError(f"not an attempt id: {attempt_id!r}")
    return AttemptId(
        found["role"], int(found["slot"]), found["token"], int(found["seq"]),
        int(found["route"]), int(found["attempt"]),
    )  # fmt: skip


def split_ledger_key(attempt_key: str) -> tuple[str, str]:
    """A ledger `attempt_key` as (request key, attempt id)."""
    request_key, _, attempt_id = attempt_key.rpartition(":")
    return request_key, attempt_id


def normalize_failure(failure: ProviderFailure, paid: bool) -> ProviderFailure:
    """Enforce the billing rules on any provider, including extensions."""
    if paid and failure.kind == "timed_out":
        return ProviderFailure(
            "charge_unknown", "charge_unknown", failure.message, cause_type=failure.cause_type
        )
    if (
        paid
        and failure.kind in {"fallback", "terminal"}
        and not failure.proven_unsent
        and not failure.accepted
    ):
        return ProviderFailure(
            "charge_unknown", "charge_unknown",
            f"{failure.reason}: {failure.message} (not proven unsent)",
            cause_type=failure.cause_type,
        )  # fmt: skip
    if not paid and failure.kind == "charge_unknown":
        return ProviderFailure(
            "provider_error", "terminal", failure.message, cause_type=failure.cause_type
        )
    return failure


def job_failure_from_provider(failure: ProviderFailure) -> JobFailure:
    status = {"timed_out": "timed_out", "charge_unknown": "charge_unknown"}.get(
        failure.kind, "failed"
    )
    return JobFailure(status, failure.reason, failure.cause(), failure.route_billing)


def summarize_error(error: BaseException) -> str:
    if isinstance(error, ValidationError):
        parts = [
            f"{'.'.join(str(p) for p in e['loc']) or 'packet'}: {e['msg']}" for e in error.errors()
        ]
        return redact("; ".join(parts), 800)
    return redact(str(error), 800)


@dataclass
class Context:
    """Execution context for a route ladder: request key, cancel event, contract, attempt ids."""

    request_id: str
    cancel: threading.Event = field(default_factory=threading.Event)
    contract: Contract | None = None
    instance: str | None = None
    token: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    seq: Any = field(default_factory=itertools.count)
    repair_taken: bool = False
    repair_lock: threading.Lock = field(default_factory=threading.Lock)
    failed_routes: set[tuple[str, str | None]] = field(default_factory=set)
    failed_lock: threading.Lock = field(default_factory=threading.Lock)

    def note_failed(self, route: Route) -> None:
        with self.failed_lock:
            self.failed_routes.add((route.provider, route.model))

    def has_failed(self, route: Route) -> bool:
        with self.failed_lock:
            return (route.provider, route.model) in self.failed_routes

    def take_repair(self) -> bool:
        with self.repair_lock:
            if self.repair_taken:
                return False
            self.repair_taken = True
            return True


@dataclass
class Job(Context):
    packet: BaseModel = None  # type: ignore[assignment]
    sealed: Sealed = None  # type: ignore[assignment]
    lead: str = "other"
    advisor_deadline: float = 0.0
    plan: Plan = None  # type: ignore[assignment]


class _Defaults(Contract):
    """Stand-in contract for jobless runs (`run_chain`): default repair, terminal schema."""


_DEFAULTS = _Defaults()


class Runner:
    """Executes admitted jobs and extension chains against providers, store and ledger."""

    def __init__(
        self,
        config: Config,
        providers: dict[str, Provider],
        store: Store,
        ledger: Ledger,
        instance: str,
    ):
        self.config = config
        self.providers = providers
        self.store = store
        self.ledger = ledger
        self.instance = instance

    def main_seconds(self, handler: Contract) -> float:
        return handler.deadline_seconds or self.config.limits.job_seconds

    def repair_seconds(self, handler: Contract) -> float | None:
        return handler.repair_seconds or self.config.limits.repair_seconds

    def execute(self, job: Job) -> dict[str, Any]:
        handler = job.contract
        assert handler is not None  # noqa: S101
        if handler.mode(job.packet) == "panel":
            memos = self._run_panel(job, job.plan)
        else:
            if not job.plan.chain:
                raise JobFailure(
                    "failed", "no_independent_route",
                    {"type": "NoRoute", "message": f"no available route outside {job.lead!r}"},
                )  # fmt: skip
            memos = [self._advise(job, 1, job.plan.chain)]
        try:
            comparison = handler.compare(
                CompareContext(
                    job.request_id, job.packet, job.sealed, memos, self._judge_runner(job)
                )
            )
            if comparison is not None:
                self.store.record_sealed_relations(
                    job.request_id, handler.mode(job.packet), comparison.get("method", ""),
                    sealed_rows(comparison, memos),
                )  # fmt: skip
        except Exception as exc:  # noqa: BLE001 - the memos are final; never fail the job here
            held = self.ledger.hold_unfinalized(job.request_id)
            if isinstance(exc, ProviderFailure) and exc.kind == "charge_unknown":
                comparison = unavailable("charge_unknown", {**exc.cause(), "reason": exc.reason})
            elif held:
                comparison = unavailable(
                    "charge_unknown", {"type": type(exc).__name__, "message": redact(str(exc))}
                )
            else:
                comparison = unavailable(
                    "compare_error", {"type": type(exc).__name__, "message": redact(str(exc))}
                )
        return {"memos": memos, "comparison": comparison}

    def _judge_runner(self, job: Job) -> Callable[..., tuple[Any, Route]]:
        limits = self.config.limits

        def judge(prompt: str, schema: dict[str, Any], validate: Callable[..., Any]):
            row = self.store.get_run(job.request_id) or {}
            deadline = min(self.store.clock() + limits.judge_seconds, row.get("deadline_at", 0))
            ladder = job.plan.chain if job.plan.judge is None else job.plan.judge
            if not ladder:
                if job.plan.judge_dropped:
                    raise ProviderFailure(
                        "judge_unaffordable", "terminal",
                        "no judge route fits the budget beside the advisors",
                        cause_type="BudgetExceeded",
                    )  # fmt: skip
                raise ProviderFailure("no_independent_route", "terminal", "no judge route")
            return self.chain(
                job, "judge", 0, ladder, prompt, schema, lambda data, _ctx: validate(data),
                limits.judge_seconds, deadline,
            )  # fmt: skip

        return judge

    def _advise(self, job: Job, slot: int, ladder: Ladder) -> dict[str, Any]:
        handler = job.contract
        assert handler is not None  # noqa: S101
        per_attempt = handler.attempt_seconds(job.packet) or self.config.limits.attempt_seconds
        value, route = self.chain(
            job, "advisor", slot, ladder, handler.render_prompt(job.packet),
            handler.memo_schema(job.packet),
            lambda data, ctx: call_validate_memo(handler, data, job.packet, ctx),
            float(per_attempt), job.advisor_deadline,
        )  # fmt: skip
        record = {
            "id": f"m{slot}",
            "family": route.family,
            "provider": route.provider,
            "model": route.model,
            "billing": route.billing,
            "same_family": route.family == job.lead,
            "memo": value,
            "memo_sha256": sha256_hex(canonical_json(value)),
        }
        self.store.append_memo(job.request_id, self.instance, record)
        return record

    def _run_panel(self, job: Job, plan: Plan) -> list[dict[str, Any]]:
        slots = [as_ladder(x) for x in plan.slots]
        reserve = [as_ladder(x) for x in plan.reserve]
        if len(slots) < 2:
            raise JobFailure(
                "failed", "insufficient_families",
                {"type": "NoRoute", "message": "a panel needs two distinct available families"},
            )  # fmt: skip
        results: dict[int, dict[str, Any]] = {}
        lost: list[int] = []
        terminals: list[JobFailure] = []
        last: ProviderFailure | None = None
        workers = max(1, min(plan.parallel or len(slots), len(slots)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                slot: pool.submit(self._advise, job, slot, ladder)
                for slot, ladder in enumerate(slots, 1)
            }
            for slot, future in futures.items():
                try:
                    results[slot] = future.result()
                except ProviderFailure as failure:
                    last = failure
                    if failure.kind == "fallback":
                        lost.append(slot)
                    elif failure.reason != "cancelled":
                        terminals.append(job_failure_from_provider(failure))
                        job.cancel.set()
                except JobFailure as failure:
                    terminals.append(failure)
                    job.cancel.set()
        if terminals:
            raise next((f for f in terminals if f.status == "charge_unknown"), terminals[0])
        for slot in lost:
            while reserve and slot not in results:
                ladder = self._eligible(job, reserve.pop(0), results.values())
                if not ladder:
                    continue
                try:
                    results[slot] = self._advise(job, slot, ladder)
                except ProviderFailure as failure:
                    last = failure
                    if failure.kind != "fallback":
                        raise job_failure_from_provider(failure) from failure
        if len(results) < 2:
            cause = last.cause() if last else {"type": "PanelIncomplete", "message": ""}
            raise JobFailure("failed", "panel_incomplete", cause)
        memos = [results[slot] for slot in sorted(results)]
        if plan.escalate and plan.escalation and len(memos) < MAX_MEMOS and plan.escalate(memos):
            memos.extend(self._escalate(job, plan, len(slots) + len(lost) + 1, memos))
        return memos

    @staticmethod
    def _eligible(job: Context, ladder: Ladder, seated: Any) -> Ladder:
        families = {m["family"] for m in seated}
        return tuple(r for r in ladder if r.family not in families and not job.has_failed(r))

    def _escalate(
        self, job: Job, plan: Plan, slot: int, memos: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        handler = job.contract
        assert handler is not None  # noqa: S101
        job.advisor_deadline = self.store.clock() + self.main_seconds(handler)
        limits = self.config.limits
        uses_judge = contract_uses_judge(handler, job.packet, has_sealed=job.sealed.present)
        tail = (self.repair_seconds(handler) or 0) + (limits.judge_seconds if uses_judge else 0)
        self.store.extend_deadline(job.request_id, self.instance, job.advisor_deadline + tail)
        for pooled in (as_ladder(x) for x in plan.escalation):
            ladder = self._eligible(job, pooled, memos)
            if not ladder:
                continue
            try:
                return [self._advise(job, slot, ladder)]
            except ProviderFailure as failure:
                if failure.kind != "fallback":
                    raise job_failure_from_provider(failure) from failure
        return []

    def chain(
        self, job: Context, role: str, slot: int, routes: tuple[Route, ...], prompt: str,
        schema: dict[str, Any], validate: Callable[[dict[str, Any], AttemptContext], Any],
        attempt_seconds: float, deadline: float,
    ) -> tuple[Any, Route]:  # fmt: skip
        failure: ProviderFailure | None = None
        for index, route in enumerate(routes):
            try:
                value = self._try_route(
                    job, role, slot, index, route, prompt, schema, validate, attempt_seconds,
                    deadline,
                )  # fmt: skip
                return value, route
            except ProviderFailure as exc:
                if exc.route_billing is None:
                    exc.route_billing = route.billing
                if exc.reason != "cancelled":
                    job.note_failed(route)
                if exc.kind != "fallback":
                    raise
                failure = exc
        if failure is None:
            raise ProviderFailure("no_route", "terminal", "no routes to try")
        raise failure

    def _try_route(
        self, job: Context, role: str, slot: int, index: int, route: Route, prompt: str,
        schema: dict[str, Any], validate: Callable[[dict[str, Any], AttemptContext], Any],
        attempt_seconds: float, deadline: float,
    ) -> Any:  # fmt: skip
        provider, paid = self.providers[route.provider], route.billing == "paid"
        handler = job.contract or _DEFAULTS
        repair_seconds = self.repair_seconds(handler)
        max_repairs = 1 if role == "judge" else max(0, int(handler.max_repairs))
        prior_raw: str | None = None
        prior_data: dict[str, Any] | None = None
        prior_error = ""
        for attempt in range(max_repairs + 1):
            if job.cancel.is_set():
                raise ProviderFailure("cancelled", "terminal", "job stopped before this attempt")
            repair = attempt > 0
            if repair and handler.shared_repair and not job.take_repair():
                raise self._exhausted(
                    role, job, handler, prior_raw, "the job's one repair was used"
                )
            if repair:
                keep = 4 * route.max_output_tokens if paid else PARTIAL_RAW_BYTES
                kept = (prior_raw or "").encode()[:keep].decode("utf-8", "ignore")
                repair_ctx = AttemptContext(
                    role=role, slot=slot, route=route, attempt_index=attempt, is_repair=True,
                    prior_data=prior_data, prior_raw=prior_raw,
                )  # fmt: skip
                if role == "judge":
                    text = handler.judge_repair_prompt(prompt, kept, prior_error)
                else:
                    text = call_repair_prompt(handler, prompt, kept, prior_error, repair_ctx)
            else:
                text = prompt
            remaining = deadline - self.store.clock()
            if repair and repair_seconds:
                timeout = float(repair_seconds)
            elif remaining <= 0:
                raise ProviderFailure(
                    "deadline_exhausted", "timed_out", "no time left before this attempt"
                )
            else:
                timeout = min(attempt_seconds, remaining)
            attempt_id = format_attempt_id(role, slot, job.token, next(job.seq), index, attempt)
            size = len(text.encode())
            if paid:
                blocked = self._authorize(job, role, slot, attempt, index, route, attempt_id, size)
                if blocked is not None:
                    if repair:
                        raise self._exhausted(role, job, handler, prior_raw, blocked.message)
                    raise blocked
            started = self.store.clock()
            state = self._progress(job, role, slot, route, index, attempt, started)
            try:
                completion = provider.complete(text, schema, route, timeout)
            except ProviderFailure as raised:
                failure = normalize_failure(raised, paid)
                self._progress_end(job, role, slot, state, failure.reason)
                self._settle_failure(job, attempt_id, route, failure)
                self._record(
                    job, role, slot, attempt, route, failure.reason, index, failure, started,
                    tokens=failure.usage,
                )  # fmt: skip
                if failure.kind == "repair":
                    prior_raw, prior_error = failure.raw, failure.message
                    prior_data = None
                    if attempt < max_repairs:
                        continue
                    raise self._exhausted(role, job, handler, prior_raw, prior_error) from failure
                if (
                    repair
                    and failure.kind == "fallback"
                    and not handler.fallback_after_repair_failure
                ):
                    raise ProviderFailure(
                        failure.reason,
                        "terminal",
                        failure.message,
                        cause_type=failure.cause_type,
                        accepted=failure.accepted,
                        cost_usd=failure.cost_usd,
                        raw=failure.raw,
                        usage=failure.usage,
                        proven_unsent=failure.proven_unsent,
                    ) from failure
                raise failure from raised
            except Exception as exc:  # noqa: BLE001 - unexpected adapter error
                self._progress_end(job, role, slot, state, type(exc).__name__)
                unexpected = ProviderFailure(
                    "charge_unknown" if paid else "provider_error",
                    "charge_unknown" if paid else "terminal",
                    str(exc), cause_type=type(exc).__name__,
                )  # fmt: skip
                self._settle_failure(job, attempt_id, route, unexpected)
                self._record(
                    job, role, slot, attempt, route, unexpected.reason, index, unexpected, started
                )
                raise unexpected from exc
            if paid:
                self.ledger.finalize(job.request_id, attempt_id, "committed", completion.cost_usd)
            self._progress_end(job, role, slot, state, "returned")
            if (
                handler.max_output_bytes is not None
                and isinstance(completion.raw, str)
                and len(completion.raw.encode("utf-8")) > handler.max_output_bytes
            ):
                oversize = ProviderFailure(
                    "output_too_large", "terminal",
                    f"provider output exceeded {handler.max_output_bytes}-byte contract bound",
                    cause_type="OutputTooLarge", accepted=True,
                    cost_usd=completion.cost_usd, usage=completion.usage,
                )  # fmt: skip
                self._record(
                    job, role, slot, attempt, route, oversize.reason, index, oversize, started,
                    clean_cost(completion.cost_usd), completion.usage,
                )  # fmt: skip
                raise oversize
            attempt_ctx = AttemptContext(
                role=role, slot=slot, route=route, attempt_index=attempt, is_repair=repair,
                prior_data=prior_data, prior_raw=prior_raw, raw=completion.raw,
            )  # fmt: skip
            try:
                value = validate(completion.data, attempt_ctx)
            except (ValidationError, ValueError) as exc:
                self._progress_end(job, role, slot, state, "invalid_output")
                self._record(
                    job, role, slot, attempt, route, "invalid_output", index, exc, started,
                    clean_cost(completion.cost_usd), completion.usage,
                )  # fmt: skip
                prior_raw = completion.raw or json.dumps(completion.data)
                prior_data = completion.data
                prior_error = summarize_error(exc)
                if attempt < max_repairs:
                    continue
                raise self._exhausted(role, job, handler, prior_raw, prior_error) from exc
            except ProviderFailure as hook:
                self._progress_end(job, role, slot, state, hook.reason)
                self._record(
                    job, role, slot, attempt, route, hook.reason, index, hook, started,
                    clean_cost(completion.cost_usd), completion.usage,
                )  # fmt: skip
                raise
            except JobFailure as hook:
                self._progress_end(job, role, slot, state, hook.reason)
                self._record(
                    job, role, slot, attempt, route, hook.reason, index, hook, started,
                    clean_cost(completion.cost_usd), completion.usage,
                )  # fmt: skip
                if hook.failed_route_billing is None:
                    hook.failed_route_billing = route.billing
                raise
            except Exception as exc:  # noqa: BLE001 - bug in a hook; keep the record
                self._progress_end(job, role, slot, state, type(exc).__name__)
                self._record(
                    job, role, slot, attempt, route, type(exc).__name__, index, exc, started,
                    clean_cost(completion.cost_usd), completion.usage,
                )  # fmt: skip
                raise ProviderFailure(
                    "internal_error", "terminal", str(exc), cause_type=type(exc).__name__
                ) from exc
            self._record(
                job, role, slot, attempt, route, "ok", index, None, started,
                clean_cost(completion.cost_usd), completion.usage,
            )  # fmt: skip
            return value
        raise AssertionError("unreachable")  # pragma: no cover  # noqa: S101

    def _authorize(
        self, job: Context, role: str, slot: int, attempt: int, index: int, route: Route,
        attempt_id: str, size: int,
    ) -> ProviderFailure | None:  # fmt: skip
        if not context_fits(self.providers, route, size):
            return ProviderFailure(
                "context_rejected", "fallback", "prompt cannot fit the model context",
                cause_type="ContextFit",
            )  # fmt: skip
        try:
            problem = price_problem(self.providers, route, size)
            if problem:
                raise BudgetExceeded(problem)
            self.ledger.authorize(
                job.request_id, attempt_id, route.model or "",
                worst_case(self.providers, route, size), instance=job.instance,
            )  # fmt: skip
        except LeaseLost as exc:
            return ProviderFailure("cancelled", "terminal", str(exc), cause_type="LeaseLost")
        except BudgetExceeded as exc:
            self._record(job, role, slot, attempt, route, "budget_blocked", index, exc, 0)
            return ProviderFailure(
                "budget_exceeded", "fallback", str(exc), cause_type="BudgetExceeded"
            )
        return None

    def _progress(
        self, job: Context, role: str, slot: int, route: Route, index: int, attempt: int,
        started: float,
    ) -> dict[str, Any]:  # fmt: skip
        state = {
            "phase": role, "slot": slot, "route_index": index, "attempt_index": attempt,
            "provider": route.provider, "model": route.model, "billing": route.billing,
            "dispatched": True, "started_at": started,
        }  # fmt: skip
        if job.instance is not None:
            self.store.set_progress(job.request_id, f"{role}{slot}", state)
        return state

    def _progress_end(
        self, job: Context, role: str, slot: int, state: dict[str, Any], outcome: str
    ) -> None:
        if job.instance is None:
            return
        self.store.set_progress(
            job.request_id,
            f"{role}{slot}",
            {**state, "dispatched": False, "finished_at": self.store.clock(), "outcome": outcome},
        )

    def _exhausted(
        self, role: str, job: Context, handler: Contract, prior_raw: str | None, error: str
    ) -> ProviderFailure:
        if role == "advisor" and isinstance(job, Job) and prior_raw:
            self.store.set_partial(
                job.request_id,
                {
                    "label": "incomplete: this reply failed validation and is not a valid memo",
                    "raw": prior_raw[:PARTIAL_RAW_BYTES],
                    "error": error,
                },
            )
        kind = "fallback" if handler.fallback_on_schema_exhausted else "terminal"
        return ProviderFailure("schema_exhausted", kind, error, cause_type="SchemaExhausted")

    def _settle_failure(
        self, job: Context, attempt_id: str, route: Route, failure: ProviderFailure
    ) -> None:
        if route.billing != "paid":
            return
        if failure.kind == "charge_unknown":
            self.ledger.finalize(job.request_id, attempt_id, "charge_unknown")
        elif failure.accepted or failure.kind == "repair":
            self.ledger.finalize(job.request_id, attempt_id, "committed", failure.cost_usd)
        elif failure.proven_unsent:
            self.ledger.finalize(job.request_id, attempt_id, "released")
        else:
            self.ledger.finalize(job.request_id, attempt_id, "charge_unknown")

    def _record(
        self, job: Context, role: str, slot: int, attempt: int, route: Route, outcome: str,
        route_index: int, error: BaseException | None, started: float, cost: float | None = None,
        tokens: Mapping[str, int] | None = None,
    ) -> None:  # fmt: skip
        failure = error if isinstance(error, ProviderFailure) else None
        tokens = tokens or {}
        self.store.record_attempt(
            job.request_id, role=role, slot=slot, attempt=attempt, provider=route.provider,
            model=route.model, family=route.family, billing=route.billing, outcome=outcome,
            reason=None if outcome == "ok" else outcome,
            cause_type=(failure.cause_type if failure else type(error).__name__) if error else None,
            cause_message=redact(failure.message if failure else str(error)) if error else None,
            started_at=started or self.store.clock(),
            seconds=max(0.0, self.store.clock() - started) if started else 0.0,
            cost_usd=cost, input_tokens=tokens.get("input_tokens"),
            output_tokens=tokens.get("output_tokens"), fallback=int(route_index > 0),
        )  # fmt: skip
