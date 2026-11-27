"""A toy extension, used by the tests and mirrored in docs/extending.md."""

from __future__ import annotations

from typing import Any

from coldtake.failures import ProviderFailure
from coldtake.providers import Completion, Route
from coldtake.schemas import Memo

from .fakes import memo


class ToyProvider:
    """Same interface as the built-in providers."""

    name = "toy"
    calls: list[str] = []
    behaviour = "ok"

    def __init__(self, config: Any, env: Any):
        self.config = config

    def available(self) -> bool:
        return True

    def complete(self, prompt: str, schema: dict, route: Route, timeout: float) -> Completion:
        ToyProvider.calls.append(route.model or "")
        if ToyProvider.behaviour == "unknown_paid":
            raise ProviderFailure("charge_unknown", "charge_unknown", "toy lost the reply")
        if ToyProvider.behaviour == "paid_timeout":
            raise ProviderFailure("timed_out", "timed_out", "toy was slow")
        if ToyProvider.behaviour == "sub_charge":
            raise ProviderFailure("charge_unknown", "charge_unknown", "misused kind")
        data = memo(f"toy says via {route.model}")
        if "choice" in schema["properties"]:
            data.update(choice=schema["properties"]["choice"]["enum"][0], choice_reason="toy")
        assert Memo.model_validate({k: v for k, v in data.items() if k in Memo.model_fields})
        return Completion(data, cost_usd=0.002 if route.billing == "paid" else None)


def factory(config: Any, env: Any) -> ToyProvider:
    return ToyProvider(config, env)


def resolver(ctx: Any) -> list[Route]:
    routes = [Route("toy", "toyfam", f"toy-{ctx.mode}-for-{ctx.lead_family}")]
    if ToyProvider.behaviour == "lead_first":
        routes = [Route("toy", ctx.lead_family, "own"), Route("toy", "toyother", "other")]
    if ToyProvider.behaviour in {"unknown_paid", "paid_timeout"}:
        routes = [
            Route("toy", "toyfam", "toy-paid", None, "paid", 1.0, 1.0, 1000),
        ]
    return routes


def register(api: Any) -> None:
    @api.tool("coldtake_toy_count", "Count stored runs (toy extension).")
    async def count() -> dict:
        with api.engine.store.connect() as db:
            return {"runs": db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]}

    @api.tool("coldtake_toy_ask", "Ask the toy ladder directly (toy extension).")
    async def ask(request_id: str, text: str) -> dict:
        routes = [Route("toy", "toyfam", "toy-alternate-for-other")]
        value, route = api.engine.run_chain(
            request_id, routes, text, {"type": "object", "properties": {}}, lambda d: d
        )
        return {"model": route.model, "recommendation": value["recommendation"]}
