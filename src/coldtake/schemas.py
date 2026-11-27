"""Strict, bounded schemas: the packet advisors see, the sealed input, memos, judge output."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .blindness import has_contamination

REQUEST_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
# Option ids are written in any case and normalized to lower case, so users may use A/B/C.
OPTION_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$"
NONE_CHOICE = "none"
MAX_MEMOS = 8
MAX_LABELS = MAX_MEMOS + 1  # memos plus the sealed answer

Mode = Literal["review", "alternate", "panel"]
Relation = Literal["agree", "partial", "conflict"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# -- packet ------------------------------------------------------------------


class Option(Strict):
    id: str = Field(pattern=OPTION_ID_PATTERN)
    label: str = Field(min_length=1, max_length=300)

    @field_validator("id")
    @classmethod
    def _lower_id(cls, value: str) -> str:
        return value.lower()


ShortText = Field(min_length=1, max_length=4000)


class Packet(Strict):
    """Everything an advisor may see. It has no sealed fields by construction."""

    mode: Mode
    question: str = Field(min_length=1, max_length=20_000)
    goals: list[str] = Field(default_factory=list, max_length=20)
    constraints: list[str] = Field(default_factory=list, max_length=50)
    context: list[str] = Field(default_factory=list, max_length=50)
    options: list[Option] | None = Field(default=None, min_length=2, max_length=8)
    stakes: Literal["low", "material", "high", "critical"] = "material"
    reversibility: Literal["easy", "medium", "difficult", "irreversible"] = "medium"
    primary_answer: str | None = Field(default=None, min_length=1, max_length=100_000)

    @field_validator("goals", "constraints", "context")
    @classmethod
    def _bounded_items(cls, values: list[str]) -> list[str]:
        if any(not item or len(item) > 4000 for item in values):
            raise ValueError("list items must be 1-4000 characters")
        return values

    @model_validator(mode="after")
    def _mode_boundary(self) -> Packet:
        if self.options and len({o.id for o in self.options}) != len(self.options):
            raise ValueError("option ids must be unique")
        if self.options and any(o.id == NONE_CHOICE for o in self.options):
            raise ValueError(f"option id {NONE_CHOICE!r} is reserved")
        if self.mode == "review":
            if not self.primary_answer:
                raise ValueError("review requires primary_answer")
            return self
        if self.primary_answer is not None:
            raise ValueError("blind modes reject primary_answer; use sealed_answer to compare")
        if has_contamination(self.visible_texts()):
            raise ValueError("blind packet contains lead or consensus wording")
        return self

    def visible_texts(self) -> list[str]:
        texts = [self.question, *self.goals, *self.constraints, *self.context]
        texts += [o.label for o in self.options or []]
        return texts

    def option_ids(self) -> list[str]:
        return [o.id for o in self.options or []]


class Sealed(Strict):
    """The lead's own answer. Stored and hashed, never rendered into an advisor prompt."""

    answer: str | None = None
    choice: str | None = None

    @property
    def present(self) -> bool:
        return self.answer is not None or self.choice is not None

    def sha256(self) -> str | None:
        """SHA-256 of the sealed answer text (or of the choice id when only that is given)."""
        text = self.answer if self.answer is not None else self.choice
        return sha256_hex(text) if text is not None else None


class Submission(Packet):
    sealed_answer: str | None = Field(default=None, min_length=1, max_length=100_000)
    sealed_choice: str | None = Field(default=None, min_length=1, max_length=32)

    @field_validator("sealed_choice")
    @classmethod
    def _lower_choice(cls, value: str | None) -> str | None:
        return value.lower() if value is not None else None

    @model_validator(mode="after")
    def _sealed_rules(self) -> Submission:
        if self.mode == "review" and (self.sealed_answer or self.sealed_choice):
            raise ValueError("review rejects sealed_answer; the answer is already visible")
        if self.sealed_choice is not None:
            if not self.options:
                raise ValueError("sealed_choice requires options")
            if self.sealed_choice not in {*self.option_ids(), NONE_CHOICE}:
                raise ValueError("sealed_choice must be an option id or 'none'")
        return self

    def split(self) -> tuple[Packet, Sealed]:
        data = self.model_dump(exclude={"sealed_answer", "sealed_choice"})
        sealed = Sealed(answer=self.sealed_answer, choice=self.sealed_choice)
        return Packet.model_validate(data), sealed


