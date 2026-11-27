"""The public names an extension may rely on, with the private ones kept as aliases."""

from __future__ import annotations

import subprocess
import sys

import pytest

import coldtake
from coldtake import engine as engine_module
from coldtake import extension
from coldtake.providers import CLIProvider, ProcessGroup
from coldtake.routing import Plan
from coldtake.server import build_server

from .conftest import PACKET
from .fakes import FakeRouter, completion, memo
from .helpers import S, spy_rig


def test_the_version_and_api_version_are_importable_and_stable_in_shape():
    assert isinstance(coldtake.__version__, str) and coldtake.__version__
    assert isinstance(coldtake.API_VERSION, int) and coldtake.API_VERSION >= 1
    assert extension.API_VERSION == coldtake.API_VERSION
    assert extension.__version__ == coldtake.__version__


def test_everything_in_the_extension_module_exists():
    for name in extension.__all__:
        assert hasattr(extension, name), name


@pytest.mark.parametrize("namespace", [None, "ns"])
def test_the_record_key_is_public_and_the_private_names_still_work(make_rig, namespace):
    rig = make_rig({"namespace": namespace} if namespace else None)
    engine = rig.engine
    expected = f"{namespace}:abc" if namespace else "abc"
    assert engine.key("abc") == expected == engine._key("abc")
    assert engine.public_id(expected) == "abc" == engine._public_id(expected)


def test_plan_and_plan_view_are_public_with_private_aliases(make_rig):
    rig, _ = spy_rig(make_rig, lambda ctx: Plan(chain=(S("m"),)))
    handler = rig.engine.contracts["coldtake_v1"]
    parsed = handler.parse(PACKET, None)
    prompt = handler.render_prompt(parsed.packet)
    plan = rig.engine.plan(handler, parsed.packet, "anthropic", prompt)
    assert [r.model for r in plan.chain] == ["m"]
    assert rig.engine._plan(handler, parsed.packet, "anthropic", prompt).chain == plan.chain
    view = extension.plan_view(plan)
    assert view == extension.plan_view(plan) == engine_module._plan_view(plan)
    assert [r["model"] for r in view["chain"]] == ["m"] and view["judge_dropped"] == []


@pytest.mark.parametrize("namespace", [None, "ns"])
def test_attempt_ids_parse_back_into_what_was_recorded(make_rig, namespace):
    router = FakeRouter(completion(memo("paid")))
    paid = make_rig({"namespace": namespace} if namespace else None, router=router)
    paid.codex.script(advisor=[{"error": "You've hit your usage limit", "code": 1}])
    paid.run(PACKET)
    with paid.engine.store.connect() as db:
        (row,) = db.execute("SELECT attempt_key FROM ledger").fetchall()
    request_key, attempt_id = extension.split_ledger_key(row["attempt_key"])
    assert request_key == paid.engine.key("req-1")
    parsed = extension.parse_attempt_id(attempt_id)
    assert parsed.role == "advisor" and parsed.slot == 1 and parsed.attempt == 0
    assert parsed.route_index == 1  # the paid route is second on the ladder
    assert (
        extension.format_attempt_id(
            parsed.role, parsed.slot, parsed.token, parsed.seq, parsed.route_index, parsed.attempt
        )
        == attempt_id
    )


def test_parse_attempt_id_rejects_other_strings_and_reads_run_chain_ids():
    parse = extension.parse_attempt_id
    parsed = parse("extension0-0123abcd-7-r2-a1")
    assert (parsed.role, parsed.slot, parsed.token, parsed.seq) == ("extension", 0, "0123abcd", 7)
    assert (parsed.route_index, parsed.attempt) == (2, 1)
    assert parse("judge0-ffffffff-0-r0-a0").role == "judge"
    for bad in ("", "advisor1", "advisor1-xyz-1-r0-a0", "x-1-r0-a0"):
        with pytest.raises(ValueError):
            parse(bad)


def test_the_process_group_and_communicate_are_public_with_private_aliases():
    from coldtake import providers

    assert providers._Group is ProcessGroup and extension.ProcessGroup is ProcessGroup
    assert callable(CLIProvider.communicate) and callable(CLIProvider._communicate)
    provider = CLIProvider(sys.executable)
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )  # fmt: skip
    try:
        out, _ = provider.communicate(ProcessGroup(process), b"hello", 20)
    finally:
        process.wait()
    assert out == b"HELLO"


def test_an_overridden_communicate_is_what_the_provider_runs(tmp_path):
    seen = []

    class Mine(CLIProvider):
        name = "mine"

        def communicate(self, group, payload, timeout):
            seen.append(payload)
            return super().communicate(group, payload, timeout)

    out, _ = Mine(sys.executable)._run(
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
        "ping", 20, str(tmp_path),
    )  # fmt: skip
    assert out == "ping" and seen == [b"ping"]


def test_an_extension_can_set_the_server_instructions(make_rig):
    rig = make_rig({"extensions": ["tests.test_extension_api:register_instructions"]})
    server = build_server(rig.config, rig.engine)
    assert server.instructions == "extension instructions"
    options = server._mcp_server.create_initialization_options()
    assert options.instructions == "extension instructions"


def test_the_default_instructions_are_unchanged_without_an_extension(make_rig):
    rig = make_rig()
    server = build_server(rig.config, rig.engine)
    assert server.instructions and "coldtake_alternate" in server.instructions


def register_instructions(api):
    api.set_instructions("extension instructions")
