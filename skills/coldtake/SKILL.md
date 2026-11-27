---
name: coldtake
description: Ask a different model family for a blind second opinion before presenting a recommendation, or critique an existing answer. Use for material decisions (irreversible, novel or uncertain, anchoring risk, several options with different downsides) and for answers with correctness risk. Not for routine facts or reversible steps.
---

# coldtake: when to ask for a cold take

coldtake is an MCP server. Its advisors come from a different model family than yours.
Calling it costs time and, on the paid fallback, money, so use it on purpose.

## Decide

| Situation | Do |
|---|---|
| Routine fact, lookup, or an easily reversible step | Stay solo. Do not call coldtake. |
| A lightweight one-turn spec, design or architecture check | `coldtake_check` (async; poll `coldtake_status` / `coldtake_result`). |
| You already have an answer or draft and correctness or omissions are the risk | `coldtake_review` with `primary_answer`. |
| A material decision: irreversible, novel or uncertain, anchoring risk, or several options with different downsides | `coldtake_alternate` BEFORE you present a recommendation, with your own answer sealed. |
| The user explicitly asks for a panel, or the decision is high-stakes and irreversible, or your answer and the alternate conflict | `coldtake_panel`. |
| The user says not to use it | Do not. |

Do not use a panel just to get more samples.

## Alternate with a sealed answer

1. Write your own answer first.
2. Call `coldtake_alternate` with the question, goals, constraints and neutral context.
   Put your answer in `sealed_answer`. It is stored and hashed, never shown to the advisor.
   Do not paste your answer or conclusions into `question`, `goals`, `constraints` or
   `context`; the server rejects obvious cases and warns when long spans overlap.
3. If you have a short list of choices, pass `options` (`[{id, label}]`, 2 to 8) and your
   pick as `sealed_choice`. The comparison is then deterministic.
4. Read `comparison`: `sealed.relation` is `agree`, `partial` or `conflict`.
   `comparison.status = "unavailable"` means the comparison step failed; the memos are
   still valid, and you must compare them yourself.
5. Present your recommendation. Say which mode you used and why, whether the advisor
   agreed, and any paid spend.

## Long jobs

Prefer `coldtake_submit`, then poll `coldtake_status` and read `coldtake_result` with the
same `request_id`. A retried `request_id` with the same packet never starts a second job
or a second charge; the same id with a different packet is rejected. Results over 64 KB
come back as a file path and a SHA-256; read the file and check the hash.

## Terminal states

`failed`, `timed_out`, `lost` and `charge_unknown` are terminal. Report the stored cause and
return to the task. Do not retry a `charge_unknown` job: a paid attempt may have been
billed. For `timed_out` or `lost` you may resubmit under a new `request_id` if still needed.