class ReviewPacket(Strict):
    """Typed MCP input for `coldtake_review`: `primary_answer` is required; no sealed fields."""

    mode: Literal["review"] = "review"
    question: str = Field(min_length=1, max_length=20_000)
    primary_answer: str = Field(min_length=1, max_length=100_000)
    goals: list[str] = Field(default_factory=list, max_length=20)
    constraints: list[str] = Field(default_factory=list, max_length=50)
    context: list[str] = Field(default_factory=list, max_length=50)
    options: list[Option] | None = Field(default=None, min_length=2, max_length=8)
    stakes: Literal["low", "material", "high", "critical"] = "material"
    reversibility: Literal["easy", "medium", "difficult", "irreversible"] = "medium"


class BlindPacket(Strict):
    """Typed MCP input for `coldtake_alternate` and `coldtake_panel`: blind by schema."""

    mode: Literal["alternate", "panel"] | None = None
    question: str = Field(min_length=1, max_length=20_000)
    goals: list[str] = Field(default_factory=list, max_length=20)
    constraints: list[str] = Field(default_factory=list, max_length=50)
    context: list[str] = Field(default_factory=list, max_length=50)
    options: list[Option] | None = Field(default=None, min_length=2, max_length=8)
    stakes: Literal["low", "material", "high", "critical"] = "material"
    reversibility: Literal["easy", "medium", "difficult", "irreversible"] = "medium"
    sealed_answer: str | None = Field(default=None, min_length=1, max_length=100_000)
    sealed_choice: str | None = Field(default=None, min_length=1, max_length=32)


# -- one-turn design / architecture check (`coldtake_check`) -----------------

CheckAuthorFamily = Literal["anthropic", "openai", "google", "gemini", "other"]
CheckVerdict = Literal["sound", "concerns", "unsound"]
CheckFallbackReason = Literal["cross_family", "primary_exhausted"]

CHECK_DEFAULT_TIMEOUT_SECONDS = 300
CHECK_MAX_TIMEOUT_SECONDS = 600


class CheckPacket(Strict):
    """Typed MCP input for `coldtake_check`: lightweight one-turn spec/architecture check."""

    question: str = Field(min_length=1, max_length=20_000)
    spec_text: str | None = Field(default=None, min_length=1, max_length=50_000)
    context_paths: list[str] = Field(default_factory=list, max_length=8)
    tradeoffs: str | None = Field(default=None, min_length=1, max_length=20_000)
    author_family: CheckAuthorFamily | None = None
    timeout_s: int = Field(
        default=CHECK_DEFAULT_TIMEOUT_SECONDS, ge=1, le=CHECK_MAX_TIMEOUT_SECONDS
    )

    @field_validator("question")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question required")
        return value

    @field_validator("context_paths")
    @classmethod
    def _valid_paths(cls, values: list[str]) -> list[str]:
        if any(not p or len(p) > 1024 for p in values):
            raise ValueError("context_paths items must be 1-1024 characters")
        if len(set(values)) != len(values):
            raise ValueError("duplicate context path")
        return values


class CheckOutput(Strict):
    """Structured answer of `coldtake_check`, validated locally after every provider."""

    verdict: CheckVerdict
    key_risks: list[str] = Field(max_length=10)
    recommendation: str = Field(min_length=1, max_length=6_000)
    alternatives: list[str] = Field(max_length=5)

    @field_validator("key_risks", "alternatives")
    @classmethod
    def _bounded_items(cls, values: list[str]) -> list[str]:
        if any(not item or len(item) > 2_000 for item in values):
            raise ValueError("list items must be 1-2000 characters")
        return values


class CheckContextFile(Strict):
    """Evidence of one server-host file actually read for `coldtake_check`."""

    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(ge=0)


class CheckAttempt(Strict):
    model: str
    outcome: Literal["succeeded", "exhausted"]


class CheckResult(Strict):
    """Stored result of `coldtake_check`."""

    result_type: Literal["check"] = "check"
    schema_version: Literal[1] = 1
    request_id: str
    packet_hash: str
    verdict: CheckVerdict
    key_risks: list[str]
    recommendation: str
    alternatives: list[str]
    model_used: str
    family_used: str
    fallback_reason: CheckFallbackReason | None = None
    server_host: str
    context_files: list[CheckContextFile] = Field(default_factory=list)
    attempts: list[CheckAttempt]
    billing: Literal["subscription", "paid"] = "subscription"
    created_at: str


