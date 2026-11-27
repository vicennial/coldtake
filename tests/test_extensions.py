"""Extension points, exercised with a toy extension (see docs/extending.md)."""

from __future__ import annotations

import asyncio
import importlib.metadata

import pytest

from coldtake.prompts import load_templates
from coldtake.providers import build_providers
from coldtake.server import build_server

from .conftest import PACKET
from .fakes import memo
from .toy_ext import ToyProvider

TOY = {
    "providers": {"toy": "tests.toy_ext:factory"},
    "routing": {"resolver": "tests.toy_ext:resolver"},
    "extensions": ["tests.toy_ext:register"],
}


@pytest.fixture(autouse=True)
def reset_toy():
    ToyProvider.calls, ToyProvider.behaviour = [], "ok"


def test_provider_registry_and_resolver_drive_the_job(make_rig):
    rig = make_rig(TOY)
    out = rig.run({**PACKET, "mode": "alternate"}, lead="anthropic")
    assert out["status"] == "complete"
    assert out["result"]["memos"][0]["model"] == "toy-alternate-for-anthropic"
    assert out["result"]["memos"][0]["family"] == "toyfam"
    assert not rig.codex.calls() and not rig.claude.calls()  # the resolver replaced the ladder


def test_resolver_routes_of_the_lead_family_survive_only_as_the_last_entry(make_rig):
    ToyProvider.behaviour = "lead_first"
    rig = make_rig(TOY)
    out = rig.run(PACKET, lead="toyfam")  # "own" comes first, so it is dropped
    assert [m["model"] for m in out["result"]["memos"]] == ["other"]
    assert out["result"]["memos"][0]["same_family"] is False
    ToyProvider.behaviour = "ok"
    out = rig.run(PACKET, request_id="req-2", lead="toyfam")  # the only route is the lead's own
    assert out["result"]["memos"][0]["same_family"] is True


def test_a_paid_extension_provider_uses_the_ledger_and_charge_semantics(make_rig):
    ToyProvider.behaviour = "unknown_paid"
    rig = make_rig(TOY)
    out = rig.run(PACKET)
    assert out["status"] == "charge_unknown"
    with rig.engine.store.connect() as db:
        row = db.execute("SELECT status, amount FROM ledger").fetchone()
    assert row["status"] == "charge_unknown" and row["amount"] > 0
    assert rig.attempts()[0]["billing"] == "paid"


def test_entry_points_cannot_shadow_builtins_but_config_can_replace_one(make_rig, monkeypatch):
    rig = make_rig()

    class EP:
        def load(self):
            return shadow

    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        lambda group=None: [EP()] if "providers" in group else [],
    )
    with pytest.raises(ValueError, match="already registered"):
        build_providers(rig.config)
    monkeypatch.undo()
    # the config mapping replaces a built-in explicitly, keyed by its name
    cfg = rig.config.model_copy(update={"providers": {"codex": "tests.test_extensions:shadow"}})
    assert type(build_providers(cfg)["codex"]).__name__ == "Shadow"
    wrong = rig.config.model_copy(update={"providers": {"mine": "tests.test_extensions:shadow"}})
    with pytest.raises(ValueError, match="not 'mine'"):
        build_providers(wrong)


def shadow(config, env):
    class Shadow(ToyProvider):
        name = "codex"

    return Shadow(config, env)


def test_entry_point_groups_are_discovered(make_rig, monkeypatch):
    class EP:
        def __init__(self, obj):
            self.obj = obj

        def load(self):
            return self.obj

    from tests import toy_ext

    def fake_entry_points(group=None):
        return {
            "coldtake.providers": [EP(toy_ext.factory)],
            "coldtake.extensions": [EP(toy_ext.register)],
        }.get(group, [])

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)
    rig = make_rig()
    assert "toy" in build_providers(rig.config)
    names = {t.name for t in asyncio.run(build_server(rig.config, rig.engine).list_tools())}
    assert "coldtake_toy_count" in names


