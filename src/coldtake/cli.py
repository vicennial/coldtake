"""Command line interface for coldtake MCP server, direct queries, stats, and onboarding."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import signal
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import API_VERSION, __version__
from .config import Config, SubscriptionRoute, _user_config_path, load_config
from .onboarding import CLIENTS, install_mcp, mcp_snippet, write_init_config

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _is_loopback(host: str) -> bool:
    return host in LOOPBACK_HOSTS


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coldtake", description=__doc__)
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    parser.add_argument("--config", help="path to a config YAML (default: user config)")
    parser.set_defaults(command="serve", transport="stdio", host="127.0.0.1", port=8000)
    commands = parser.add_subparsers(dest="command")

    serve = commands.add_parser("serve", help="run the MCP server (default)")
    serve.add_argument("--config", default=argparse.SUPPRESS)
    serve.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    serve.add_argument("--host", default="127.0.0.1", help="http only; 127.0.0.1, localhost or ::1")
    serve.add_argument("--port", type=int, default=8000, help="http only")

    stats = commands.add_parser("stats", help="summaries from local run state")
    stats.add_argument("--config", default=argparse.SUPPRESS)
    stats.add_argument("--days", type=float, help="only runs from the last N days")
    stats.add_argument("--json", action="store_true", help="machine-readable output")
    stats.add_argument(
        "--share",
        action="store_true",
        help="anonymized copy-pasteable scorecard (no prompt text or private details)",
    )

    history = commands.add_parser("history", help="list recent local runs")
    history.add_argument("--config", default=argparse.SUPPRESS)
    history.add_argument("--limit", type=int, default=20, help="maximum runs to show (default: 20)")
    history.add_argument("--mode", choices=["review", "alternate", "panel", "check"])
    history.add_argument("--status")
    history.add_argument("--json", action="store_true", help="machine-readable output")

    show = commands.add_parser("show", help="inspect one stored run by request_id")
    show.add_argument("--config", default=argparse.SUPPRESS)
    show.add_argument("request_id", help="request_id (or un-namespaced suffix if unique)")
    show.add_argument("--json", action="store_true", help="machine-readable output")

    report = commands.add_parser(
        "report", help="generate a self-contained interactive HTML scorecard"
    )
    report.add_argument("--config", default=argparse.SUPPRESS)
    report.add_argument("--days", type=float, help="only runs from the last N days")
    report.add_argument("--limit", type=int, default=50, help="recent runs to include")
    report.add_argument("--out", help="write HTML to this file (default: stdout)")

    ask = commands.add_parser("ask", help="run one blind alternate, review, or panel from the CLI")
    ask.add_argument("--config", default=argparse.SUPPRESS)
    ask.add_argument("question", help="decision question or prompt to evaluate")
    ask.add_argument("--mode", choices=["alternate", "review", "panel"], default="alternate")
    ask.add_argument("--primary-answer", help="existing answer to critique (required for review)")
    ask.add_argument("--sealed", help="your own answer to seal and compare after memos are final")
    ask.add_argument("--goal", action="append", default=[], help="goal (repeatable)")
    ask.add_argument("--constraint", action="append", default=[], help="constraint (repeatable)")
    ask.add_argument("--context", action="append", default=[], help="context note (repeatable)")
    ask.add_argument("--lead", help="override lead family (default: config.lead_family or 'other')")
    ask.add_argument("--request-id", help="explicit idempotent request_id")
    ask.add_argument("--json", action="store_true", help="machine-readable output")

    check = commands.add_parser("check", help="run a one-turn spec/architecture check from the CLI")
    check.add_argument("--config", default=argparse.SUPPRESS)
    check.add_argument("question", help="question to evaluate")
    check.add_argument("--spec", help="inline spec or design text")
    check.add_argument(
        "--file",
        action="append",
        default=[],
        help="local UTF-8 file to include as context (repeatable)",
    )
    check.add_argument("--tradeoffs", help="known tradeoffs to consider")
    check.add_argument(
        "--author",
        choices=["anthropic", "openai", "google", "gemini", "other"],
        help="model family that wrote the design (excluded from reviewing its own work)",
    )
    check.add_argument("--timeout", type=int, default=300, help="attempt timeout seconds (1-600)")
    check.add_argument("--lead", help="override lead family")
    check.add_argument("--request-id", help="explicit idempotent request_id")
    check.add_argument("--json", action="store_true", help="machine-readable output")

    doctor = commands.add_parser("doctor", help="inspect providers, ladders, limits and state")
    doctor.add_argument("--config", default=argparse.SUPPRESS)
    doctor.add_argument("--json", action="store_true", help="machine-readable output")

    init = commands.add_parser("init", help="write a starter config.yaml from detected CLIs")
    init.add_argument("--config", default=argparse.SUPPRESS)
    init.add_argument("--path", help="output path (default: ~/.config/coldtake/config.yaml)")
    init.add_argument("--force", action="store_true", help="overwrite an existing config file")

    install = commands.add_parser(
        "install", help="configure coldtake as an MCP server for claude, codex, gemini, or cursor"
    )
    install.add_argument("client", choices=list(CLIENTS))
    install.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="print the config snippet instead of editing the client config file",
    )
    install.add_argument(
        "--with-skill",
        action="store_true",
        help="also install the coldtake SKILL.md for this client",
    )
    install.add_argument(
        "--command",
        dest="server_command",
        default="uvx coldtake",
        help="MCP server command (default: 'uvx coldtake')",
    )
    install.add_argument("--json", action="store_true", help="machine-readable output")

    demo = commands.add_parser(
        "try", help="run a zero-network, zero-API-key local demo in a temp directory"
    )
    demo.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.version:
        print(f"coldtake {__version__}")
        return 0
    if args.command == "try":
        return _run_try(as_json=args.json)
    if args.command == "install":
        return _run_install(args)
    config = load_config(args.config)
    if args.command == "init":
        return _run_init(config, args)
    if args.command == "stats":
        from .stats import collect, render, render_share
        from .store import Store

        data = collect(Store(config.resolved_state_dir()), args.days)
        if args.json:
            print(json.dumps(data, indent=2, sort_keys=True))
        elif args.share:
            print(render_share(data))
        else:
            print(render(data))
        return 0
    if args.command == "history":
        from .stats import list_runs, render_history
        from .store import Store

        items = list_runs(
            Store(config.resolved_state_dir()),
            limit=args.limit,
            mode=args.mode,
            status=args.status,
        )
        print(json.dumps(items, indent=2, sort_keys=True) if args.json else render_history(items))
        return 0
    if args.command == "show":
        from .stats import get_run_detail, render_run_detail
        from .store import Store

        try:
            detail = get_run_detail(Store(config.resolved_state_dir()), args.request_id)
        except ValueError as exc:
            print(f"coldtake: {exc}", file=sys.stderr)
            return 2
        if detail is None:
            print(f"coldtake: run not found: {args.request_id}", file=sys.stderr)
            return 1
        print(
            json.dumps(detail, indent=2, sort_keys=True) if args.json else render_run_detail(detail)
        )
        return 0
    if args.command == "report":
        from .report_html import build_report_payload, render_html
        from .store import Store

        html = render_html(
            build_report_payload(
                Store(config.resolved_state_dir()), days=args.days, limit=args.limit
            )
        )
        if args.out:
            out_path = Path(args.out).expanduser()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = out_path.with_name(f".{out_path.name}.tmp.{os.getpid()}")
            tmp.write_text(html, encoding="utf-8")
            os.replace(tmp, out_path)
            print(f"Wrote scorecard: {out_path}")
        else:
            print(html)
        return 0
    if args.command == "ask":
        return _run_ask(config, args)
    if args.command == "check":
        return _run_check(config, args)
    if args.command == "doctor":
        return _run_doctor(config, config_arg=args.config, as_json=args.json)
    return _serve(args, config)


def _run_install(args: argparse.Namespace) -> int:
    if args.print_only:
        print(mcp_snippet(args.client, command=args.server_command), end="")
        return 0
    try:
        info = install_mcp(args.client, command=args.server_command, with_skill=args.with_skill)
    except (OSError, ValueError) as exc:
        print(f"coldtake: install failed: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(info, indent=2, sort_keys=True))
    else:
        print(f"Installed coldtake MCP config for {info['client']}: {info['config_path']}")
        if info["skill_path"]:
            print(f"Installed SKILL.md: {info['skill_path']}")
        print("Restart your client to pick up the new coldtake tools.")
    return 0


def _run_init(config: Config, args: argparse.Namespace) -> int:
    report = _collect_doctor(config, args.config)
    target = Path(args.path).expanduser() if args.path else None
    try:
        written = write_init_config(report["providers"], path=target, force=args.force)
    except FileExistsError as exc:
        print(f"coldtake: {exc}", file=sys.stderr)
        return 2
    print(f"Wrote starter config: {written}")
    print("Run `coldtake doctor` to inspect detected providers and ladders.")
    return 0


def _run_ask(config: Config, args: argparse.Namespace) -> int:
    from .engine import Engine, InvalidRequest
    from .stats import get_run_detail, render_run_detail

    lead = args.lead or (config.lead_family if config.lead_family != "auto" else "other")
    rid = args.request_id or f"cli-{uuid.uuid4().hex[:12]}"
    packet: dict[str, Any] = {
        "mode": args.mode,
        "question": args.question,
        "goals": list(args.goal),
        "constraints": list(args.constraint),
        "context": list(args.context),
    }
    if args.primary_answer is not None:
        packet["primary_answer"] = args.primary_answer
    if args.sealed is not None:
        packet["sealed_answer"] = args.sealed

    engine = Engine(config)
    try:
        try:
            engine.submit(rid, packet, lead_family=lead, mode=args.mode)
        except InvalidRequest as exc:
            print(f"coldtake: {exc}", file=sys.stderr)
            return 2
        out = engine.wait(rid, config.limits.block_seconds)
        if args.json:
            print(json.dumps(out, indent=2, sort_keys=True))
        else:
            detail = get_run_detail(engine.store, engine.key(rid))
            print(render_run_detail(detail) if detail else json.dumps(out, indent=2))
        return 0 if out.get("status") == "complete" else 1
    finally:
        engine.close()


def _run_check(config: Config, args: argparse.Namespace) -> int:
    from .check import CONTRACT_CHECK
    from .engine import Engine, InvalidRequest
    from .stats import get_run_detail, render_run_detail

    lead = args.lead or (config.lead_family if config.lead_family != "auto" else "other")
    rid = args.request_id or f"cli-check-{uuid.uuid4().hex[:12]}"
    resolved_files = [str(Path(f).expanduser().resolve()) for f in args.file]
    if resolved_files:
        # Local human CLI user explicitly passed --file paths; allow their parent directories
        # for this one CLI invocation (MCP server context_roots remains opt-in).
        extra_roots = tuple(
            dict.fromkeys(
                [*config.check.context_roots, *(str(Path(p).parent) for p in resolved_files)]
            )
        )
        config = config.model_copy(
            update={"check": config.check.model_copy(update={"context_roots": extra_roots})}
        )
    packet: dict[str, Any] = {
        "question": args.question,
        "context_paths": resolved_files,
        "timeout_s": args.timeout,
    }
    if args.spec is not None:
        packet["spec_text"] = args.spec
    if args.tradeoffs is not None:
        packet["tradeoffs"] = args.tradeoffs
    if args.author is not None:
        packet["author_family"] = args.author

    engine = Engine(config)
    try:
        try:
            engine.submit(rid, packet, lead_family=lead, contract=CONTRACT_CHECK)
        except InvalidRequest as exc:
            print(f"coldtake: {exc}", file=sys.stderr)
            return 2
        out = engine.wait(rid, max(config.limits.block_seconds, args.timeout + 60))
        if args.json:
            print(json.dumps(out, indent=2, sort_keys=True))
        else:
            detail = get_run_detail(engine.store, engine.key(rid))
            print(render_run_detail(detail) if detail else json.dumps(out, indent=2))
        return 0 if out.get("status") == "complete" else 1
    finally:
        engine.close()


def _route_dict(route: Any) -> dict[str, Any]:
    return {
        "provider": route.provider,
        "family": route.family,
        "model": route.model,
        "effort": route.effort,
        "billing": route.billing,
    }


def _collect_doctor(config: Config, config_arg: str | None) -> dict[str, Any]:
    from .check import CONTRACT_CHECK
    from .providers import build_providers
    from .routing import RouteContext, plan_routes

    providers = build_providers(config)
    provider_info: dict[str, dict[str, Any]] = {}
    for name, prov in providers.items():
        info: dict[str, Any] = {"available": prov.available()}
        if hasattr(prov, "executable"):
            info["executable"] = prov.executable
        if name == "openrouter":
            info["api_key_set"] = bool(os.environ.get("OPENROUTER_API_KEY"))
        provider_info[name] = info

    ladders: dict[str, dict[str, Any]] = {}
    for lead in ("anthropic", "openai", "google"):
        entry: dict[str, Any] = {}
        for mode, contract_name in (
            ("alternate", "coldtake_v1"),
            ("panel", "coldtake_v1"),
            ("check", CONTRACT_CHECK),
        ):
            ctx = RouteContext(
                lead_family=lead,
                mode=mode,
                packet=None,
                contract=contract_name,
                namespace=config.namespace,
                prompt_bytes=0,
                config=config,
                providers=providers,
            )
            try:
                plan = plan_routes(ctx)
            except Exception as exc:
                entry[mode] = {"error": f"{type(exc).__name__}: {exc}"}
                continue
            if mode == "panel":
                entry[mode] = [[_route_dict(r) for r in slot] for slot in plan.slots]
            else:
                entry[mode] = [_route_dict(r) for r in plan.chain]
        ladders[lead] = entry

    ep_names = [
        getattr(ep, "name", "")
        for ep in importlib.metadata.entry_points(group="coldtake.extensions")
    ]
    state_dir = config.resolved_state_dir()
    cfg_path = config_arg or (str(_user_config_path()) if _user_config_path() else None)
    return {
        "version": __version__,
        "api_version": API_VERSION,
        "config_path": cfg_path,
        "state_dir": str(state_dir),
        "state_dir_exists": state_dir.exists(),
        "lead_family": config.lead_family,
        "namespace": config.namespace,
        "accepting_new_jobs": config.accepting_new_jobs,
        "resolver": config.routing.resolver,
        "providers": provider_info,
        "ladders": ladders,
        "budget": {
            "per_request_usd": config.budget.per_request_usd,
            "daily_usd": config.budget.daily_usd,
        },
        "limits": {
            "max_concurrent_jobs": config.limits.max_concurrent_jobs,
            "attempt_seconds": config.limits.attempt_seconds,
            "job_seconds": config.limits.job_seconds,
            "judge_seconds": config.limits.judge_seconds,
            "block_seconds": config.limits.block_seconds,
            "drain_seconds": config.limits.drain_seconds,
        },
        "check": {
            "context_roots": list(config.check.context_roots),
            "max_context_file_bytes": config.check.max_context_file_bytes,
        },
        "extensions": {
            "entry_points": ep_names,
            "config": list(config.extensions),
        },
    }


def _fmt_route(route: dict[str, Any]) -> str:
    model = route.get("model") or "default"
    tag = f"{route['provider']}:{model}({route['family']}"
    if route.get("billing") == "paid":
        tag += ",paid"
    return tag + ")"


def _render_doctor(report: dict[str, Any]) -> str:
    lines = [
        f"coldtake {report['version']} (API v{report['api_version']})",
        f"config:    {report['config_path'] or '(packaged defaults)'}",
        f"state_dir: {report['state_dir']} ({'exists' if report['state_dir_exists'] else 'new'})",
        f"lead:      {report['lead_family']}"
        + (f"  namespace: {report['namespace']}" if report["namespace"] else ""),
        "",
        "Providers:",
    ]
    for name, info in report["providers"].items():
        status = "available" if info["available"] else "unavailable"
        detail = info.get("executable")
        if detail is None and "api_key_set" in info:
            detail = "OPENROUTER_API_KEY set" if info["api_key_set"] else "OPENROUTER_API_KEY unset"
        lines.append(f"  - {name:12s} {status:11s} {detail or ''}".rstrip())
    lines.append("")
    lines.append("Effective ladders (by lead family):")
    for lead, modes in report["ladders"].items():
        alt = modes["alternate"]
        chk = modes["check"]
        pnl = modes["panel"]
        alt_str = (
            alt["error"]
            if isinstance(alt, dict)
            else (", ".join(_fmt_route(r) for r in alt) or "(none)")
        )
        chk_str = (
            chk["error"]
            if isinstance(chk, dict)
            else (", ".join(_fmt_route(r) for r in chk) or "(none)")
        )
        pnl_str = (
            pnl["error"]
            if isinstance(pnl, dict)
            else (" | ".join(" -> ".join(_fmt_route(r) for r in slot) for slot in pnl) or "(none)")
        )
        lines.append(f"  lead={lead}:")
        lines.append(f"    alternate: {alt_str}")
        lines.append(f"    panel:     {pnl_str}")
        lines.append(f"    check:     {chk_str}")
    b = report["budget"]
    lim = report["limits"]
    lines.extend(
        [
            "",
            f"Budget:  ${b['per_request_usd']:.2f}/request, ${b['daily_usd']:.2f}/day",
            (
                f"Limits:  max_concurrent_jobs={lim['max_concurrent_jobs']}, "
                f"attempt={lim['attempt_seconds']}s, job={lim['job_seconds']}s, "
                f"judge={lim['judge_seconds']}s"
            ),
            f"Check:   context_roots={report['check']['context_roots'] or '(disabled)'}",
        ]
    )
    exts = [*report["extensions"]["entry_points"], *report["extensions"]["config"]]
    if exts:
        lines.append(f"Extensions: {', '.join(exts)}")
    return "\n".join(lines)


def _run_doctor(config: Config, *, config_arg: str | None, as_json: bool) -> int:
    report = _collect_doctor(config, config_arg)
    print(json.dumps(report, indent=2, sort_keys=True) if as_json else _render_doctor(report))
    return 0


class _DemoProvider:
    """In-process deterministic provider used only by `coldtake try`."""

    def __init__(self, name: str, handler: Callable[[str, dict[str, Any], Any], dict[str, Any]]):
        self.name = name
        self._handler = handler

    def available(self) -> bool:
        return True

    def complete(self, prompt: str, schema: dict[str, Any], route: Any, timeout: float):
        from .providers import Completion

        del timeout
        return Completion(data=self._handler(prompt, schema, route))


def _demo_memo(recommendation: str, *, choice: str | None = None) -> dict[str, Any]:
    base: dict[str, Any] = {
        "framing": "Balance predictability, operational simplicity, and tail latency.",
        "recommendation": recommendation,
        "best_alternative": "Start with a bounded LRU cache and revisit once hit-rate data exists.",
        "evidence": ["Access patterns in local CLI indexes are skew-heavy."],
        "assumptions": ["Working set fits within 256 MB of resident memory."],
        "value_tradeoffs": ["Memory ceiling versus cold-start latency."],
        "forecasts": ["Hit rate stays above 85% under steady usage."],
        "feasibility": ["Single-file implementation with no external dependency."],
        "missing_information": ["Exact distribution of infrequent batch scans."],
        "failure_modes": ["Sequential scans can evict hot keys under plain LRU."],
        "reversal_evidence": ["Measured scan pollution exceeding 20% of lookups."],
        "confidence": 0.78,
    }
    if choice is not None:
        base["choice"] = choice
        base["choice_reason"] = f"Selected '{choice}' for clear schema and comment support."
    return base


def _run_try(*, as_json: bool) -> int:
    from .check import CONTRACT_CHECK
    from .engine import Engine

    canary = "SEALED-CANARY-LOCAL-DEMO"
    seen_canary_in_advisor = False

    def handle_openai(prompt: str, schema: dict[str, Any], route: Any) -> dict[str, Any]:
        nonlocal seen_canary_in_advisor
        del route
        if "COLDTAKE_ROLE: judge" in prompt:
            return {
                "positions": [
                    {"label": "A", "stance": "Use S3-FIFO to resist scan pollution."},
                    {"label": "S", "stance": "Use a plain LRU cache for simplicity."},
                ],
                "clusters": [["A"]],
                "pairwise": [
                    {
                        "a": "A",
                        "b": "S",
                        "relation": "partial",
                        "crux": (
                            "Both keep an in-process bounded cache, but differ on scan resistance."
                        ),
                    }
                ],
                "sealed": {
                    "relation": "partial",
                    "crux": (
                        "Both keep an in-process bounded cache, but differ on scan resistance."
                    ),
                },
                "contradictions": [],
                "material": False,
            }
        if canary in prompt:
            seen_canary_in_advisor = True
        if "choice" in schema.get("properties", {}):
            return _demo_memo("Use TOML for human-edited local config.", choice="toml")
        return _demo_memo("Use S3-FIFO instead of plain LRU so batch scans do not evict hot keys.")

    def handle_google(prompt: str, schema: dict[str, Any], route: Any) -> dict[str, Any]:
        nonlocal seen_canary_in_advisor
        del route
        if "verdict" in schema.get("properties", {}):
            return {
                "verdict": "concerns",
                "key_risks": [
                    "Per-process token buckets drift across multiple worker processes.",
                    "Burst limit is not separated from sustained throughput.",
                ],
                "recommendation": (
                    "Use a shared atomic store or explicitly document single-process scope."
                ),
                "alternatives": ["Leaky bucket in SQLite with a single writer lock."],
            }
        if canary in prompt:
            seen_canary_in_advisor = True
        if "choice" in schema.get("properties", {}):
            return _demo_memo("TOML avoids YAML type-coercion surprises.", choice="toml")
        return _demo_memo("Use S3-FIFO with a small probation queue.")

    with tempfile.TemporaryDirectory(prefix="coldtake-try-") as tmp:
        base = load_config()
        cfg = base.model_copy(
            update={
                "lead_family": "anthropic",
                "state_dir": tmp,
                "routing": base.routing.model_copy(update={"resolver": None}),
                "subscription": {
                    "openai": SubscriptionRoute(provider="demo_openai", model="demo-gpt"),
                    "google": SubscriptionRoute(provider="demo_google", model="demo-gemini"),
                    "anthropic": SubscriptionRoute(provider="demo_anthropic", model="demo-claude"),
                },
                "openrouter": base.openrouter.model_copy(update={"models": ()}),
            }
        )
        providers = {
            "demo_openai": _DemoProvider("demo_openai", handle_openai),
            "demo_google": _DemoProvider("demo_google", handle_google),
            "demo_anthropic": _DemoProvider("demo_anthropic", handle_openai),
        }
        engine = Engine(cfg, providers=providers)
        try:
            alt_ack = engine.submit(
                "demo-alternate",
                {
                    "mode": "alternate",
                    "question": "Which eviction policy should our local CLI index cache use?",
                    "goals": ["Keep lookup latency predictable", "Stay dependency-free"],
                    "constraints": ["256 MB RAM ceiling"],
                    "sealed_answer": f"Use a plain LRU cache for simplicity. ({canary})",
                },
                lead_family="anthropic",
            )
            alt_res = engine.wait("demo-alternate", 10)

            engine.submit(
                "demo-panel",
                {
                    "mode": "panel",
                    "question": "Which configuration file format should the tool adopt?",
                    "options": [
                        {"id": "yaml", "label": "YAML"},
                        {"id": "toml", "label": "TOML"},
                        {"id": "json", "label": "JSON"},
                    ],
                    "sealed_choice": "yaml",
                },
                lead_family="anthropic",
            )
            panel_res = engine.wait("demo-panel", 10)

            engine.submit(
                "demo-check",
                {
                    "question": "Is an in-memory token bucket sufficient for our local daemon?",
                    "spec_text": (
                        "Each worker keeps an in-memory token bucket capped at 60 req/min."
                    ),
                },
                lead_family="anthropic",
                contract=CONTRACT_CHECK,
            )
            check_res = engine.wait("demo-check", 10)
        finally:
            engine.close()

    payload = {
        "offline_demo": True,
        "sealed_canary_leaked": seen_canary_in_advisor,
        "alternate": {
            "sealed_sha256": alt_ack["sealed_sha256"],
            "advisor_family": alt_res["result"]["memos"][0]["family"],
            "recommendation": alt_res["result"]["memos"][0]["memo"]["recommendation"],
            "comparison": alt_res["result"]["comparison"],
        },
        "panel": {
            "advisor_count": panel_res["result"]["advisor_count"],
            "choices": {m["family"]: m["memo"]["choice"] for m in panel_res["result"]["memos"]},
            "comparison": panel_res["result"]["comparison"],
        },
        "check": {
            "verdict": check_res["result"]["verdict"],
            "family_used": check_res["result"]["family_used"],
            "key_risks": check_res["result"]["key_risks"],
            "recommendation": check_res["result"]["recommendation"],
        },
    }
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    alt = payload["alternate"]
    pnl = payload["panel"]
    chk = payload["check"]
    print(
        "\n".join(
            [
                f"coldtake {__version__} offline demo (zero network, zero API key)",
                "",
                "1) Blind alternate with sealed answer:",
                f"   sealed_sha256:   {alt['sealed_sha256'][:16]}... "
                f"(canary leaked to advisor: {payload['sealed_canary_leaked']})",
                f"   advisor ({alt['advisor_family']}): {alt['recommendation']}",
                f"   sealed relation: {alt['comparison']['sealed']['relation']} — "
                f"{alt['comparison']['sealed']['crux']}",
                "",
                "2) Deterministic options panel:",
                f"   advisors ({pnl['advisor_count']}): {pnl['choices']}",
                f"   sealed choice:   yaml -> relation={pnl['comparison']['sealed']['relation']} "
                f"(material={pnl['comparison']['material']})",
                "",
                "3) One-turn spec check (coldtake_check):",
                f"   verdict ({chk['family_used']}): {chk['verdict']}",
                f"   top risk:        {chk['key_risks'][0]}",
                f"   recommendation:  {chk['recommendation']}",
                "",
                (
                    "Next: run `coldtake doctor` to inspect your local CLIs, "
                    "or `coldtake install claude` to wire MCP."
                ),
            ]
        )
    )
    return 0


def _serve(args: argparse.Namespace, config: Config) -> int:
    from .server import build_server

    if args.transport == "http" and not _is_loopback(args.host):
        print("coldtake: --host must be 127.0.0.1, localhost or ::1", file=sys.stderr)
        return 2
    server = build_server(config, host=args.host, port=args.port)
    state = _StopState()
    restore = _stop_on_sigterm(state)
    stopped = False
    drained = True
    try:
        try:
            try:
                server.run(transport="streamable-http" if args.transport == "http" else "stdio")
            finally:
                state.serving = False
        except _Stopped:
            stopped = True
    finally:
        try:
            drained = server.coldtake_engine.close(config.limits.drain_seconds)  # type: ignore[attr-defined]
        finally:
            restore()
    code = 0 if drained else 1
    if stopped and args.transport == "stdio":
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
    return code


class _Stopped(SystemExit):
    """Raised in the main thread by SIGTERM: a stop request, not a failure."""


class _StopState:
    serving = True
    requested = False


def _stop_on_sigterm(state: _StopState) -> Callable[[], None]:
    if threading.current_thread() is not threading.main_thread():
        return lambda: None

    def stop(signum: int, frame: Any) -> None:
        if state.requested:
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
            return
        state.requested = True
        if state.serving:
            raise _Stopped(0)

    previous = signal.signal(signal.SIGTERM, stop)

    def restore() -> None:
        signal.signal(signal.SIGTERM, previous if previous is not None else signal.SIG_DFL)

    return restore


if __name__ == "__main__":
    raise SystemExit(main())
