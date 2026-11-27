"""Self-contained interactive HTML scorecard generator (`coldtake report`)."""
# ruff: noqa: E501

from __future__ import annotations

import json
from typing import Any

from .stats import collect, get_run_detail, list_runs, render_share
from .store import Store


def build_report_payload(
    store: Store, *, days: float | None = None, limit: int = 50
) -> dict[str, Any]:
    stats = collect(store, days)
    runs = list_runs(store, limit=limit)
    details: dict[str, dict[str, Any]] = {}
    for item in runs:
        det = get_run_detail(store, item["request_id"])
        if det is None:
            continue
        # Keep payload compact for interactive browser inspection.
        details[item["request_id"]] = {
            "request_id": det["request_id"],
            "mode": det["mode"],
            "status": det["status"],
            "lead_family": det["lead_family"],
            "elapsed_seconds": det["elapsed_seconds"],
            "paid_usd": det["paid_usd"],
            "sealed_sha256": det["sealed_sha256"],
            "question": (det["question"] or "")[:300],
            "comparison": det["comparison"],
            "check_verdict": (det["result"] or {}).get("verdict")
            if det["mode"] == "check" and isinstance(det["result"], dict)
            else None,
            "check_risks": ((det["result"] or {}).get("key_risks") or [])[:5]
            if det["mode"] == "check" and isinstance(det["result"], dict)
            else [],
            "check_recommendation": ((det["result"] or {}).get("recommendation") or "")[:400]
            if det["mode"] == "check" and isinstance(det["result"], dict)
            else None,
            "memos": [
                {
                    "family": m.get("family"),
                    "model": m.get("model") or m.get("provider"),
                    "same_family": bool(m.get("same_family")),
                    "choice": (m.get("memo") or {}).get("choice"),
                    "confidence": (m.get("memo") or {}).get("confidence"),
                    "recommendation": str((m.get("memo") or {}).get("recommendation") or "")[:400],
                }
                for m in det.get("memos") or []
            ],
            "reason": det["reason"],
            "cause_message": det["cause_message"],
        }
    return {
        "schema": "coldtake-report/1",
        "stats": stats,
        "share_text": render_share(stats),
        "runs": runs,
        "details": details,
    }


