"""MCP server: typed job tools, blocking conveniences, aliases and the extension API."""

from __future__ import annotations

import asyncio
import importlib.metadata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any

from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field

from .check import CONTRACT_CHECK
from .config import Config, load_config, read_text_setting
from .contracts import Contract
from .engine import Engine, RecordSource
from .providers import load_object
from .routing import detect_lead_family
from .schemas import (
    REQUEST_ID_PATTERN,
    BlindPacket,
    CheckPacket,
    ReviewPacket,
    Submission,
)

TOOL_PREFIX = "coldtake_"
RequestId = Annotated[str, Field(pattern=REQUEST_ID_PATTERN)]

DEFAULT_INSTRUCTIONS = """\
coldtake gives a blind second opinion from a different model family.
- Routine facts and reversible steps: stay solo. Do not call coldtake.
- An existing answer with correctness risk: coldtake_review.
- A lightweight one-turn spec, design or architecture check: coldtake_check (async; poll
  coldtake_status / coldtake_result with the same request_id).
- A material decision (irreversible, novel or uncertain, anchoring risk, or several options
  with different downsides): coldtake_alternate BEFORE presenting a recommendation. Pass
  your own answer as sealed_answer (and sealed_choice if you list options). It is stored and
  hashed, never shown to the advisor, and compared after the advisor's memo is final.
- coldtake_panel only on explicit request, for high-stakes irreversible decisions, or when
  your answer and the alternate conflict.
- Prefer coldtake_submit then coldtake_status/coldtake_result with the same request_id for
  long jobs. A retried request_id never starts a second job or a second charge.
- Treat charge_unknown, timed_out and lost as terminal; report the cause. Disclose the mode
  used and any paid spend.
"""


def client_name(ctx: Context | None) -> str | None:
    try:
        return ctx.session.client_params.clientInfo.name  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        return None


@dataclass
class ExtensionAPI:
    """What an extension receives. It can add tools to the same server and reuse the
    engine: `engine.store` (job store), `engine.ledger` (budget), `engine.providers`,
    `engine.run_chain`, `settings` (from `extensions_config`) and `config`."""

    mcp: FastMCP
    engine: Engine
    config: Config
    settings: Mapping[str, Any] = field(default_factory=dict)

    def extension_config(self, name: str) -> dict[str, Any]:
        """Return a copy of the configured `extensions_config.<name>` mapping, if any."""
        return dict(self.config.extensions_config.get(name, {}))

    def tool(self, name: str, description: str | None = None) -> Callable[[Any], Any]:
        """Register a tool under `name` plus any configured aliases of it."""

        def decorate(function: Any) -> Any:
            for exposed in tool_names(self.config, name):
                self.mcp.tool(name=exposed, description=description)(function)
            return function

        return decorate

    def lead_family(self, ctx: Context | None) -> str:
        return detect_lead_family(client_name(ctx), self.config.lead_family)

    def set_instructions(self, text: str) -> None:
        """Replace the server instructions the MCP client is shown at initialization (the
        default, or `instructions:` from the config). Call it from `register(api)`."""
        self.mcp._mcp_server.instructions = text

    @property
    def instructions(self) -> str | None:
        return self.mcp.instructions

    def register_contract(self, contract: Contract) -> None:
        self.engine.register_contract(contract)

    def register_record_source(self, source: RecordSource) -> None:
        self.engine.register_record_source(source)


def tool_names(config: Config, builtin: str) -> list[str]:
    names = [builtin]
    if builtin.startswith(TOOL_PREFIX):
        suffix = builtin[len(TOOL_PREFIX) :]
        names += [prefix + suffix for prefix in config.tools.extra_prefixes]
    names += [alias for alias, target in config.tools.aliases.items() if target == builtin]
    return list(dict.fromkeys(names))


def _extension_settings(config: Config, name: str, spec: str | None = None) -> dict[str, Any]:
    if spec and spec in config.extensions_config:
        return dict(config.extensions_config[spec])
    if name in config.extensions_config:
        return dict(config.extensions_config[name])
    short = name.split(".", 1)[0]
    if short in config.extensions_config:
        return dict(config.extensions_config[short])
    return {}


def _extensions(config: Config) -> list[tuple[str, str | None, Callable[[ExtensionAPI], None]]]:
    found: list[tuple[str, str | None, Callable[[ExtensionAPI], None]]] = []
    for ep in importlib.metadata.entry_points(group="coldtake.extensions"):
        ep_name = getattr(ep, "name", "") or ""
        found.append((ep_name, None, ep.load()))
    for path in config.extensions:
        module_name = path.partition(":")[0]
        found.append((module_name, path, load_object(path)))
    return found


