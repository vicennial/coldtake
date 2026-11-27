"""Job contracts. A contract owns everything that defines one kind of job: the packet
model, the prompt, the memo schema and validation, repair, comparison and the result and
status shapes. The engine owns the lifecycle (idempotency, leases, routes, budget).

`CoreContract` is coldtake's own (sealed answer, options or judge comparison). An
extension registers more with `engine.register_contract` and starts jobs under them with
`engine.submit(..., contract=name)`; the contract name is stored with the run and is part
of its idempotency identity.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from .blindness import sealed_overlap_warnings
from .compare import judge_comparison, not_applicable, options_comparison, unavailable
from .config import Config
from .failures import ProviderFailure
from .prompts import (
    judge_labels,
    judge_repair_text,
    load_templates,
    render_advisor_prompt,
    render_judge_prompt,
    repair_text,
)
from .providers import Route
from .schemas import (
    JudgeOutput,
    Memo,
    Packet,
    Sealed,
    Submission,
    check_choice,
    memo_model,
    provider_schema,
)

# A tool-free comparison runner supplied by the engine: judge(prompt, schema, validate)
# returns (validated value, route used) or raises ProviderFailure.
JudgeRunner = Callable[[str, dict[str, Any], Callable[[dict[str, Any]], Any]], tuple[Any, Route]]


@dataclass(frozen=True)
class AttemptContext:
    """Per-attempt metadata handed to contract validation and repair hooks."""

    role: str  # "advisor" | "judge" | "extension"
    slot: int
    route: Route
    attempt_index: int  # 0 first try, 1..N repair
    is_repair: bool
    prior_data: dict[str, Any] | None = None
    prior_raw: str | None = None
    raw: str | None = None


@dataclass
class Parsed:
    packet: BaseModel
    sealed: Sealed = field(default_factory=Sealed)
    identity: Any = None  # JSON-able; hashed with the contract name for idempotency
    warnings: list[str] = field(default_factory=list)


@dataclass
class CompareContext:
    request_id: str
    packet: BaseModel
    sealed: Sealed
    memos: list[dict[str, Any]]
    judge: JudgeRunner


@dataclass
class RunView:
    """Read-only view of a run, handed to the shaping hooks and `engine.run_view`."""

    request_id: str  # the caller-visible id (without any namespace)
    contract: str
    mode: str
    status: str
    lead_family: str
    packet: BaseModel
    memos: list[dict[str, Any]]
    comparison: dict[str, Any] | None
    sealed_sha256: str | None
    warnings: list[str]
    paid: dict[str, float]
    attempts: list[dict[str, Any]]
    row: dict[str, Any]  # the stored row: plan_json (admitted plan), progress_json, ...
    base_result: dict[str, Any]  # coldtake's default result shape
    plan: dict[str, Any] | None = None  # the plan admitted at ACK
    progress: dict[str, Any] = field(default_factory=dict)  # in-progress attempt state


class Contract:
    """Base contract. Subclass and override; only the first five hooks are required."""

    name: str = ""
    packet_model: type[BaseModel]
    # Schema exhaustion after repairs stops the job unless this is True.
    fallback_on_schema_exhausted: bool = False
    # Number of repair attempts permitted after an invalid reply (0 = strictly one-turn).
    max_repairs: int = 1
    # If False, an operational (`fallback`) failure during a repair attempt stops the job
    # instead of falling through to the next route.
    fallback_after_repair_failure: bool = False
    # Optional per-contract cap on raw provider response bytes.
    max_output_bytes: int | None = None
    # Per-contract overrides of limits.job_seconds / limits.repair_seconds / inline size.
    deadline_seconds: int | None = None
    repair_seconds: int | None = None
    inline_result_bytes: int | None = None
    # True: the job gets ONE repair in total, shared by every route and member (the first
    # attempt that needs it takes it), instead of one repair per route.
    shared_repair: bool = False

    def uses_judge(self, packet: BaseModel, *, has_sealed: bool = False) -> bool:
        """Whether this packet needs the comparison judge (reserves judge time and planning)."""
        del packet, has_sealed
        return False

    def attempt_seconds(self, packet: BaseModel) -> float | None:
        """Per-packet attempt timeout override, or None to use limits.attempt_seconds."""
        del packet
        return None

    def quick_identity(self, data: dict[str, Any], mode: str | None) -> Any | None:
        """Optional side-effect-free idempotency identity checked before `parse()` on replays."""
        del data, mode
        return None

    def parse(self, data: dict[str, Any], mode: str | None) -> Parsed:
        """Validate a submission (raise ValueError) and split off anything sealed."""
        raise NotImplementedError

    def mode(self, packet: BaseModel) -> str:
        raise NotImplementedError

    def render_prompt(self, packet: BaseModel) -> str:
        raise NotImplementedError

    def memo_schema(self, packet: BaseModel) -> dict[str, Any]:
        raise NotImplementedError

    def validate_memo(
        self,
        data: dict[str, Any],
        packet: BaseModel,
        attempt: AttemptContext | None = None,
    ) -> dict[str, Any]:
        """Return the final memo dict or raise ValueError (which triggers a repair)."""
        raise NotImplementedError

    # -- optional hooks ------------------------------------------------------------
    def dump_packet(self, packet: BaseModel) -> str:
        return packet.model_dump_json(exclude_none=True)

    def load_packet(self, text: str) -> BaseModel:
        return self.packet_model.model_validate_json(text)

    def repair_prompt(
        self,
        prompt: str,
        prior_raw: str | None,
        error: str,
        attempt: AttemptContext | None = None,
    ) -> str:
        del prior_raw, attempt
        return prompt + repair_text(error)

    def judge_repair_prompt(self, prompt: str, prior_raw: str | None, error: str) -> str:
        """The repair prompt for a failed comparison (judge) reply. It is separate from
        `repair_prompt`, which is written for memos: a judge must be asked for the
        comparison schema. `prompt` is the judge prompt the reply answered."""
        return prompt + judge_repair_text(error)

    def repair_overhead_bytes(self, prompt: str, route: Route) -> int | None:
        """Planning bound of what a repair prompt adds to `prompt` for `route`, or None for
        the default (framing plus 4 bytes per output token of the prior reply)."""
        return None

    def compare(self, cx: CompareContext) -> dict[str, Any] | None:
        return None

    def shape_result(self, run: RunView) -> dict[str, Any]:
        return run.base_result

    def shape_status(self, run: dict[str, Any], view: dict[str, Any]) -> dict[str, Any]:
        """`run` is the stored row; `view` is the default status view."""
        return view


class CoreContract(Contract):
    """coldtake's own contract: Packet in, Memo out, sealed answer compared at the end.

    `compare` selects the comparison: "judge" (options path when the packet lists options,
    judge path otherwise), "none", or a callable `(CompareContext) -> dict | None`.
    """

    name = "coldtake_v1"
    packet_model = Packet

    def __init__(self, config: Config | None = None, compare: str | Callable[..., Any] = "judge"):
        self.templates = load_templates(config) if config else {}
        self._compare = compare

    def uses_judge(self, packet: BaseModel, *, has_sealed: bool = False) -> bool:
        if self._compare != "judge":
            return False
        mode = getattr(packet, "mode", "review")
        if mode == "review" or getattr(packet, "options", None):
            return False
        return mode == "panel" or has_sealed

    def parse(self, data: dict[str, Any], mode: str | None) -> Parsed:
        data = dict(data)
        if mode is not None:
            if data.get("mode", mode) != mode:
                raise ValueError(f"packet mode must be {mode!r} for this tool")
            data["mode"] = mode
        submission = Submission.model_validate(data)
        packet, sealed = submission.split()
        warnings = (
            sealed_overlap_warnings(sealed.answer, packet.visible_texts()) if sealed.answer else []
        )
        return Parsed(packet, sealed, submission.model_dump(exclude_none=True), warnings)

    def mode(self, packet: BaseModel) -> str:
        return packet.mode  # type: ignore[attr-defined]

    def render_prompt(self, packet: BaseModel) -> str:
        return render_advisor_prompt(packet, self.templates)  # type: ignore[arg-type]

    def memo_schema(self, packet: BaseModel) -> dict[str, Any]:
        return provider_schema(memo_model(packet), packet.option_ids() or None)  # type: ignore[attr-defined,arg-type]

    def validate_memo(
        self,
        data: dict[str, Any],
        packet: BaseModel,
        attempt: AttemptContext | None = None,
    ) -> dict[str, Any]:
        del attempt
        memo = memo_model(packet).model_validate(data)  # type: ignore[arg-type]
        check_choice(memo, packet)  # type: ignore[arg-type]
        return memo.model_dump(mode="json")

    def compare(self, cx: CompareContext) -> dict[str, Any] | None:
        if callable(self._compare):
            return self._compare(cx)
        packet, sealed, memos = cx.packet, cx.sealed, cx.memos
        if self._compare == "none" or packet.mode == "review":  # type: ignore[attr-defined]
            return None
        if len(memos) < 2 and not sealed.present:
            return not_applicable("one advisor and no sealed answer: nothing to compare")
        if packet.options:  # type: ignore[attr-defined]
            return options_comparison(packet, memos, sealed)  # type: ignore[arg-type]
        return self._judge(cx)

    def _judge(self, cx: CompareContext) -> dict[str, Any]:
        records = [Memo.model_validate(m["memo"]) for m in cx.memos]
        order = judge_labels(cx.request_id, len(records))
        question = cx.packet.question  # type: ignore[attr-defined]
        prompt = render_judge_prompt(question, records, order, cx.sealed.answer)
        has_sealed = cx.sealed.answer is not None

        def validate(data: dict[str, Any]) -> dict[str, Any]:
            return judge_comparison(JudgeOutput.model_validate(data), order, cx.memos, has_sealed)

        try:
            comparison, route = cx.judge(prompt, provider_schema(JudgeOutput), validate)
        except ProviderFailure as failure:
            # The memos are final and valid; a failed comparison never becomes a guess. A paid
            # judge that may have been billed says so by name; the result carries a
            # top-level `charge_unknown` flag too.
            if failure.kind == "charge_unknown":
                return unavailable("charge_unknown", {**failure.cause(), "reason": failure.reason})
            return unavailable(failure.reason, failure.cause())
        comparison["judge"] = {"family": route.family, "model": route.model}
        return comparison


def contract_uses_judge(handler: Contract, packet: BaseModel, *, has_sealed: bool = False) -> bool:
    """Supports both the API v2 method `uses_judge(packet, has_sealed=...)` and legacy bool attr."""
    attr = handler.uses_judge
    if callable(attr):
        try:
            return bool(attr(packet, has_sealed=has_sealed))
        except TypeError:
            return bool(attr(packet))
    return bool(attr)


def _accepts_extra(fn: Callable[..., Any], base_positional: int) -> bool:
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    if any(p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD) for p in params):
        return True
    return len(params) > base_positional


def call_validate_memo(
    handler: Contract,
    data: dict[str, Any],
    packet: BaseModel,
    attempt: AttemptContext | None,
) -> dict[str, Any]:
    if _accepts_extra(handler.validate_memo, 2):
        return handler.validate_memo(data, packet, attempt)
    return handler.validate_memo(data, packet)  # type: ignore[call-arg]


def call_repair_prompt(
    handler: Contract,
    prompt: str,
    prior_raw: str | None,
    error: str,
    attempt: AttemptContext | None,
) -> str:
    if _accepts_extra(handler.repair_prompt, 3):
        return handler.repair_prompt(prompt, prior_raw, error, attempt)
    return handler.repair_prompt(prompt, prior_raw, error)  # type: ignore[call-arg]