def test_extension_tools_share_the_store_and_the_runner(make_rig):
    rig = make_rig(TOY)
    server = build_server(rig.config, rig.engine)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert {"coldtake_toy_count", "coldtake_toy_ask", "coldtake_submit"} <= names
    asked = asyncio.run(server.call_tool("coldtake_toy_ask", {"request_id": "x1", "text": "hi"}))
    assert "toy says via" in str(asked)
    assert any(a["role"] == "extension" for a in rig.attempts("x1"))
    from coldtake.stats import collect

    assert "toy:toy-alternate-for-other" in collect(rig.engine.store)["routes"]
    assert "runs" in str(asyncio.run(server.call_tool("coldtake_toy_count", {})))


def test_tool_aliases_and_prefixes_expose_identical_tools(make_rig):
    rig = make_rig(
        {
            "tools": {
                "extra_prefixes": ["legacy_"],
                "aliases": {"my_second_opinion": "coldtake_alternate"},
            }
        }
    )
    server = build_server(rig.config, rig.engine)
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    for suffix in ("submit", "status", "result", "review", "alternate", "panel"):
        assert f"coldtake_{suffix}" in tools and f"legacy_{suffix}" in tools
    assert "my_second_opinion" in tools
    assert tools["legacy_review"].description == tools["coldtake_review"].description
    assert tools["legacy_review"].inputSchema == tools["coldtake_review"].inputSchema


def test_instructions_are_overridable_inline_and_from_file(make_rig, tmp_path):
    rig = make_rig({"instructions": "Custom house policy."})
    assert build_server(rig.config, rig.engine).instructions == "Custom house policy."
    text = tmp_path / "policy.txt"
    text.write_text("From a file.")
    rig2 = make_rig({"instructions": f"file:{text}"})
    assert build_server(rig2.config, rig2.engine).instructions == "From a file."
    rig3 = make_rig()
    assert "coldtake_alternate" in build_server(rig3.config, rig3.engine).instructions


def test_prompt_templates_are_overridable_and_validated(make_rig):
    rig = make_rig({"prompts": {"alternate": "HOUSE RULES\n{data_rule}\n{request_json}"}})
    rig.codex.script(advisor=[{"json": memo()}], judge=[{"judge_auto": {}}])
    rig.run({**PACKET, "sealed_answer": "SEALED-CANARY-XYZ"})
    stdin = rig.codex.calls("advisor")[0]["stdin"]
    assert "HOUSE RULES" in stdin and "SEALED-CANARY-XYZ" not in stdin
    assert stdin.startswith("COLDTAKE_ROLE: advisor")  # role line is not templated away
    for bad in ("no placeholder here", "{request_json} {sealed_answer}"):
        cfg = rig.config.model_copy(update={"prompts": {"review": bad}})
        with pytest.raises(ValueError):
            load_templates(cfg)


def test_lead_family_and_namespace_come_from_env(tmp_path, monkeypatch):
    from coldtake.config import load_config

    monkeypatch.setenv("COLDTAKE_LEAD_FAMILY", "openai")
    monkeypatch.setenv("COLDTAKE_NAMESPACE", "team-a")
    config = load_config()
    assert (config.lead_family, config.namespace) == ("openai", "team-a")
    from coldtake.routing import detect_lead_family

    assert detect_lead_family("claude-code", "auto") == "anthropic"
    assert detect_lead_family("codex-mcp-client", "auto") == "openai"
    assert detect_lead_family("mystery", "auto") == "other"
    assert detect_lead_family("claude-code", "openai") == "openai"  # config overrides


def test_a_paid_provider_timeout_is_normalized_to_charge_unknown(make_rig):
    ToyProvider.behaviour = "paid_timeout"
    rig = make_rig(TOY)
    out = rig.run(PACKET)
    assert out["status"] == "charge_unknown"
    with rig.engine.store.connect() as db:
        assert db.execute("SELECT status FROM ledger").fetchone()[0] == "charge_unknown"


def test_a_subscription_provider_can_never_be_charge_ambiguous(make_rig):
    ToyProvider.behaviour = "sub_charge"
    rig = make_rig(TOY)
    out = rig.run(PACKET)
    assert out["status"] == "failed" and out["reason"] == "provider_error"
    assert rig.attempts()[0]["billing"] == "subscription"