def build_server(
    config: Config | None = None,
    engine: Engine | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> FastMCP:
    config = config or load_config()
    engine = engine or Engine(config)
    instructions = (
        read_text_setting(config.instructions) if config.instructions else DEFAULT_INSTRUCTIONS
    )
    options: dict[str, Any] = {
        "instructions": instructions,
        "host": host,
        "port": port,
        "stateless_http": config.server.stateless_http,
    }
    if config.limits.max_request_bytes:
        options["max_request_body_size"] = config.limits.max_request_bytes
    mcp = FastMCP(config.server.name, **options)
    api = ExtensionAPI(mcp, engine, config)
    if config.tools.builtins:
        _register_builtins(api, engine, config)
    for name, spec, register in _extensions(config):
        ext_api = ExtensionAPI(
            mcp,
            engine,
            config,
            settings=_extension_settings(config, name, spec),
        )
        register(ext_api)
    mcp.coldtake_engine = engine  # type: ignore[attr-defined]
    return mcp


def _register_builtins(api: ExtensionAPI, engine: Engine, config: Config) -> None:
    block = config.limits.block_seconds

    @api.tool("coldtake_submit", "Start a job and return an ACK. Poll with the same request_id.")
    async def submit(request_id: RequestId, packet: Submission, ctx: Context) -> dict:
        lead = api.lead_family(ctx)
        payload = packet.model_dump(exclude_none=True)
        return await asyncio.to_thread(engine.submit, request_id, payload, lead_family=lead)

    @api.tool("coldtake_status", "Status of a job. Stale running jobs become terminal here.")
    async def status(request_id: RequestId) -> dict:
        return await asyncio.to_thread(engine.status, request_id)

    @api.tool("coldtake_result", "The result of a finished job, or its current status.")
    async def result(request_id: RequestId) -> dict:
        return await asyncio.to_thread(engine.result, request_id)

    @api.tool(
        "coldtake_review",
        "Critique an existing answer (primary_answer is required). Blocks.",
    )
    async def review(request_id: RequestId, packet: ReviewPacket, ctx: Context) -> dict:
        lead = api.lead_family(ctx)
        payload = packet.model_dump(exclude_none=True)

        def go() -> dict:
            engine.submit(request_id, payload, lead_family=lead, mode="review")
            return engine.wait(request_id, block)

        return await asyncio.to_thread(go)

    def blocking_blind(mode: str, description: str) -> None:
        async def run(request_id: RequestId, packet: BlindPacket, ctx: Context) -> dict:
            lead = api.lead_family(ctx)
            payload = packet.model_dump(exclude_none=True)

            def go() -> dict:
                engine.submit(request_id, payload, lead_family=lead, mode=mode)
                return engine.wait(request_id, block)

            return await asyncio.to_thread(go)

        run.__name__ = f"coldtake_{mode}"
        api.tool(f"coldtake_{mode}", description)(run)

    blocking_blind(
        "alternate",
        "Independent opinion; may carry a sealed_answer to compare. Blocks.",
    )
    blocking_blind(
        "panel",
        "Independent opinions from distinct model families (default three). Blocks.",
    )

    @api.tool(
        "coldtake_check",
        (
            "Lightweight asynchronous one-turn spec/design/architecture check from a different "
            "model family. Returns an ACK immediately; poll coldtake_status / coldtake_result."
        ),
    )
    async def check(request_id: RequestId, packet: CheckPacket, ctx: Context) -> dict:
        lead = api.lead_family(ctx)
        payload = packet.model_dump(exclude_none=True)
        return await asyncio.to_thread(
            engine.submit,
            request_id,
            payload,
            lead_family=lead,
            contract=CONTRACT_CHECK,
        )

    @api.mcp.resource("coldtake://stats", mime_type="application/json")
    async def stats_resource() -> str:
        """Read-only JSON summary of local runs, conflict rates, verdicts, and spend."""
        import json

        from .stats import collect

        return json.dumps(await asyncio.to_thread(collect, engine.store), sort_keys=True)

    @api.mcp.resource("coldtake://history", mime_type="application/json")
    async def history_resource() -> str:
        """Read-only JSON list of the 50 most recent local coldtake runs."""
        import json

        from .stats import list_runs

        return json.dumps(
            await asyncio.to_thread(list_runs, engine.store, limit=50), sort_keys=True
        )
