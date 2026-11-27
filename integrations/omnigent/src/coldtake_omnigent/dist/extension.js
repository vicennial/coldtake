(() => {
  "use strict";

  const identity = globalThis.__OMNIGENT_EXTENSION__;
  if (!identity) return;

  let port = null;
  let seq = 0;
  const pending = new Map();

  function send(msg) {
    if (!port) return;
    port.postMessage({
      source: "omnigent-extension",
      extensionId: identity.extensionId,
      pageId: identity.pageId,
      view: identity.view,
      nonce: identity.nonce,
      apiVersion: identity.apiVersion,
      ...msg,
    });
  }

  function callHost(method, params) {
    return new Promise((resolve, reject) => {
      const requestId = `ct-${++seq}`;
      pending.set(requestId, { resolve, reject });
      send({ type: "request", requestId, method, params: params ?? {} });
    });
  }

  function el(tag, attrs, text) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    }
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function pct(v) {
    return v == null ? "-" : `${Math.round(v * 100)}%`;
  }

  function applyTheme(val) {
    const mode = typeof val === "string" ? val : (val && (val.resolved || val.theme || val.mode));
    if (mode === "light" || mode === "dark") {
      document.documentElement.setAttribute("data-theme", mode);
    }
  }

  function renderScorecard(root, state) {
    root.textContent = "";
    const wrap = el("div", { class: "ct-wrap" });

    // Header
    const head = el("div", { class: "ct-head" });
    const left = el("div");
    left.append(
      el("h1", { class: "ct-title" }, "Coldtake Scorecard"),
      el(
        "div",
        { class: "ct-sub" },
        "Blind cross-family checks for single-agent sessions (conflict-free with polly & debby)"
      )
    );
    const actions = el("div", { class: "ct-actions" });
    const newBtn = el("button", { class: "ct-btn", type: "button" }, "New session");
    newBtn.addEventListener("click", () => callHost("navigation.openNewSession", {}).catch(() => {}));
    const importBtn = el("button", { class: "ct-btn", type: "button" }, "Import stats JSON");
    const refreshBtn = el("button", { class: "ct-btn", type: "button" }, "Refresh sessions");
    refreshBtn.addEventListener("click", () => loadAll(root, state));
    actions.append(newBtn, importBtn, refreshBtn);
    head.append(left, actions);
    wrap.append(head);

    // Import panel (paste output of `coldtake stats --json` under Extension API v1 connect-src 'none')
    const importBox = el("div", { class: "ct-card ct-import" });
    importBox.append(
      el("div", { class: "ct-sub" }, "Paste output of `coldtake stats --json` (saved in Omnigent user storage):"),
    );
    const ta = el("textarea", { placeholder: '{"runs": {...}, "sealed": {...}, "billing": {...}}' });
    const errLine = el("div", { class: "ct-err" });
    const saveBtn = el("button", { class: "ct-btn", type: "button", style: "margin-top:8px" }, "Save snapshot");
    saveBtn.addEventListener("click", async () => {
      errLine.textContent = "";
      try {
        const parsed = JSON.parse(ta.value);
        if (!parsed || typeof parsed !== "object" || !parsed.runs) {
          throw new Error("Expected `coldtake stats --json` object with `runs`");
        }
        const snap = { savedAt: new Date().toISOString(), stats: parsed };
        await callHost("storage.user.set", { key: "stats_snapshot", value: snap });
        state.snapshot = snap;
        renderScorecard(root, state);
      } catch (e) {
        errLine.textContent = e instanceof Error ? e.message : "Invalid JSON";
      }
    });
    importBox.append(ta, el("div", null), saveBtn, errLine);
    importBtn.addEventListener("click", () => importBox.classList.toggle("open"));
    wrap.append(importBox);

    const S = state.snapshot && state.snapshot.stats;

    // KPIs
    const kpis = el("div", { class: "ct-kpis" });
    const completeTotal = S
      ? Object.values(S.runs || {}).reduce((a, m) => a + (m.complete || 0), 0)
      : 0;
    const sealedModes = S ? Object.values((S.sealed && S.sealed.by_mode) || {}) : [];
    const sTot = sealedModes.reduce((a, m) => a + m.n, 0);
    const sConf = sealedModes.reduce((a, m) => a + m.conflict, 0);
    const subShare = S && S.billing ? pct(S.billing.subscription_share) : "-";
    const paidUsd = S && S.billing ? `$${(S.billing.total_committed_usd || 0).toFixed(2)} paid` : "no snapshot";
    const cards = [
      ["Complete Runs", S ? String(completeTotal) : "-", S ? `saved ${state.snapshot.savedAt.slice(0, 10)}` : "import stats JSON"],
      ["Sealed Conflict Rate", sTot ? pct(sConf / sTot) : "-", sTot ? `${sConf}/${sTot} blind comparisons` : "no sealed runs"],
      ["Subscription Share", subShare, paidUsd],
      ["Active Sessions", String(state.sessions.length), `${state.sessions.filter((s) => s.status === "running").length} running`],
    ];
    for (const [label, val, sub] of cards) {
      const c = el("div", { class: "ct-card" });
      c.append(el("div", { class: "ct-label" }, label), el("div", { class: "ct-val" }, val), el("div", { class: "ct-sub" }, sub));
      kpis.append(c);
    }
    wrap.append(kpis);

    // Two-column main area
    const grid = el("div", { class: "ct-grid" });

    // Left: Lead -> Advisor Disagreement + Spec Checks
    const statsCard = el("div", { class: "ct-card" });
    statsCard.append(el("h2", null, "Lead \u2192 Advisor Disagreement & Checks"));
    const pairs = S && S.sealed && S.sealed.by_pair ? Object.entries(S.sealed.by_pair) : [];
    if (!pairs.length) {
      statsCard.append(
        el("div", { class: "ct-sub" }, "No sealed comparison snapshot loaded yet. Run `coldtake stats --json` and click Import.")
      );
    } else {
      for (const [pair, st] of pairs) {
        const hdr = el("div", { class: "ct-row", style: "border-bottom:none;padding:2px 0" });
        hdr.append(
          el("strong", null, pair),
          el("span", { class: "ct-sub" }, `n=${st.n} · conflict ${pct(st.conflict_rate)} · agree ${pct(st.agree_rate)}`)
        );
        const bar = el("div", { class: "ct-bar" });
        for (const [cls, r] of [["ct-agree", st.agree_rate], ["ct-partial", st.partial_rate], ["ct-conflict", st.conflict_rate]]) {
          if (r > 0) bar.append(el("div", { class: cls, style: `width:${(r * 100).toFixed(1)}%` }));
        }
        statsCard.append(hdr, bar);
      }
    }
    if (S && S.checks && S.checks.total) {
      const v = S.checks.verdicts;
      const box = el("div", { style: "margin-top:12px;display:flex;gap:6px;flex-wrap:wrap" });
      box.append(
        el("span", { class: "ct-chip ct-ok" }, `sound: ${v.sound}`),
        el("span", { class: "ct-chip ct-warn" }, `concerns: ${v.concerns}`),
        el("span", { class: "ct-chip ct-bad" }, `unsound: ${v.unsound}`)
      );
      statsCard.append(box);
    }
    grid.append(statsCard);

    // Right: Recent Omnigent sessions (with 1-click open)
    const sessCard = el("div", { class: "ct-card" });
    sessCard.append(el("h2", null, "Recent Sessions"));
    if (!state.sessions.length) {
      sessCard.append(el("div", { class: "ct-sub" }, "No active sessions found."));
    } else {
      for (const s of state.sessions.slice(0, 12)) {
        const row = el("div", { class: "ct-row" });
        const info = el("div");
        info.append(
          el("div", null, s.title || s.id.slice(0, 12)),
          el("div", { class: "ct-sub" }, `${s.status}${s.gitBranch ? ` · ${s.gitBranch}` : ""}`)
        );
        const openBtn = el("button", { class: "ct-btn", type: "button" }, "Open");
        openBtn.addEventListener("click", () =>
          callHost("navigation.openSession", { sessionId: s.id }).catch(() => {})
        );
        row.append(info, openBtn);
        sessCard.append(row);
      }
    }
    grid.append(sessCard);
    wrap.append(grid);

    root.append(wrap);
  }

  async function loadAll(root, state) {
    try {
      const [theme, sessPage, snap] = await Promise.all([
        callHost("theme.getCurrent", {}).catch(() => null),
        callHost("sessions.listPage", { limit: 25 }).catch(() => ({ sessions: [] })),
        callHost("storage.user.get", { key: "stats_snapshot" }).catch(() => null),
      ]);
      applyTheme(theme);
      state.sessions = (sessPage && sessPage.sessions) || [];
      state.snapshot = snap;
    } catch {
      // Render gracefully with partial data.
    }
    renderScorecard(root, state);
  }

  window.addEventListener("message", (event) => {
    const data = event.data;
    if (!data || data.source !== "omnigent-extension" || data.type !== "init") return;
    if (data.nonce !== identity.nonce || !event.ports || !event.ports[0]) return;
    port = event.ports[0];
    const root = document.getElementById("root");
    const state = { sessions: [], snapshot: null };

    port.onmessage = (ev) => {
      const msg = ev.data;
      if (!msg || msg.source !== "omnigent-extension") return;
      if (msg.type === "response" && pending.has(msg.requestId)) {
        const entry = pending.get(msg.requestId);
        pending.delete(msg.requestId);
        if (msg.error) entry.reject(new Error(msg.error.message || msg.error.code));
        else entry.resolve(msg.result);
      } else if (msg.type === "event" && msg.event === "theme") {
        applyTheme(msg.value);
      }
    };
    port.start();
    send({ type: "ready" });
    callHost("theme.subscribe", {}).catch(() => {});
    loadAll(root, state);
  });
})();