def _safe_json_for_script(data: Any) -> str:
    return (
        json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def render_html(payload: dict[str, Any]) -> str:
    """Render a zero-dependency, self-contained HTML scorecard."""
    embedded = _safe_json_for_script(payload)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>coldtake scorecard</title>
<style>
  :root {{
    --bg: #0c0f14; --card: #141923; --card-2: #1a2130; --border: #263144;
    --text: #e8eef7; --muted: #8b9bb4; --accent: #60a5fa;
    --green: #34d399; --amber: #fbbf24; --red: #f87171;
  }}
  @media (prefers-color-scheme: light) {{
    :root {{
      --bg: #f6f8fb; --card: #ffffff; --card-2: #f1f5f9; --border: #dbe3ee;
      --text: #0f172a; --muted: #64748b; --accent: #2563eb;
      --green: #059669; --amber: #d97706; --red: #dc2626;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 28px 20px 64px; background: var(--bg); color: var(--text);
    font: 14px/1.55 ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
  }}
  .wrap {{ max-width: 1080px; margin: 0 auto; }}
  header {{ display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between;
            gap: 12px; margin-bottom: 20px; }}
  h1 {{ margin: 0; font-size: 22px; letter-spacing: -0.02em; }}
  .sub {{ color: var(--muted); font-size: 13px; }}
  .btn {{
    background: var(--card-2); color: var(--text); border: 1px solid var(--border);
    border-radius: 8px; padding: 7px 12px; font-size: 13px; cursor: pointer;
  }}
  .btn:hover {{ border-color: var(--accent); }}
  .kpis {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
           gap: 12px; margin-bottom: 20px; }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px;
           padding: 16px; }}
  .kpi-label {{ color: var(--muted); font-size: 12px; text-transform: uppercase;
                letter-spacing: 0.04em; }}
  .kpi-val {{ font-size: 26px; font-weight: 700; margin-top: 4px; }}
  .kpi-sub {{ color: var(--muted); font-size: 12px; margin-top: 2px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
           gap: 16px; margin-bottom: 20px; }}
  h2 {{ margin: 0 0 12px; font-size: 15px; font-weight: 600; }}
  .bar {{ display: flex; height: 10px; border-radius: 999px; overflow: hidden;
          background: var(--card-2); margin: 6px 0; }}
  .seg-agree {{ background: var(--green); }}
  .seg-partial {{ background: var(--amber); }}
  .seg-conflict {{ background: var(--red); }}
  .pair-row {{ margin-bottom: 12px; }}
  .pair-head {{ display: flex; justify-content: space-between; font-size: 13px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); }}
  th {{ color: var(--muted); font-weight: 500; font-size: 12px; }}
  tr.clickable {{ cursor: pointer; }}
  tr.clickable:hover {{ background: var(--card-2); }}
  .chip {{ display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px;
           font-weight: 600; border: 1px solid var(--border); }}
  .c-ok {{ color: var(--green); border-color: var(--green); }}
  .c-warn {{ color: var(--amber); border-color: var(--amber); }}
  .c-bad {{ color: var(--red); border-color: var(--red); }}
  .filters {{ display: flex; gap: 6px; margin-bottom: 12px; flex-wrap: wrap; }}
  .filters button.active {{ border-color: var(--accent); color: var(--accent); }}
  .drawer {{ margin-top: 14px; background: var(--card-2); border: 1px solid var(--border);
             border-radius: 10px; padding: 14px; display: none; }}
  .drawer.open {{ display: block; }}
  pre {{ margin: 8px 0 0; white-space: pre-wrap; font-family: ui-monospace, SFMono-Regular,
         Menlo, monospace; font-size: 12px; color: var(--muted); }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>coldtake scorecard</h1>
      <div class="sub" id="subtitle"></div>
    </div>
    <div style="display:flex;gap:8px;align-items:center">
      <button class="btn" id="copy-share">Copy shareable scorecard</button>
    </div>
  </header>

  <section class="kpis" id="kpis"></section>

  <section class="grid">
    <div class="card">
      <h2>Lead &rarr; Advisor Disagreement</h2>
      <div id="pairs"></div>
    </div>
    <div class="card">
      <h2>Spec Checks &amp; Routes</h2>
      <div id="checks"></div>
      <div id="routes" style="margin-top:12px"></div>
    </div>
  </section>

  <section class="card">
    <h2>Recent Runs</h2>
    <div class="filters" id="filters"></div>
    <table>
      <thead>
        <tr><th>Time (UTC)</th><th>Mode</th><th>Status</th><th>Cost</th><th>Outcome</th><th>Request ID</th></tr>
      </thead>
      <tbody id="runs-body"></tbody>
    </table>
    <div class="drawer" id="drawer"></div>
  </section>
</div>
<script>
const DATA = {embedded};
const S = DATA.stats;
const el = (tag, attrs, text) => {{
  const n = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
}};
const pct = (v) => (v == null ? "-" : Math.round(v * 100) + "%");

document.getElementById("subtitle").textContent =
  (S.window_days ? `Last ${{S.window_days}} days` : "All time") +
  ` · ${{S.billing.total_runs}} recorded runs`;

document.getElementById("copy-share").addEventListener("click", async (ev) => {{
  try {{
    await navigator.clipboard.writeText(DATA.share_text);
    ev.target.textContent = "Copied!";
    setTimeout(() => (ev.target.textContent = "Copy shareable scorecard"), 1600);
  }} catch {{
    window.prompt("Copy scorecard:", DATA.share_text);
  }}
}});

// KPIs
const kpiRoot = document.getElementById("kpis");
const completeTotal = Object.values(S.runs).reduce((a, m) => a + (m.complete || 0), 0);
const sealedModes = Object.values(S.sealed.by_mode || {{}});
const sTot = sealedModes.reduce((a, m) => a + m.n, 0);
const sConf = sealedModes.reduce((a, m) => a + m.conflict, 0);
const altP50 = S.latency_seconds.alternate ? S.latency_seconds.alternate.p50 : null;
const kpis = [
  ["Complete Runs", completeTotal, Object.entries(S.runs).map(([k, v]) => `${{k}}=${{v.complete||0}}`).join(" · ") || "none"],
  ["Sealed Conflict Rate", sTot ? pct(sConf / sTot) : "-", sTot ? `${{sConf}}/${{sTot}} blind comparisons` : "no sealed comparisons yet"],
  ["Subscription Share", pct(S.billing.subscription_share), `$${{(S.billing.total_committed_usd || 0).toFixed(2)}} paid fallback`],
  ["Median Latency", altP50 != null ? `${{altP50.toFixed(1)}}s` : "-", "alternate p50"],
];
for (const [label, val, sub] of kpis) {{
  const c = el("div", {{class: "card"}});
  c.append(el("div", {{class: "kpi-label"}}, label), el("div", {{class: "kpi-val"}}, val), el("div", {{class: "kpi-sub"}}, sub));
  kpiRoot.append(c);
}}

// Pairs
const pairsRoot = document.getElementById("pairs");
const pairEntries = Object.entries((S.sealed && S.sealed.by_pair) || {{}});
if (!pairEntries.length) {{
  pairsRoot.append(el("div", {{class: "sub"}}, "No sealed comparisons recorded yet."));
}} else {{
  for (const [pair, st] of pairEntries) {{
    const row = el("div", {{class: "pair-row"}});
    const head = el("div", {{class: "pair-head"}});
    head.append(el("strong", null, pair), el("span", {{class: "sub"}}, `n=${{st.n}} · agree ${{pct(st.agree_rate)}} · partial ${{pct(st.partial_rate)}} · conflict ${{pct(st.conflict_rate)}}`));
    const bar = el("div", {{class: "bar"}});
    for (const [cls, r] of [["seg-agree", st.agree_rate], ["seg-partial", st.partial_rate], ["seg-conflict", st.conflict_rate]]) {{
      if (r > 0) bar.append(el("div", {{class: cls, style: `width:${{(r * 100).toFixed(1)}}%`}}));
    }}
    row.append(head, bar);
    pairsRoot.append(row);
  }}
}}

// Checks + Routes
const checksRoot = document.getElementById("checks");
if (S.checks && S.checks.total) {{
  const v = S.checks.verdicts;
  checksRoot.append(
    el("span", {{class: "chip c-ok"}}, `sound: ${{v.sound}}`), " ",
    el("span", {{class: "chip c-warn"}}, `concerns: ${{v.concerns}}`), " ",
    el("span", {{class: "chip c-bad"}}, `unsound: ${{v.unsound}}`)
  );
}} else {{
  checksRoot.append(el("div", {{class: "sub"}}, "No coldtake_check runs yet."));
}}
const routesRoot = document.getElementById("routes");
const rEntries = Object.entries(S.routes || {{}});
if (rEntries.length) {{
  const t = el("table");
  const th = el("tr");
  for (const h of ["Route", "Billing", "N", "OK"]) th.append(el("th", null, h));
  t.append(el("thead").appendChild(th).parentNode);
  const tb = el("tbody");
  for (const [key, r] of rEntries) {{
    const tr = el("tr");
    tr.append(el("td", null, key), el("td", null, r.billing), el("td", null, r.attempts), el("td", null, pct(r.success_rate)));
    tb.append(tr);
  }}
  t.append(tb);
  routesRoot.append(t);
}}

// Runs table + filter + drawer
let activeMode = "all";
const modes = ["all", "alternate", "review", "panel", "check"];
const filtersRoot = document.getElementById("filters");
const bodyRoot = document.getElementById("runs-body");
const drawer = document.getElementById("drawer");

function showDetail(rid) {{
  const d = DATA.details[rid];
  drawer.textContent = "";
  if (!d) {{ drawer.classList.remove("open"); return; }}
  drawer.classList.add("open");
  drawer.append(el("strong", null, `${{d.request_id}} · ${{d.mode}} · ${{d.status}}`));
  if (d.question) drawer.append(el("div", {{style: "margin-top:6px"}}, `Q: ${{d.question}}`));
  if (d.sealed_sha256) drawer.append(el("div", {{class: "sub"}}, `sealed_sha256: ${{d.sealed_sha256.slice(0, 16)}}...`));
  if (d.check_verdict) {{
    drawer.append(el("div", {{style: "margin-top:8px"}}, `Verdict: ${{d.check_verdict}} — ${{d.check_recommendation || ""}}`));
    for (const r of d.check_risks || []) drawer.append(el("div", {{class: "sub"}}, `• ${{r}}`));
  }}
  if (d.comparison && d.comparison.sealed) {{
    drawer.append(el("div", {{style: "margin-top:8px"}}, `Sealed relation: ${{d.comparison.sealed.relation}} — ${{d.comparison.sealed.crux || ""}}`));
  }}
  for (const m of d.memos || []) {{
    drawer.append(el("pre", null, `[${{m.family}}/${{m.model}}${{m.same_family ? " same_family" : ""}}] ${{m.recommendation}}`));
  }}
  if (d.cause_message) drawer.append(el("pre", null, `Error: ${{d.cause_message}}`));
}}

function renderRuns() {{
  bodyRoot.textContent = "";
  const list = DATA.runs.filter((r) => activeMode === "all" || r.mode === activeMode);
  if (!list.length) {{
    const tr = el("tr");
    const td = el("td", {{colspan: "6", class: "sub"}}, "No runs match this filter.");
    tr.append(td);
    bodyRoot.append(tr);
    return;
  }}
  for (const r of list) {{
    const tr = el("tr", {{class: "clickable"}});
    tr.addEventListener("click", () => showDetail(r.request_id));
    const cost = r.billing === "paid" ? `$${{r.paid_usd.toFixed(3)}}` : "$0";
    tr.append(
      el("td", null, r.created_at || "-"),
      el("td", null, r.mode),
      el("td", null, r.status),
      el("td", null, cost),
      el("td", null, r.headline),
      el("td", null, r.request_id),
    );
    bodyRoot.append(tr);
  }}
}}

for (const m of modes) {{
  const b = el("button", {{class: "btn" + (m === activeMode ? " active" : "")}}, m);
  b.addEventListener("click", () => {{
    activeMode = m;
    for (const child of filtersRoot.children) child.classList.toggle("active", child.textContent === m);
    renderRuns();
  }});
  filtersRoot.append(b);
}}
renderRuns();
</script>
</body>
</html>
"""


__all__ = ["build_report_payload", "render_html"]
