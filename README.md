<!-- mcp-name: io.github.vicennial/coldtake -->

# coldtake

**Blind second opinions from another model family — for any big AI decision. Zero API keys to start.**

When you ask your AI *"Is this a good plan?"* — or ask a second AI to review the first one's draft — the reviewer anchors on what's already written. It either agrees (*"You're absolutely right!"*) or invents nitpicks to look thorough. `coldtake` fixes that locally: your AI writes its answer down and seals it in an envelope, `coldtake` sends only the raw question to a **different model family** (`claude`, `codex`, or `gemini`) using the CLIs you already pay for, and compares the answers only after both are in.

Use it for **any significant decision** — product strategy, pricing, architecture, hard emails, contracts, research, or code.

| Run it | What it does |
|---|---|
| `uvx coldtake try` | 10-second offline demo (zero network, zero API keys) |
| `uvx coldtake doctor` | Shows installed CLIs and the cross-family routes `coldtake` builds |
| `uvx coldtake install claude --with-skill` | Wires MCP + decision skill into Claude Code, Codex, Gemini, or Cursor |
| `coldtake ask "Raise price 2x now?"` | One-shot blind second opinion from your terminal |

---

## How does `coldtake` work?

```text
Your AI (e.g. Claude)                  coldtake (local MCP)                 Other family (e.g. Codex)
─────────────────────                  ────────────────────                 ─────────────────────────
1. Writes its own answer
2. Calls coldtake_alternate(
     question=...,
     sealed_answer="Raise 2x..." ──►   Locks answer in envelope
   )                                   Strips your draft completely   ──►   Sees ONLY the raw question
                                                                            & neutral constraints
                                       Gets independent cold take     ◄──   "Fix churn first; add a tier"
                                       Opens envelope & compares
3. Sees exact clash              ◄──   { relation: "conflict",
                                         crux: "churn before repricing" }
```

- **Truly blind by construction:** Your AI's draft is never rendered into any advisor prompt. Blind calls reject leading framing and `primary_answer`. (Sealing your answer is optional for quick terminal asks, and powers automatic `CONFLICT` detection + your disagreement scorecard when used.)
- **Reuses your existing CLI logins ($0):** Runs `claude`, `codex`, and `gemini` CLIs in tool-less, read-only sandboxes (prompts on stdin, never argv). Never reads or copies login tokens.
- **Fail-closed paid fallback:** OpenRouter is off unless `OPENROUTER_API_KEY` is set. Worst-case cost is authorized in an atomic SQLite ledger before spawn; ambiguous post-send failures never retry or double-bill.
- **Idempotent & crash-safe:** Retrying a `request_id` returns the same job and never starts a second charge.

---

## Why not just ask the same AI or ask another AI to "review my draft"?

| Approach | Stops anchoring? | Avoids nitpick bias? | Works with $0 CLI logins? | Tracks conflict rates? |
|---|---|---|---|---|
| Ask the same AI *"are you sure?"* | No | No | Yes | No |
| Paste draft into another AI (*"review this"*) | No (anchors on draft) | No (**6× more spurious nitpicks**) | Manual copy-paste | No |
| **`coldtake` blind alternate / panel** | **Yes** (draft stripped) | **Yes** (**0% false alarms** in eval) | **Yes** (`claude`, `codex`, `gemini`) | **Yes** (`coldtake stats` / HTML report) |

---

## What tools does the MCP server expose?

Every tool has a strict Pydantic JSON schema:

| Tool | Blind? | Delivery | When to use |
|---|---|---|---|
| `coldtake_alternate` | Yes (`sealed_answer` optional) | Blocking | Independent cold take from another model family before a material decision. |
| `coldtake_panel` | Yes (`sealed_answer` optional) | Blocking | 2–3 distinct model families + agreement map for high-stakes or contested calls. |
| `coldtake_check` | Evaluates spec/plan | Async (ACK) | Fast one-turn spec, strategy, or design check (`verdict`: `sound` / `concerns` / `unsound`). |
| `coldtake_review` | No (`primary_answer`) | Blocking | Critique an existing draft when catching omissions or correctness errors is the goal. |
| `coldtake_submit` | By `mode` | Async (ACK) | Start any job asynchronously and poll with the same `request_id`. |
| `coldtake_status` | — | Instant | Check progress or recover stale runs cleanly. |
| `coldtake_result` | — | Instant | Read the verified result (inline, or file path + hash over 64 KB). |

---

## How do I see if second opinions are actually helping?

`coldtake` tracks every run locally in `~/.local/state/coldtake/state.db`:

```bash
coldtake stats                         # Lead->advisor conflict matrix, check verdicts, latency, spend
coldtake stats --share                 # Anonymized scorecard safe to paste (zero prompt text)
coldtake report --out scorecard.html   # Self-contained interactive HTML scorecard
coldtake history                       # Recent runs with status, relation/verdict, and cost
coldtake show <request_id>             # Full memo, sealed comparison crux, and attempt trace
```

