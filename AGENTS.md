# AGENTS.md — Using `coldtake`

`coldtake` is a local MCP server and CLI that gives AI agents blind second opinions from another model family (`claude`, `codex`, `gemini`) before committing to a significant decision.

## When to call `coldtake`

Call `coldtake` on **any material decision** — architecture, product strategy, pricing, writing/emails, negotiation, ops changes, or tricky code — when:
- the decision is hard to reverse or expensive to undo;
- there are multiple reasonable options with different downsides;
- you suspect anchoring or confirmation bias on your own draft; or
- correctness or subtle omissions carry real risk.

Skip `coldtake` for routine facts, obvious typos, or cheap reversible edits.

## Which tool to use

- **`coldtake_alternate`** (default for decisions): Write your own position into `sealed_answer` (optional), send the neutral question without leading language, and get a blind memo + independent comparison (`agree` / `partial` / `conflict` + `crux`).
- **`coldtake_panel`**: Same blind protocol across 2–3 distinct model families when stakes are high or an `alternate` returned `conflict`.
- **`coldtake_review`**: Show `primary_answer` directly when critiquing an existing draft or checking correctness/omissions.
- **`coldtake_check`**: Fast one-turn spec, design, or plan check returning `verdict` (`sound` / `concerns` / `unsound`).
- **`coldtake_submit` / `coldtake_status` / `coldtake_result`**: Async equivalents when you want to continue other work while advisors run.

## Local commands

```bash
uvx coldtake try                                 # 10s offline demo (no network, no API keys)
uvx coldtake doctor                              # inspect installed CLIs and cross-family routes
uvx coldtake install claude --with-skill         # wire MCP + skill (claude, codex, gemini, cursor)
coldtake stats --share                           # anonymized conflict & spend scorecard
coldtake report --out scorecard.html             # standalone interactive HTML scorecard
```
