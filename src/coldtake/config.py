"""Configuration: packaged defaults, merged with an optional user YAML file."""

from __future__ import annotations

import os
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

_NAME = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"
MODES = ("review", "alternate", "panel")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SubscriptionRoute(_Strict):
    provider: str = Field(pattern=_NAME)
    model: str | None = None
    effort: str | None = None


class PaidModel(_Strict):
    model: str = Field(min_length=1, max_length=200)
    family: str = Field(min_length=1, max_length=64)
    prompt_usd_per_mtok: float = Field(ge=0, le=1000)
    completion_usd_per_mtok: float = Field(gt=0, le=1000)
    max_output_tokens: int = Field(ge=256, le=200_000)
    structured_outputs: bool = True
    # Tokens reserved for chat framing on top of the prompt bytes in the worst-case estimate.
    input_reserve_tokens: int = Field(default=4000, ge=0, le=1_000_000)
    # When set, a prompt that cannot fit (bytes + reserve + max output) skips the route.
    context_tokens: int | None = Field(default=None, ge=1_000)
    # Sent as provider.only: pin the exact endpoints this model may be served from.
    endpoints_only: tuple[str, ...] | None = None


class OpenRouterConfig(_Strict):
    base_url: str = "https://openrouter.ai/api/v1"
    models: tuple[PaidModel, ...] = ()


class Budget(_Strict):
    per_request_usd: float = Field(ge=0, le=100)
    daily_usd: float = Field(ge=0, le=1000)

    @model_validator(mode="after")
    def _daily_covers_request(self) -> Budget:
        if self.daily_usd < self.per_request_usd:
            raise ValueError("daily_usd must be at least per_request_usd")
        return self


class PanelConfig(_Strict):
    max_members: int = Field(ge=2, le=8)
    max_paid_members: int = Field(ge=0, le=3)


class Limits(_Strict):
    max_prompt_bytes: int = Field(ge=1_000, le=1_000_000)
    attempt_seconds: int = Field(ge=1, le=7200)
    job_seconds: int = Field(ge=1, le=14400)
    judge_seconds: int = Field(ge=1, le=7200)
    lease_seconds: int = Field(ge=5, le=3600)
    block_seconds: int = Field(ge=1, le=7200)
    # Maximum jobs executing provider calls at once inside one Engine process.
    max_concurrent_jobs: int = Field(default=8, ge=1, le=64)
    # Separate allowance for the single repair attempt, granted only after the provider
    # returned an inspectable reply. None: the repair shares the normal attempt limits.
    repair_seconds: int | None = Field(default=None, ge=1, le=7200)
    # Largest MCP request body the HTTP transport accepts. None: the library default.
    max_request_bytes: int | None = Field(default=None, ge=1000)
    # How long `serve` waits for running jobs on exit. None waits without a bound.
    drain_seconds: int | None = Field(default=30, ge=0)


class CheckConfig(_Strict):
    # Server-host directories that `coldtake_check` may read `context_paths` from (opt-in).
    context_roots: tuple[str, ...] = ()
    max_context_file_bytes: int = Field(default=150_000, ge=1_024, le=1_000_000)


class RoutingConfig(_Strict):
    # "package.module:callable" returning the route ladder; see docs/extending.md.
    resolver: str | None = None


class ServerConfig(_Strict):
    name: str = "coldtake"
    stateless_http: bool = False


class ToolsConfig(_Strict):
    # False: do not register the built-in coldtake_* tools (an extension supplies its own).
    builtins: bool = True
    # alias name -> built-in tool name, e.g. {"legacy_review": "coldtake_review"}
    aliases: dict[str, str] = {}
    # each prefix p also exposes every built-in "coldtake_x" tool as "p" + "x"
    extra_prefixes: list[str] = []


class Config(_Strict):
    lead_family: str = Field(default="auto", pattern=_NAME)
    # Optional caller namespace: scopes request ids and per-request budget.
    namespace: str | None = Field(default=None, pattern=_NAME)
    state_dir: str | None = None
    binaries: dict[str, str | None] = {}
    # extra providers: name -> "package.module:ClassOrFactory"
    providers: dict[str, str] = {}
    # extensions: "package.module:register" callables, in addition to entry points
    extensions: list[str] = []
    # Optional per-extension settings mappings passed to extensions via ExtensionAPI.
    extensions_config: dict[str, dict[str, Any]] = {}
    # Operator switch: False stops NEW jobs; reading and replaying existing ones still works.
    accepting_new_jobs: bool = True
    server: ServerConfig = ServerConfig()
    routing: RoutingConfig = RoutingConfig()
    tools: ToolsConfig = ToolsConfig()
    check: CheckConfig = CheckConfig()
    # Server instructions text, or "file:/path/to/text".
    instructions: str | None = None
    # Advisor prompt templates per mode, inline or "file:/path". See docs/extending.md.
    prompts: dict[Literal["review", "alternate", "panel"], str] = {}
    subscription: dict[str, SubscriptionRoute]
    openrouter: OpenRouterConfig
    budget: Budget
    panel: PanelConfig
    limits: Limits

    def resolved_state_dir(self) -> Path:
        if self.state_dir:
            return Path(self.state_dir).expanduser()
        base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
        return Path(base) / "coldtake"


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _user_config_path() -> Path | None:
    explicit = os.environ.get("COLDTAKE_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    candidate = Path(base) / "coldtake" / "config.yaml"
    return candidate if candidate.exists() else None


def load_config(path: str | Path | None = None) -> Config:
    raw = yaml.safe_load(files("coldtake").joinpath("default_config.yaml").read_text())
    user = Path(path) if path else _user_config_path()
    if user is not None:
        extra = yaml.safe_load(user.read_text()) or {}
        if not isinstance(extra, dict):
            raise ValueError("configuration must be a mapping")
        raw = _merge(raw, extra)
    if os.environ.get("COLDTAKE_STATE_DIR"):
        raw["state_dir"] = os.environ["COLDTAKE_STATE_DIR"]
    if os.environ.get("COLDTAKE_LEAD_FAMILY"):
        raw["lead_family"] = os.environ["COLDTAKE_LEAD_FAMILY"]
    if os.environ.get("COLDTAKE_NAMESPACE"):
        raw["namespace"] = os.environ["COLDTAKE_NAMESPACE"]
    return Config.model_validate(raw)


def read_text_setting(value: str) -> str:
    """A setting that is either literal text or "file:/path"."""
    if value.startswith("file:"):
        return Path(value[5:]).expanduser().read_text(encoding="utf-8")
    return value
