"""`coldtake stats`, `coldtake history`, and `coldtake show`: local run summaries and inspection."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .store import Store

DAY = 86400.0


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(pct / 100 * len(ordered)) - 1)]


def _rates(counts: dict[str, int]) -> dict[str, Any]:
    total = sum(counts.values())
    return {
        "n": total,
        **{k: counts.get(k, 0) for k in ("agree", "partial", "conflict")},
        **{
            f"{k}_rate": round(counts.get(k, 0) / total, 3) if total else None
            for k in ("agree", "partial", "conflict")
        },
    }


def _load_json_field(raw: str | None, path: str | None = None) -> dict[str, Any] | None:
    if raw:
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except ValueError:
            return None
    if path:
        p = Path(path)
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else None
            except (OSError, ValueError):
                return None
    return None


def collect(store: Store, days: float | None = None) -> dict[str, Any]:
    since = store.clock() - days * DAY if days else 0.0
    with store.connect() as db:
        runs = db.execute(
            "SELECT request_id, contract, mode, status, lead_family, created_at, finished_at,"
            " memos_json, result_json, result_path FROM runs WHERE created_at >= ?",
            (since,),
        ).fetchall()
        ids = {r["request_id"] for r in runs}
        lead_by_id = {r["request_id"]: (r["lead_family"] or "other") for r in runs}
        # Attempts and spend are windowed by time, so extension calls (no job) count too.
        attempts = db.execute("SELECT * FROM attempts WHERE started_at >= ?", (since,)).fetchall()
        relations = [
            r
            for r in db.execute("SELECT * FROM sealed_relations").fetchall()
            if r["request_id"] in ids
        ]
        ledger = db.execute("SELECT * FROM ledger WHERE authorized_at >= ?", (since,)).fetchall()

    by_mode: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    latency: dict[str, list[float]] = defaultdict(list)
    same_family_memos = 0
    check_verdicts: dict[str, int] = defaultdict(int)
    check_fallbacks: dict[str, int] = defaultdict(int)
    check_total = 0

    for run in runs:
        by_mode[run["mode"]][run["status"]] += 1
        if run["status"] == "complete" and run["finished_at"]:
            latency[run["mode"]].append(run["finished_at"] - run["created_at"])
        if run["status"] == "complete":
            try:
                memos = json.loads(run["memos_json"] or "[]")
            except ValueError:
                memos = []
            for m in memos:
                if isinstance(m, dict) and m.get("same_family"):
                    same_family_memos += 1
            if run["mode"] == "check":
                res = _load_json_field(run["result_json"], run["result_path"])
                if res:
                    check_total += 1
                    verdict = str(res.get("verdict") or "unknown")
                    check_verdicts[verdict] += 1
                    fb = res.get("fallback_reason") or "none"
                    check_fallbacks[str(fb)] += 1

    routes: dict[str, dict[str, Any]] = {}
    fallbacks = 0
    for a in attempts:
        key = f"{a['provider']}:{a['model'] or 'default'}"
        entry = routes.setdefault(
            key, {"billing": a["billing"], "family": a["family"], "attempts": 0, "outcomes": {}}
        )
        entry["attempts"] += 1
        entry["outcomes"][a["outcome"]] = entry["outcomes"].get(a["outcome"], 0) + 1
        fallbacks += a["fallback"]
    for entry in routes.values():
        ok = entry["outcomes"].get("ok", 0)
        entry["success_rate"] = round(ok / entry["attempts"], 3)

    spend: dict[str, float] = defaultdict(float)
    held = {"attempts": 0, "usd": 0.0}
    paid_run_ids: set[str] = set()
    for row in ledger:
        if row["status"] == "committed":
            paid_run_ids.add(row["request_id"])
            spend[row["day"]] += (
                row["actual_cost"] if row["actual_cost"] is not None else row["amount"]
            )
        elif row["status"] == "charge_unknown":
            paid_run_ids.add(row["request_id"])
            held["attempts"] += 1
            held["usd"] += row["amount"]

    total_runs = len(runs)
    paid_runs = sum(1 for r in runs if r["request_id"] in paid_run_ids)
    sub_runs = total_runs - paid_runs
    total_committed_usd = round(sum(spend.values()), 6)

    sealed: dict[str, dict[str, dict[str, Any]]] = {
        "by_mode": {},
        "by_family": {},
        "by_pair": {},
    }
    for scope, field in (("by_mode", "mode"), ("by_family", "family")):
        grouped: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for r in relations:
            grouped[r[field]][r["relation"]] += 1
        sealed[scope] = {k: _rates(dict(v)) for k, v in grouped.items()}

    pair_grouped: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in relations:
        lead = lead_by_id.get(r["request_id"], "other")
        pair_grouped[f"{lead}->{r['family']}"][r["relation"]] += 1
    sealed["by_pair"] = {k: _rates(dict(v)) for k, v in pair_grouped.items()}

    return {
        "window_days": days,
        "runs": {mode: dict(counts) for mode, counts in by_mode.items()},
        "routes": routes,
        "fallback_attempts": fallbacks,
        "same_family_memos": same_family_memos,
        "billing": {
            "total_runs": total_runs,
            "subscription_runs": sub_runs,
            "paid_runs": paid_runs,
            "subscription_share": round(sub_runs / total_runs, 3) if total_runs else None,
            "total_committed_usd": total_committed_usd,
        },
        "checks": {
            "total": check_total,
            "verdicts": {k: check_verdicts.get(k, 0) for k in ("sound", "concerns", "unsound")},
            "fallback_reasons": dict(sorted(check_fallbacks.items())),
        },
        "paid": {
            "committed_usd_by_day": {d: round(v, 6) for d, v in sorted(spend.items())},
            "charge_unknown_held": {"attempts": held["attempts"], "usd": round(held["usd"], 6)},
        },
        "latency_seconds": {
            mode: {p: _percentile(v, n) for p, n in (("p50", 50), ("p90", 90), ("p95", 95))}
            for mode, v in latency.items()
        },
        "sealed": sealed,
    }


def _fmt(value: Any) -> str:
    return "-" if value is None else f"{value:.1f}" if isinstance(value, float) else str(value)


def _pct(rate: float | None) -> str:
    return "-" if rate is None else f"{round(rate * 100)}%"


def render(data: dict[str, Any]) -> str:
    window = f"last {data['window_days']:g} days" if data["window_days"] else "all time"
    lines = [f"coldtake stats ({window})", "", "Runs by mode"]
    if not data["runs"]:
        lines.append("  (none)")
    for mode, counts in sorted(data["runs"].items()):
        lines.append(f"  {mode:<10}" + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    billing = data.get("billing") or {}
    if billing.get("total_runs"):
        lines += [
            "",
            "Billing mix",
            f"  subscription={billing['subscription_runs']} ({_pct(billing['subscription_share'])})"
            f"  paid={billing['paid_runs']}"
            f"  committed=${billing['total_committed_usd']:.4f}"
            f"  same_family_memos={data.get('same_family_memos', 0)}",
        ]

    checks = data.get("checks") or {}
    if checks.get("total"):
        v = checks["verdicts"]
        fb = ", ".join(f"{k}={val}" for k, val in checks.get("fallback_reasons", {}).items())
        lines += [
            "",
            "Spec checks (coldtake_check)",
            f"  n={checks['total']}  sound={v['sound']}  concerns={v['concerns']}"
            f"  unsound={v['unsound']}" + (f"  ({fb})" if fb else ""),
        ]

    lines += ["", "Routes (attempts, success rate)"]
    for key, e in sorted(data["routes"].items()):
        outcomes = ", ".join(f"{k}={v}" for k, v in sorted(e["outcomes"].items()))
        lines.append(
            f"  {key:<34} {e['billing']:<12} n={e['attempts']:<4} "
            f"ok={e['success_rate']:<5} {outcomes}"
        )
    lines.append(f"  fallback attempts: {data['fallback_attempts']}")
    paid = data["paid"]
    lines += ["", "Paid spend (USD, committed)"]
    for day, usd in paid["committed_usd_by_day"].items():
        lines.append(f"  {day}  {usd:.4f}")
    if not paid["committed_usd_by_day"]:
        lines.append("  (none)")
    held = paid["charge_unknown_held"]
    lines.append(
        f"  charge_unknown (allowance held): {held['attempts']} attempts, {held['usd']:.4f}"
    )
    lines += ["", "Latency of complete runs (seconds)"]
    for mode, p in sorted(data["latency_seconds"].items()):
        lines.append(
            f"  {mode:<10} p50={_fmt(p['p50'])}  p90={_fmt(p['p90'])}  p95={_fmt(p['p95'])}"
        )
    lines += ["", "Sealed comparison (agree / partial / conflict)"]
    for scope, title in (
        ("by_mode", "mode"),
        ("by_family", "advisor family"),
        ("by_pair", "lead->advisor"),
    ):
        for name, r in sorted(data["sealed"].get(scope, {}).items()):
            lines.append(
                f"  {title:<15} {name:<20} n={r['n']:<3} "
                f"{r['agree']} / {r['partial']} / {r['conflict']}"
                f"  (conflict {_pct(r['conflict_rate'])})"
            )
    if not any(data["sealed"].values()):
        lines.append("  (none)")
    return "\n".join(lines)


def render_share(data: dict[str, Any]) -> str:
    """Anonymized, copy-pasteable scorecard with zero prompt text or private details."""
    window = f"last {data['window_days']:g} days" if data["window_days"] else "all time"
    total_complete = sum(counts.get("complete", 0) for counts in data["runs"].values())
    mode_bits = ", ".join(
        f"{m}={counts.get('complete', 0)}"
        for m, counts in sorted(data["runs"].items())
        if counts.get("complete", 0)
    )
    lines = [
        f"coldtake scorecard ({window})",
        f"- Runs: {total_complete} complete" + (f" ({mode_bits})" if mode_bits else ""),
    ]

    by_mode = data["sealed"].get("by_mode", {})
    s_agree = sum(v["agree"] for v in by_mode.values())
    s_partial = sum(v["partial"] for v in by_mode.values())
    s_conflict = sum(v["conflict"] for v in by_mode.values())
    s_total = s_agree + s_partial + s_conflict
    if s_total:
        lines.append(
            f"- Blind sealed comparisons: {s_total} "
            f"(agree {_pct(s_agree / s_total)} · partial {_pct(s_partial / s_total)} · "
            f"conflict {_pct(s_conflict / s_total)})"
        )
        for pair, stats in sorted(data["sealed"].get("by_pair", {}).items()):
            lines.append(
                f"  - {pair}: n={stats['n']} "
                f"(conflict {_pct(stats['conflict_rate'])}, "
                f"partial {_pct(stats['partial_rate'])}, agree {_pct(stats['agree_rate'])})"
            )

    checks = data.get("checks") or {}
    if checks.get("total"):
        v = checks["verdicts"]
        lines.append(
            f"- Spec checks: {checks['total']} "
            f"(sound={v['sound']}, concerns={v['concerns']}, unsound={v['unsound']})"
        )

    billing = data.get("billing") or {}
    if billing.get("total_runs"):
        lines.append(
            f"- Cost: {_pct(billing['subscription_share'])} on existing CLI subscriptions ($0) "
            f"· ${billing['total_committed_usd']:.2f} paid fallback"
        )

    lat_bits = [
        f"{mode} {_fmt(p['p50'])}s"
        for mode, p in sorted(data["latency_seconds"].items())
        if p.get("p50") is not None
    ]
    if lat_bits:
        lines.append(f"- Median latency: {' · '.join(lat_bits)}")
    return "\n".join(lines)


# -- history & show ----------------------------------------------------------


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def list_runs(
    store: Store,
    *,
    limit: int = 20,
    mode: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    with store.connect() as db:
        rows = db.execute(
            "SELECT request_id, contract, mode, status, lead_family, reason, created_at,"
            " finished_at, memos_json, comparison_json, result_json, result_path"
            " FROM runs WHERE (? IS NULL OR mode = ?) AND (? IS NULL OR status = ?)"
            " ORDER BY created_at DESC LIMIT ?",
            (mode, mode, status, status, max(1, limit)),
        ).fetchall()
        ledger_rows = db.execute(
            "SELECT request_id, status, amount, actual_cost FROM ledger"
        ).fetchall()

    cost_by_id: dict[str, float] = defaultdict(float)
    paid_ids: set[str] = set()
    for lr in ledger_rows:
        if lr["status"] in {"committed", "charge_unknown"}:
            paid_ids.add(lr["request_id"])
            cost_by_id[lr["request_id"]] += (
                lr["actual_cost"] if lr["actual_cost"] is not None else lr["amount"]
            )

    items: list[dict[str, Any]] = []
    for row in rows:
        rid = row["request_id"]
        elapsed = (
            round(row["finished_at"] - row["created_at"], 2)
            if row["finished_at"] is not None
            else None
        )
        memos = json.loads(row["memos_json"] or "[]")
        advisors = [
            f"{m.get('family')}:{m.get('model') or m.get('provider')}"
            for m in memos
            if isinstance(m, dict)
        ]
        headline = _run_headline(row)
        items.append(
            {
                "request_id": rid,
                "mode": row["mode"],
                "contract": row["contract"],
                "status": row["status"],
                "lead_family": row["lead_family"],
                "created_at": _iso(row["created_at"]),
                "elapsed_seconds": elapsed,
                "billing": "paid" if rid in paid_ids else "subscription",
                "paid_usd": round(cost_by_id.get(rid, 0.0), 6),
                "advisors": advisors,
                "headline": headline,
            }
        )
    return items


def _run_headline(row: Any) -> str:
    if row["status"] != "complete":
        return str(row["reason"] or row["status"])
    if row["mode"] == "check":
        res = _load_json_field(row["result_json"], row["result_path"])
        if res:
            fb = f" ({res['fallback_reason']})" if res.get("fallback_reason") else ""
            return f"verdict={res.get('verdict')}{fb}"
    comp = _load_json_field(row["comparison_json"])
    if comp:
        sealed = comp.get("sealed")
        if isinstance(sealed, dict) and sealed.get("relation"):
            return f"sealed={sealed['relation']}"
        if comp.get("status") == "unavailable":
            return f"comparison=unavailable({comp.get('cause')})"
        if "material" in comp:
            return f"material={comp['material']}"
    return "complete"


def render_history(items: list[dict[str, Any]]) -> str:
    if not items:
        return "coldtake history: (no runs)"
    lines = ["TIMESTAMP             MODE       STATUS     COST    HEADLINE / REQUEST_ID"]
    for item in items:
        cost = f"${item['paid_usd']:.3f}" if item["billing"] == "paid" else "$0"
        lines.append(
            f"{item['created_at'] or '-':<21} {item['mode']:<10} {item['status']:<10} "
            f"{cost:<7} {item['headline']}  [{item['request_id']}]"
        )
    return "\n".join(lines)


def get_run_detail(store: Store, request_id: str) -> dict[str, Any] | None:
    with store.connect() as db:
        row = db.execute("SELECT * FROM runs WHERE request_id = ?", (request_id,)).fetchone()
        if row is None:
            matches = db.execute(
                "SELECT * FROM runs WHERE request_id LIKE ?", (f"%:{request_id}",)
            ).fetchall()
            if len(matches) == 1:
                row = matches[0]
            elif len(matches) > 1:
                names = ", ".join(m["request_id"] for m in matches)
                raise ValueError(f"ambiguous request_id {request_id!r}; matches: {names}")
        if row is None:
            return None
        rid = row["request_id"]
        attempts = [
            dict(a)
            for a in db.execute(
                "SELECT role, slot, attempt, provider, model, family, billing, outcome,"
                " reason, seconds, cost_usd, fallback FROM attempts WHERE request_id = ?"
                " ORDER BY id",
                (rid,),
            ).fetchall()
        ]
        ledger = [
            dict(lr)
            for lr in db.execute(
                "SELECT day, model, amount, status, actual_cost FROM ledger WHERE request_id = ?",
                (rid,),
            ).fetchall()
        ]

    packet = _load_json_field(row["packet_json"]) or {}
    question = packet.get("question") or (packet.get("packet") or {}).get("question") or ""
    memos = json.loads(row["memos_json"] or "[]")
    comparison = _load_json_field(row["comparison_json"])
    result = _load_json_field(row["result_json"], row["result_path"])
    paid_usd = sum(
        (lr["actual_cost"] if lr["actual_cost"] is not None else lr["amount"])
        for lr in ledger
        if lr["status"] in {"committed", "charge_unknown"}
    )
    elapsed = (
        round(row["finished_at"] - row["created_at"], 2) if row["finished_at"] is not None else None
    )
    return {
        "request_id": rid,
        "contract": row["contract"],
        "mode": row["mode"],
        "status": row["status"],
        "lead_family": row["lead_family"],
        "created_at": _iso(row["created_at"]),
        "finished_at": _iso(row["finished_at"]),
        "elapsed_seconds": elapsed,
        "sealed_sha256": row["sealed_sha256"],
        "reason": row["reason"],
        "cause_type": row["cause_type"],
        "cause_message": row["cause_message"],
        "question": question,
        "memos": memos,
        "comparison": comparison,
        "result": result,
        "attempts": attempts,
        "paid_usd": round(paid_usd, 6),
    }


def render_run_detail(detail: dict[str, Any]) -> str:
    lines = [
        f"request_id: {detail['request_id']}",
        f"mode:       {detail['mode']} ({detail['contract']})",
        f"status:     {detail['status']}"
        + (f" ({detail['reason']})" if detail.get("reason") else ""),
        f"lead:       {detail['lead_family']}  elapsed: {_fmt(detail['elapsed_seconds'])}s"
        f"  paid: ${detail['paid_usd']:.4f}",
    ]
    if detail.get("sealed_sha256"):
        lines.append(f"sealed:     {detail['sealed_sha256'][:16]}...")
    if detail.get("question"):
        q = detail["question"].replace("\n", " ").strip()
        lines.append(f"question:   {q[:200]}" + ("..." if len(q) > 200 else ""))

    if detail["mode"] == "check" and isinstance(detail.get("result"), dict):
        res = detail["result"]
        fb = res.get("fallback_reason") or "none"
        lines.extend(
            [
                "",
                f"Check verdict: {res.get('verdict')} ({res.get('model_used')}, "
                f"family={res.get('family_used')}, fallback={fb})",
                f"Recommendation: {res.get('recommendation')}",
            ]
        )
        for risk in res.get("key_risks") or []:
            lines.append(f"  - risk: {risk}")
        for alt in res.get("alternatives") or []:
            lines.append(f"  - alt:  {alt}")
    elif detail.get("memos"):
        lines.append("")
        lines.append("Advisor memos:")
        for m in detail["memos"]:
            memo = m.get("memo") or {}
            sf = " [same_family]" if m.get("same_family") else ""
            ch = f" choice={memo['choice']}" if memo.get("choice") else ""
            lines.append(
                f"  - {m.get('family')}/{m.get('model') or m.get('provider')}{sf}{ch} "
                f"(conf={memo.get('confidence')}):"
            )
            lines.append(f"    {memo.get('recommendation', '')}")

    comp = detail.get("comparison")
    if isinstance(comp, dict):
        lines.append("")
        lines.append("Comparison:")
        if comp.get("status") == "unavailable":
            lines.append(f"  unavailable ({comp.get('cause')})")
        else:
            sealed = comp.get("sealed")
            if isinstance(sealed, dict):
                lines.append(f"  sealed: {sealed.get('relation')} — {sealed.get('crux', '')}")
            lines.append(f"  material_disagreement: {comp.get('material')}")

    if detail.get("cause_message"):
        lines.extend(["", f"Cause: {detail.get('cause_type')}: {detail['cause_message']}"])
    return "\n".join(lines)