The MCP server also exposes read-only `coldtake://stats` and `coldtake://history` resources over both `stdio` and loopback `http`.

### Omnigent UI extension (`coldtake-omnigent`)

If you use [Omnigent](https://github.com/omnigent-ai/omnigent), [integrations/omnigent](https://github.com/vicennial/coldtake/tree/main/integrations/omnigent) ships a sandboxed Extension API v1 **Coldtake Scorecard** sidebar page (`pip install coldtake-omnigent` / `uv pip install coldtake-omnigent`). It is conflict-free with `polly` and `debby`, requires no extra HTTP API on `coldtake`, and runs entirely inside Omnigent's declared host RPC capabilities.

---

## What did the 54-task benchmark measure?

`coldtake` ships a 54-task audited evaluation suite ([evals/README.md](https://github.com/vicennial/coldtake/blob/main/evals/README.md)) covering coding bugs (graded by hidden unit tests), architecture decisions (option-ID scored), and technical claims across four arms (`self_review`, `cross_review`, `blind_same`, `blind_other`).

On a full **432-run subscription-only baseline** (`claude-opus-5-5` medium + `gpt-6.2-astra` high, `$0.00` paid spend, 0 invalid, 0 ungraded):

| Metric (pooled across both lead families) | `self_review` | `cross_review` | `blind_same` | `blind_other` |
|---|---|---|---|---|
| **Lenient recall** (identified + partial, n=72) | 100.0% (72/72) | 100.0% (72/72) | 100.0% (72/72) | 100.0% (72/72) |
| **Strict recall** (exact rubric detail, n=72) | 100.0% (72/72) | 100.0% (72/72) | 95.8% (69/72) | 93.1% (67/72) |
| **Hidden unit tests passed** (code errors, n=24) | 100.0% (24/24) | 100.0% (24/24) | 100.0% (24/24) | 100.0% (24/24) |
| **False alarms on clean controls** (n=36) | 2.8% (1/36) | 5.6% (2/36) | **0.0% (0/36)** | **0.0% (0/36)** |
| **Spurious nitpicks** (LLM-graded code+claim, n=72) | 58 (0.81/task) | 62 (0.86/task) | **12 (0.17/task)** | **8 (0.11/task)** |

- **6× fewer spurious nitpicks & 0% false alarms in blind mode:** Asking an AI to *review* a draft triggered defect-hunting bias (120 spurious findings across 144 review runs, 3 false alarms on clean controls). Solving blind dropped spurious findings to 20 across 144 runs (**8 in `blind_other`**) with **0/72 false alarms**.
- **Clean sealed comparison separation (n=216 blind runs):** `0%` false `agree` on planted-error tasks (105 `conflict`, 39 `partial`, 0 `agree`) and `0%` false `conflict` on clean controls (66 `agree`, 6 `partial`, 0 `conflict`).
- **Honest caveat:** Frontier models hit a ceiling on planted single-defect tasks (100% lenient recall in all arms, 0% wrong-answer survival), so this corpus separates **precision and noise** rather than recall at frontier scale. Median latency: `review` 19.2s, `alternate` 23.6s.

---

## FAQ

- **Do I need OpenRouter or API keys?** No. `coldtake` uses whichever of `claude`, `codex`, and `gemini` CLIs you are already logged into. Paid OpenRouter fallback is completely opt-in.
- **Do I have to pass a sealed answer every time?** No. Leaving `sealed_answer` out runs a fast 1-call blind second opinion. Passing `sealed_answer` adds automatic `agree / partial / conflict` grading and populates `coldtake stats`.
- **Does `coldtake` touch my credentials or run tools in advisor sessions?** No. Advisor CLIs run in stripped, tool-less, read-only sandboxes with prompts passed over stdin. Loopback HTTP (`coldtake serve --transport http`) binds only to `127.0.0.1` / `::1`.

---

## Documentation

- Architecture & protocol: [docs/design.md](https://github.com/vicennial/coldtake/blob/main/docs/design.md)
- Extension API (`API_VERSION = 2`): [docs/extending.md](https://github.com/vicennial/coldtake/blob/main/docs/extending.md)
- Agent decision skill: [skills/coldtake/SKILL.md](https://github.com/vicennial/coldtake/blob/main/skills/coldtake/SKILL.md)
- Omnigent UI extension: [integrations/omnigent/README.md](https://github.com/vicennial/coldtake/blob/main/integrations/omnigent/README.md)
- Evaluation harness: [evals/README.md](https://github.com/vicennial/coldtake/blob/main/evals/README.md)
- Releasing: [RELEASING.md](https://github.com/vicennial/coldtake/blob/main/RELEASING.md)

Requires Python 3.11+ and `mcp >= 1.30`. License: Apache-2.0.
