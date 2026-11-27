## Second opinions (coldtake)

coldtake is available as an MCP server. Use it deliberately:

- Routine facts and reversible steps: stay solo.
- A lightweight one-turn spec, design or architecture check: `coldtake_check` (async; poll
  `coldtake_status` / `coldtake_result`).
- An existing answer with correctness risk: `coldtake_review`.
- A material decision (irreversible, novel or uncertain, anchoring risk, or several
  options with different downsides): `coldtake_alternate` BEFORE presenting a
  recommendation. Pass your own answer as `sealed_answer` (and `sealed_choice` if you
  list `options`); never put it in the question or context.
- `coldtake_panel` only on explicit request, for high-stakes irreversible decisions, or
  when your answer and the alternate conflict.
- Long jobs: `coldtake_submit`, then `coldtake_status` / `coldtake_result` with the same
  `request_id`. Treat `charge_unknown`, `timed_out` and `lost` as terminal.
- Always tell the user which mode you used and any paid spend.
