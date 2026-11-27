"""Tests for API_VERSION = 2 core features:
- Typed MCP schemas (ReviewPacket, BlindPacket, CheckPacket, Submission)
- Built-in CheckContract / coldtake_check (one-turn, context_paths, diskless replay, routing)
- Contract/Extension v2 hooks (AttemptContext, uses_judge(), fallback_after_repair_failure,
  max_output_bytes, live_process_group, Ledger.attempts, Engine.has_run/run_view, Plan.extra,
  RecordSource.claims, extensions_config / api.settings)
- Event-driven Engine.wait() & bounded max_concurrent_jobs
- Built-in GeminiProvider, `coldtake doctor`, `coldtake try`
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
import threading
import time
from typing import Any

import pytest

from coldtake import API_VERSION, extension
from coldtake.check import CONTRACT_CHECK
from coldtake.config import load_config
from coldtake.contracts import AttemptContext, CoreContract
from coldtake.engine import Engine, InvalidRequest
from coldtake.failures import ProviderFailure
from coldtake.providers import CLIProvider, Completion, GeminiProvider, Route, live_process_group
from coldtake.routing import Plan
from coldtake.server import build_server

from .conftest import PACKET
from .fakes import memo
from .helpers import S, spy_rig


def _check_output(verdict: str = "concerns") -> dict[str, Any]:
    return {
        "verdict": verdict,
        "key_risks": ["Single point of failure on local disk."],
        "recommendation": "Add atomic rename and checksum verification.",
        "alternatives": ["Use SQLite WAL mode instead of flat files."],
    }


# -- 1. Typed MCP schemas ----------------------------------------------------


def test_builtin_mcp_tools_expose_typed_schemas(make_rig):
    rig = make_rig()
    server = build_server(rig.config, rig.engine)
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    expected = {
        "coldtake_submit",
        "coldtake_status",
        "coldtake_result",
        "coldtake_review",
        "coldtake_alternate",
        "coldtake_panel",
        "coldtake_check",
    }
    assert expected <= set(tools)

    def packet_props(tool_name: str) -> dict[str, Any]:
        schema = tools[tool_name].inputSchema
        defs = schema.get("$defs", {})
        pkt = schema["properties"]["packet"]
        if "$ref" in pkt:
            pkt = defs[pkt["$ref"].rsplit("/", 1)[-1]]
        return pkt["properties"]

    rev_props = packet_props("coldtake_review")
    assert {"question", "primary_answer"} <= set(rev_props)
    assert "sealed_answer" not in rev_props

    alt_props = packet_props("coldtake_alternate")
    assert {"question", "sealed_answer", "sealed_choice", "options"} <= set(alt_props)
    assert "primary_answer" not in alt_props

    chk_props = set(packet_props("coldtake_check"))
    assert {
        "question",
        "spec_text",
        "context_paths",
        "tradeoffs",
        "author_family",
        "timeout_s",
    } <= chk_props

    sub_props = packet_props("coldtake_submit")
    assert {"mode", "question", "primary_answer", "sealed_answer"} <= set(sub_props)


# -- 2. Built-in CheckContract / coldtake_check ------------------------------


def test_coldtake_check_end_to_end_and_context_files(make_rig, tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    spec_file = root / "spec.md"
    spec_file.write_text("# Design\nUse append-only log.\n", encoding="utf-8")

    rig = make_rig({"check": {"context_roots": [str(root)]}})
    rig.codex.script(advisor=[{"json": _check_output("concerns")}])
    server = build_server(rig.config, rig.engine)

    ack_raw = asyncio.run(
        server.call_tool(
            "coldtake_check",
            {
                "request_id": "chk-1",
                "packet": {
                    "question": "Is append-only log safe here?",
                    "context_paths": [str(spec_file)],
                    "tradeoffs": "Disk growth vs crash recovery",
                    "timeout_s": 42,
                },
            },
        )
    )
    first = ack_raw[0] if isinstance(ack_raw, list) else ack_raw[0][0]
    ack = json.loads(first.text)
    assert ack["request_id"] == "chk-1"
    assert ack["kind"] == "check"
    assert len(ack["context_files"]) == 1
    assert ack["context_files"][0]["path"] == str(spec_file.resolve())
    assert ack["context_files"][0]["bytes"] == len(spec_file.read_bytes())

    done = rig.engine.wait("chk-1", 10)
    assert done["status"] == "complete"
    res = done["result"]
    assert res["result_type"] == "check"
    assert res["verdict"] == "concerns"
    assert res["family_used"] == "openai"
    expected_model = rig.config.subscription["openai"].model or "codex"
    assert res["attempts"] == [{"model": expected_model, "outcome": "succeeded"}]
    assert "Use append-only log." in rig.codex.calls("advisor")[0]["stdin"]

    # Diskless replay via quick_identity works even after the context file is deleted!
    spec_file.unlink()
    replay = rig.engine.submit(
        "chk-1",
        {
            "question": "Is append-only log safe here?",
            "context_paths": [str(spec_file)],
            "tradeoffs": "Disk growth vs crash recovery",
            "timeout_s": 42,
        },
        lead_family="anthropic",
        contract=CONTRACT_CHECK,
    )
    assert replay["status"] == "complete" and replay["request_id"] == "chk-1"


def test_coldtake_check_zero_repairs_and_strict_duplicate_key_rejection(make_rig):
    rig = make_rig()
    # Invalid output: bad verdict -> zero repairs, immediate terminal schema_invalid.
    rig.codex.script(advisor=[{"json": {**_check_output(), "verdict": "maybe"}}])
    rig.engine.submit(
        "chk-bad",
        {"question": "Check this."},
        lead_family="anthropic",
        contract=CONTRACT_CHECK,
    )
    out = rig.engine.wait("chk-bad", 10)
    assert out["status"] == "failed"
    assert out["reason"] == "schema_invalid"
    assert len(rig.codex.calls()) == 1  # max_repairs = 0

    # Duplicate JSON key in raw stdout -> rejected even though json.loads would keep last key.
    dup_raw = (
        '{"verdict":"sound","verdict":"unsound","key_risks":[],'
        '"recommendation":"ok","alternatives":[]}'
    )

    class DupProv:
        name = "dup"

        def available(self) -> bool:
            return True

        def complete(self, prompt: str, schema: dict[str, Any], route: Route, timeout: float):
            del prompt, schema, route, timeout
            return Completion(data=_check_output("unsound"), raw=dup_raw)

    from coldtake.config import SubscriptionRoute

    cfg = load_config().model_copy(
        update={
            "state_dir": str(rig.tmp / "dup-state"),
            "subscription": {"openai": SubscriptionRoute(provider="dup", model="dup-m")},
        }
    )
    eng = Engine(cfg, providers={"dup": DupProv()})
    try:
        eng.submit(
            "chk-dup",
            {"question": "Check duplicate keys."},
            lead_family="anthropic",
            contract=CONTRACT_CHECK,
        )
        out2 = eng.wait("chk-dup", 10)
    finally:
        eng.close()
    assert out2["status"] == "failed"
    assert out2["reason"] == "schema_invalid"


def test_coldtake_check_context_path_security_refusals(make_rig, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    hidden = root / ".env"
    hidden.write_text("TOKEN=1", encoding="utf-8")
    escape_link = root / "link.txt"
    escape_link.symlink_to(outside)
    fifo = root / "pipe"
    os.mkfifo(fifo)
    big = root / "big.txt"
    big.write_text("x" * 2048, encoding="utf-8")

    # Disabled by default when context_roots is empty.
    rig_disabled = make_rig()
    with pytest.raises(InvalidRequest, match="disabled"):
        rig_disabled.engine.submit(
            "c-dis",
            {"question": "q", "context_paths": [str(big)]},
            lead_family="anthropic",
            contract=CONTRACT_CHECK,
        )

    rig = make_rig({"check": {"context_roots": [str(root)], "max_context_file_bytes": 1024}})
    for idx, (path, pattern) in enumerate(
        [
            ("relative.txt", "absolute"),
            (str(outside), "outside"),
            (str(hidden), "hidden"),
            (str(escape_link), "outside"),
            (str(fifo), "not a regular file"),
            (str(big), "exceeds"),
        ]
    ):
        with pytest.raises(InvalidRequest, match=pattern):
            rig.engine.submit(
                f"c-refuse-{idx}",
                {"question": "q", "context_paths": [path]},
                lead_family="anthropic",
                contract=CONTRACT_CHECK,
            )


def test_coldtake_check_cross_family_and_exhaustion_fallback(tmp_path):
    seen_timeouts: list[float] = []

    class FakeProv:
        def __init__(self, name: str, behaviour: str):
            self.name = name
            self.behaviour = behaviour

        def available(self) -> bool:
            return True

        def complete(self, prompt: str, schema: dict[str, Any], route: Route, timeout: float):
            del prompt, schema, route
            seen_timeouts.append(timeout)
            if self.behaviour == "exhaust":
                raise ProviderFailure("usage_limit", "fallback", "out of quota")
            return Completion(data=_check_output("sound"))

    from coldtake.config import SubscriptionRoute

    cfg = load_config().model_copy(
        update={
            "state_dir": str(tmp_path / "state"),
            "subscription": {
                "google": SubscriptionRoute(provider="g", model="gem-check"),
                "openai": SubscriptionRoute(provider="o", model="gpt-check"),
                "anthropic": SubscriptionRoute(provider="a", model="cla-check"),
            },
        }
    )
    engine = Engine(
        cfg,
        providers={
            "g": FakeProv("g", "exhaust"),
            "o": FakeProv("o", "ok"),
            "a": FakeProv("a", "ok"),
        },
    )
    try:
        # 1) Primary google exhausts -> falls back to openai with primary_exhausted and timeout_s.
        engine.submit(
            "chk-fb",
            {"question": "Check fallback", "timeout_s": 77},
            lead_family="anthropic",
            contract=CONTRACT_CHECK,
        )
        res1 = engine.wait("chk-fb", 10)["result"]
        assert res1["model_used"] == "gpt-check"
        assert res1["fallback_reason"] == "primary_exhausted"
        assert [a["outcome"] for a in res1["attempts"]] == ["exhausted", "succeeded"]
        assert seen_timeouts and seen_timeouts[0] == 77.0

        # 2) Author family is gemini/google -> skips google directly with cross_family.
        engine.submit(
            "chk-cf",
            {"question": "Check cross family", "author_family": "gemini"},
            lead_family="anthropic",
            contract=CONTRACT_CHECK,
        )
        res2 = engine.wait("chk-cf", 10)["result"]
        assert res2["family_used"] == "openai"
        assert res2["fallback_reason"] == "cross_family"
    finally:
        engine.close()


# -- 3. API_VERSION = 2 hooks ------------------------------------------------


def test_api_version_2_contract_hooks_attempt_context_and_repair_policy(tmp_path):
    assert API_VERSION == 2 and extension.API_VERSION == 2

    seen_attempts: list[AttemptContext] = []

    class V2Contract(CoreContract):
        name = "v2_hook_test"
        fallback_after_repair_failure = False

        def validate_memo(self, data, packet, attempt: AttemptContext | None = None):
            assert attempt is not None
            seen_attempts.append(attempt)
            if attempt.attempt_index == 0:
                raise ValueError("force repair once")
            return super().validate_memo(data, packet)

        def repair_prompt(self, prompt, prior_raw, error, attempt: AttemptContext | None = None):
            assert attempt is not None and attempt.route.model == "first-model"
            assert attempt.is_repair is True and attempt.attempt_index == 1
            return super().repair_prompt(prompt, prior_raw, error)

    calls: list[str] = []

    class SpyProv:
        name = "spy"

        def available(self) -> bool:
            return True

        def complete(self, prompt: str, schema: dict[str, Any], route: Route, timeout: float):
            del prompt, schema, timeout
            calls.append(route.model or "")
            if len(calls) == 2:
                # Repair attempt raises a fallback error: with fallback_after_repair_failure=False,
                # this must stop terminally rather than jumping to second-model.
                raise ProviderFailure("rate_limit", "fallback", "rate limited during repair")
            return Completion(
                data=memo(),
                raw='{"raw": true}',
                usage={"input_tokens": 11, "output_tokens": 22},
            )

    base = load_config()
    cfg = base.model_copy(
        update={
            "state_dir": str(tmp_path / "state"),
            "routing": base.routing.model_copy(
                update={"resolver": "tests.test_v2_features:_two_route_plan"}
            ),
        }
    )
    engine = Engine(cfg, providers={"spy": SpyProv()})
    try:
        engine.register_contract(V2Contract(cfg))
        engine.submit("v2-1", PACKET, lead_family="anthropic", contract="v2_hook_test")
        out = engine.wait("v2-1", 10)
    finally:
        engine.close()
    assert out["status"] == "failed"
    assert calls == ["first-model", "first-model"]  # did NOT fall back to second-model
    assert len(seen_attempts) == 1
    assert seen_attempts[0].raw == '{"raw": true}'
    assert seen_attempts[0].is_repair is False and seen_attempts[0].attempt_index == 0


def _two_route_plan(ctx):
    del ctx
    return Plan(
        chain=(Route("spy", "openai", "first-model"), Route("spy", "google", "second-model")),
        extra={"dropped_routes": ["skipped-model"]},
    )


def test_plan_extra_has_run_run_view_ledger_attempts_and_record_source_claims(make_rig):
    rig, _ = spy_rig(
        make_rig,
        lambda ctx: Plan(
            chain=(S("m"),),
            extra={"dropped_routes": [{"model": "x", "reason": "too_small"}]},
        ),
    )
    assert not rig.engine.has_run("r-meta")
    out = rig.run(PACKET, request_id="r-meta")
    assert out["status"] == "complete"
    assert rig.engine.has_run("r-meta")

    view = rig.engine.run_view("r-meta", include_result=False)
    assert view is not None
    assert view.plan["extra"] == {"dropped_routes": [{"model": "x", "reason": "too_small"}]}
    assert rig.engine.ledger.attempts("r-meta") == []  # subscription run has no paid ledger rows

    # RecordSource.claims(): replay vs collision
    class ClaimingSource:
        def __init__(self, expected_hash: str):
            self.expected_hash = expected_hash

        def status(self, request_id: str):
            if request_id == "legacy-1":
                return {"request_id": "legacy-1", "status": "complete", "source": "legacy"}
            return None

        def result(self, request_id: str):
            return self.status(request_id)

        def reserved(self, request_id: str) -> bool:
            return request_id == "legacy-1"

        def claims(self, request_id: str, packet_hash: str):
            if request_id != "legacy-1":
                return None
            return "replay" if packet_hash == self.expected_hash else "collision"

    handler = rig.engine.contracts["coldtake_v1"]
    from coldtake.schemas import canonical_json, sha256_hex

    pkt_hash = sha256_hex(canonical_json(handler.parse(PACKET, None).identity))
    rig.engine.register_record_source(ClaimingSource(pkt_hash))

    replayed = rig.engine.submit("legacy-1", PACKET, lead_family="anthropic")
    assert replayed["source"] == "legacy" and replayed["status"] == "complete"
    with pytest.raises(InvalidRequest, match="different packet"):
        rig.engine.submit(
            "legacy-1", {**PACKET, "question": "A different question"}, lead_family="anthropic"
        )


def test_extensions_config_reaches_register_via_api_settings(make_rig):
    captured: list[dict[str, Any]] = []

    def _capture(api):
        captured.append(dict(api.settings))
        captured.append(api.extension_config("tests"))

    global _TEMP_REGISTER
    _TEMP_REGISTER = _capture
    rig = make_rig(
        {
            "extensions": ["tests.test_v2_features:_temp_register"],
            "extensions_config": {"tests": {"legacy_root": "/example/path"}},
        }
    )
    build_server(rig.config, rig.engine)
    assert captured == [{"legacy_root": "/example/path"}, {"legacy_root": "/example/path"}]


_TEMP_REGISTER = lambda api: None  # noqa: E731


def _temp_register(api):
    _TEMP_REGISTER(api)


def test_cli_provider_max_output_bytes_override_and_live_process_group():
    prov = CLIProvider(sys.executable)
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import sys; sys.stdout.write('a' * 5000)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    with live_process_group(proc) as group:
        with pytest.raises(ProviderFailure) as exc_info:
            prov.communicate(group, b"", 10, max_output_bytes=1024)
    assert exc_info.value.reason == "output_too_large"
    assert group.reaped is True


# -- 4. Event-driven wait & max_concurrent_jobs ------------------------------


def test_event_driven_wait_returns_without_polling_delay(make_rig):
    rig, _ = spy_rig(make_rig, lambda ctx: Plan(chain=(S("fast"),)))
    rig.engine.submit("fast-1", PACKET, lead_family="anthropic")
    started = time.monotonic()
    out = rig.engine.wait("fast-1", 5)
    elapsed = time.monotonic() - started
    assert out["status"] == "complete"
    assert elapsed < 0.2  # well under the old 0.25s polling interval


def test_max_concurrent_jobs_caps_active_provider_calls(tmp_path):
    active = 0
    peak = 0
    lock = threading.Lock()

    class SlowProv:
        name = "slow"

        def available(self) -> bool:
            return True

        def complete(self, prompt: str, schema: dict[str, Any], route: Route, timeout: float):
            nonlocal active, peak
            del prompt, schema, route, timeout
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.08)
            with lock:
                active -= 1
            return Completion(data=memo())

    from coldtake.config import SubscriptionRoute

    base = load_config()
    cfg = base.model_copy(
        update={
            "state_dir": str(tmp_path / "state"),
            "limits": base.limits.model_copy(update={"max_concurrent_jobs": 2}),
            "subscription": {"openai": SubscriptionRoute(provider="slow", model="s1")},
        }
    )
    engine = Engine(cfg, providers={"slow": SlowProv()})
    try:
        for i in range(4):
            engine.submit(f"conc-{i}", PACKET, lead_family="anthropic")
        for i in range(4):
            assert engine.wait(f"conc-{i}", 10)["status"] == "complete"
    finally:
        engine.close()
    assert peak == 2


# -- 5. GeminiProvider + CLI doctor & try ------------------------------------


def test_gemini_provider_runs_cli_and_parses_envelope(tmp_path):
    script = tmp_path / "fake-gemini"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "stdin = sys.stdin.read()\n"
        "payload = {\n"
        "    'verdict': 'sound',\n"
        "    'key_risks': [],\n"
        "    'recommendation': stdin.strip(),\n"
        "    'alternatives': [],\n"
        "}\n"
        "print(json.dumps({'response': json.dumps(payload)}))\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    prov = GeminiProvider(str(script))
    assert prov.available()
    comp = prov.complete(
        "Check this architecture",
        {"type": "object"},
        Route("gemini", "google", "gemini-2.5-pro"),
        10,
    )
    assert comp.data["verdict"] == "sound"
    assert comp.data["recommendation"] == "Check this architecture"


def test_cli_doctor_and_try_offline(tmp_path):
    env = {
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "OPENROUTER_API_KEY": "example-secret-never-print-this",
    }
    doc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "coldtake", "doctor", "--json"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert "example-secret-never-print-this" not in doc.stdout
    data = json.loads(doc.stdout)
    assert data["api_version"] == 2
    assert data["providers"]["openrouter"]["api_key_set"] is True
    assert "gemini" in data["providers"]

    demo = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "coldtake", "try", "--json"],
        capture_output=True,
        text=True,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        check=True,
    )
    demo_data = json.loads(demo.stdout)
    assert demo_data["offline_demo"] is True
    assert demo_data["sealed_canary_leaked"] is False
    assert demo_data["alternate"]["comparison"]["sealed"]["relation"] == "partial"
    assert demo_data["panel"]["comparison"]["sealed"]["relation"] == "conflict"
    assert demo_data["check"]["verdict"] == "concerns"

    # Also verify human-readable text output doesn't crash.
    human = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "coldtake", "try"],
        capture_output=True,
        text=True,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        check=True,
    )
    assert "offline demo" in human.stdout