# -- memos -------------------------------------------------------------------

Text = Field(min_length=1, max_length=10_000)


class Memo(Strict):
    framing: str = Text
    recommendation: str = Text
    best_alternative: str = Text
    evidence: list[str] = Field(max_length=50)
    assumptions: list[str] = Field(max_length=50)
    value_tradeoffs: list[str] = Field(max_length=50)
    forecasts: list[str] = Field(max_length=50)
    feasibility: list[str] = Field(max_length=50)
    missing_information: list[str] = Field(max_length=50)
    failure_modes: list[str] = Field(max_length=50)
    reversal_evidence: list[str] = Field(max_length=50)
    confidence: float = Field(ge=0, le=1)

    @field_validator(
        "evidence",
        "assumptions",
        "value_tradeoffs",
        "forecasts",
        "feasibility",
        "missing_information",
        "failure_modes",
        "reversal_evidence",
    )
    @classmethod
    def _bounded_items(cls, values: list[str]) -> list[str]:
        if any(not item or len(item) > 10_000 for item in values):
            raise ValueError("list items must be 1-10000 characters")
        return values


class ChoiceMemo(Memo):
    """Memo for a packet with options: one option id, or 'none' with a reason."""

    choice: str = Field(min_length=1, max_length=32)
    choice_reason: str = Field(max_length=10_000)

    @field_validator("choice")
    @classmethod
    def _lower_choice(cls, value: str) -> str:
        return value.strip().lower()


def memo_model(packet: Packet) -> type[Memo]:
    return ChoiceMemo if packet.options else Memo


def check_choice(memo: Memo, packet: Packet) -> None:
    """Raise ValueError when a choice memo violates the packet's options."""
    if not isinstance(memo, ChoiceMemo):
        return
    if memo.choice not in {*packet.option_ids(), NONE_CHOICE}:
        raise ValueError("choice is not one of the packet options")
    if memo.choice == NONE_CHOICE and not memo.choice_reason.strip():
        raise ValueError("choice 'none' requires choice_reason")


# -- judge output ------------------------------------------------------------


class Position(Strict):
    label: str
    stance: str = Field(min_length=1, max_length=1000)


class Pair(Strict):
    a: str
    b: str
    relation: Relation
    crux: str = Field(max_length=2000)


class SealedRelation(Strict):
    relation: Relation
    crux: str = Field(max_length=2000)


class Contradiction(Strict):
    claim: str = Field(min_length=1, max_length=2000)
    labels: list[str] = Field(min_length=2, max_length=MAX_LABELS)


class JudgeOutput(Strict):
    positions: list[Position] = Field(max_length=MAX_LABELS)
    clusters: list[list[str]] = Field(max_length=MAX_LABELS)
    pairwise: list[Pair] = Field(max_length=MAX_LABELS * (MAX_LABELS + 1) // 2)
    sealed: SealedRelation | None
    contradictions: list[Contradiction] = Field(max_length=50)
    material: bool


# -- provider-facing JSON schema ----------------------------------------------

_DROP = {
    "title",
    "default",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
    "pattern",
}


def _inline(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            return _inline(copy.deepcopy(defs[node["$ref"].rsplit("/", 1)[-1]]), defs)
        out = {k: _inline(v, defs) for k, v in node.items() if k not in _DROP}
        if "properties" in out:
            out["required"] = list(out["properties"])
            out["additionalProperties"] = False
        return out
    if isinstance(node, list):
        return [_inline(item, defs) for item in node]
    return node


def provider_schema(model: type[BaseModel], choice_ids: list[str] | None = None) -> dict[str, Any]:
    """Flat, strict JSON schema for provider structured output.

    Size and range keywords are dropped here (not every provider accepts them);
    the local pydantic validation remains authoritative.
    """
    raw = model.model_json_schema()
    schema = _inline(raw, raw.get("$defs", {}))
    schema.pop("$defs", None)
    if choice_ids is not None and "choice" in schema.get("properties", {}):
        schema["properties"]["choice"] = {"type": "string", "enum": [*choice_ids, NONE_CHOICE]}
    return schema
