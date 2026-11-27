# coldtake — design (0.1)

coldtake is a local MCP server that gives a coding agent a blind second opinion
from the *other* model family, using the CLIs the user is already logged into
(`claude`, `codex`), with an optional paid OpenRouter fallback under hard USD caps
that fail closed.

Source of the ideas: a private router used ~1,050 times over 10 weeks. This is a
clean re-implementation of its generic core, not a copy of its history.

## Modes

| Mode | Advisor sees the lead's answer? | Output |
|---|---|---|
| `review` | Yes (`primary_answer`) | Critique memo |
| `alternate` | No. Lead may *seal* its answer | Independent memo + comparison with the sealed answer |
| `panel` | No. Lead may seal its answer | 2–3 independent memos (distinct families) + agreement map (+ sealed comparison) |
| `check` (`coldtake_check`) | Evaluates supplied spec/context | Lightweight one-turn verdict, key risks, recommendation, alternatives (async) |

## Sealed answer (new)

Problem: in blind modes nothing compared the blind answer with the lead's own
answer, so blindness was unverifiable and the result was hard to act on.

- `alternate` and `panel` accept optional `sealed_answer` (string) and optional
  `sealed_choice` (an option id, see below).
- The router stores it in run state and returns `sealed_sha256` in the ACK.
  It is NEVER rendered into any advisor prompt (test this with a canary string).
- After all advisor memos are final (immutable), the router runs the comparison
  step and attaches `comparison` to the result.
- `review` rejects `sealed_answer` (the answer is already visible there).

## Agreement map (replaces the old lexical disagreement map)

Problem: the old map clustered recommendations by exact normalized text, so
every panel (34/34 in real use) was marked "material disagreement".

Two paths:

1. **Options path (deterministic).** If the packet lists `options`
   (`[{id, label}]`, 2–8 items), each advisor must return `choice` = one option id
   (or `"none"` with a reason) in its structured output. Clusters are by `choice`.
   The sealed comparison is `sealed_choice == choice`.
2. **Judge path (free-form).** Without options, one comparison call runs on a
   tool-free route (prefer a subscription route of a family that is NOT the lead
   family; any already-used advisor family is acceptable). Input: the question and
   the final memos (+ sealed answer when present), labelled A/B/C in a shuffled
   order with model names hidden. Output schema:
   - `positions`: for each memo, a one-sentence canonical stance
   - `clusters`: groups of memo labels that recommend materially the same action
   - `pairwise`: `{a, b, relation: agree|partial|conflict, crux}` 
   - `sealed`: `{relation: agree|partial|conflict, crux}` when sealed answer given
   - `contradictions`: claim-level conflicts with the memo labels involved
   - `material`: true iff more than one cluster OR any `conflict`
   The judge call obeys the same budget, timeout, fail-closed and charge-unknown
   rules as advisor calls. If it fails, return memos with
   `comparison: {status: "unavailable", cause}` — never fall back to the lexical map
   for `material`.

   The judge is planned apart from the advisors (`Plan.judge`, default: the advisor
   chain). With `preflight_ladder_budget` on (an explicit judge ladder is left as it is when
   only `prune_paid_if_unaffordable` is set; with no judge ladder the judge uses the
   possibly pruned chain) a paid judge route is kept only if its worst case fits
   beside the advisors' worst case under the per-request cap. A sealed answer makes the
   judge prompt larger, but it never removes an advisor route: the unaffordable judge
   routes are dropped and listed in the stored plan (`judge_dropped`), and with no judge
   route left the comparison is `unavailable` with cause `judge_unaffordable`.

   A judge reply that fails validation gets one repair like any other attempt, asked for
   the comparison schema through the contract's `judge_repair_prompt` (not the memo repair
   prompt).

   A paid judge whose attempt may have been billed unseen (`charge_unknown`) does not
   fail the job: the memos are final and valid, so the job stays `complete`, and
   `comparison` is `{status: "unavailable", cause: "charge_unknown", cause_detail}`. The
   result and the status view also carry a top-level `charge_unknown: true`, and the
   judge's allowance stays held (`paid.charge_unknown_held_usd`).
   Keep the cheap deterministic diffs (unique assumptions, forecast/feasibility
   differences) as supplementary fields, clearly labelled heuristic.

Panel defaults to 3 distinct families when 3 are available (two subscription
families + one paid family if the budget allows, else 2) and reports the actual
count. Memos are immutable before any cross-exposure; no debate rounds in 0.1.

Option ids may be written in any case (`A`, `B`, `C`); ids, `sealed_choice` and the
advisor's `choice` are normalized to lower case. The judge must give one stance per memo
label; a stance for the sealed position `S` is optional. The sealed relation is `agree`
when S agrees with every memo, `conflict` when it conflicts with every memo and `partial`
otherwise, and is recomputed locally.

## Blindness enforcement

- Schema: blind modes reject `primary_answer`.
- Contamination check on question/goals/constraints (keep the existing regex idea:
  "my answer is", "I think we should", "the consensus", etc.).
- New framing check (advisory, not blocking): if `sealed_answer` is given, warn when
  long verbatim spans (≥ 12 consecutive words) of the sealed answer appear in the
  question/context. Return `warnings` in the ACK.

## Routing

- Lead family: detected from MCP `clientInfo.name` (claude-code → anthropic,
  codex → openai), overridable by config `lead_family`. Advisors come from other
  families first. The lead's own family is a last resort: it may be the LAST ladder entry
  or the LAST panel member, and its memo is labelled `same_family: true`. A panel needs
  at least two members (at most 8), or it is refused before the ACK.
- Replacement and escalation pools are planned before anything runs, so they are
  filtered when they are used: a route of a family that already has a memo in the job,
  or a route that already failed in it, is skipped. A panel never seats one family twice
  (the lead family can be the labelled last member only once).
- Providers: `claude` CLI, `codex` CLI, `gemini` CLI, OpenRouter (HTTP).
- Provider commands run tool-less and sandboxed (claude: `--safe-mode`, empty
  setting sources, empty tools, strict empty MCP config, no session persistence,
  `--json-schema`; codex: `--ignore-user-config --ephemeral --sandbox read-only
  --output-schema`; gemini: `--output-format json --sandbox`). Prompts go on stdin, never argv.
- Never read, copy or store CLI login tokens. Binary paths come from PATH or config.
- Fallback only on classified safe failures (usage limit, expired login, pre-accept
  overload/unavailable, rate limit). Refusal/policy/unknown/charge-unknown are
  terminal.
- On a PAID route, fallback and terminal failures are trusted only with an explicit
  allow-list entry: the provider sets `ProviderFailure.proven_unsent` when it can prove
  the request never reached inference. The built-in OpenRouter provider does so only for
  the HTTP statuses 400, 401, 402, 403, 404, 413, 422 and 429 and for a connection that
  was never made. It also claims the one 503 whose body is OpenRouter's documented "no
  available model provider meets your routing requirements" error, raised before
  inference (matched conservatively on that message, which is kept in the failure). Every
  other failure after the request was sent (any other 503, 502, 408, other 5xx) is
  ambiguous by policy and is `charge_unknown`. Any other paid failure is
  treated as `charge_unknown`: the allowance stays held, the ladder stops, and nothing is
  ever released on a guess. A failure that carries a reply (`accepted`, `repair`) is
  committed.
- Model ids and efforts live in a user-editable YAML with sensible defaults.

## Budget (OpenRouter)

- Off unless `OPENROUTER_API_KEY` is set. Defaults: per-request cap $1, daily cap $5.
- Worst-case cost is computed from rendered prompt bytes + max output tokens at
  configured price ceilings, before spawn. Atomic per-attempt authorization against
  a UTC-day ledger with a file lock. Unused fallbacks consume nothing. Post-spawn
  ambiguity is recorded as `charge_unknown` and keeps its full allowance.
- Guarded request: no SDK retries, no model substitution, `max_tokens` enforced,
  provider-reported cost recorded when available.
- `charge_unknown` outranks every other terminal cause. In a panel every member is
  allowed to finish, and if any of them ended `charge_unknown` the job is
  `charge_unknown`, whatever the earlier members' failures were. One rule decides this
  for a failing worker and for recovery of a crashed or lease-expired run, in the same
  transaction as the status change: any paid attempt still `authorized` is held as
  `charge_unknown`, and a run with any `charge_unknown` row (just held or finalized
  earlier) ends `charge_unknown`, never `lost` or `timed_out`. A job that completes never
  leaves an attempt `authorized` either: a judge attempt nobody saw the end of makes the
  comparison `unavailable` with cause `charge_unknown` and sets the top-level flag.
- Job billing comes from the ledger, not from the route that failed. Every status view
  has `billing` (`paid` once any paid attempt was dispatched and not released,
  otherwise `subscription`) and `charge_unknown` (a paid attempt may have been billed
  unseen). A failed job also has `failed_route_billing`: the billing of the route whose
  failure decided the job, which can differ (a subscription timeout after a paid member
  was committed is `billing: paid`, `failed_route_billing: subscription`). The complete
  result carries `billing` and `charge_unknown` too.

## Jobs and API

MCP tools: `coldtake_submit`, `coldtake_status`, `coldtake_result`,
`coldtake_check` (async one-turn spec/architecture check),
plus blocking conveniences `coldtake_review`, `coldtake_alternate`,
`coldtake_panel`. Every tool exposes a typed Pydantic JSON schema. Idempotent `request_id`;
the same id + same packet returns the same job; same id + different packet is rejected.
Client disconnect never cancels or re-bills. Active provider jobs per engine are bounded
by `limits.max_concurrent_jobs`, and `Engine.wait` wakes via per-job events rather than
polling. Restart: orphaned running jobs become terminal (`lost`,
or `charge_unknown` if an attempt may have been accepted). Stale `running` jobs
must not stay `running` forever.

`serve` handles SIGTERM as a graceful stop on both transports: running jobs are drained
(`limits.drain_seconds`), any CLI still running is killed, and the exit status is 0 (1
if the drain timed out and jobs were cut off). The handler stays installed until the drain
is done, so a SIGTERM that arrives after stdin closed does not skip it. A second SIGTERM
during the drain ends the process at once, and whatever was cut off is recovered as
`lost` or `charge_unknown` on the next start.

Transports: stdio (default, for `uvx coldtake`) and `--transport http`. The HTTP
transport accepts only the hosts `127.0.0.1`, `localhost` and `::1`, and has NO
authentication: any local process that can reach the port can submit jobs and read
results. Use stdio unless you need HTTP, and do not forward the port.

## Local stats

`coldtake stats` prints from local run state: counts by mode, route success,
fallbacks, paid spend, latency percentiles, and — new — sealed-comparison
agreement rates (agree/partial/conflict) per mode and advisor family.

## When to ask (shipped policy)

Ship `skills/coldtake/SKILL.md` and an AGENTS.md snippet: routine facts and
reversible steps stay solo; an existing answer with correctness risk → review;
a material decision (irreversible, novel/uncertain, anchoring risk, several options
with different downsides) → alternate with a sealed answer BEFORE presenting a
recommendation; panel only on explicit request, high-stakes irreversible decisions,
or when lead and alternate conflict. Disclose the mode used and any paid spend.

## Out of scope for 0.1

Website/design review lanes, retrieval proxies, namespaces, host model registries,
debate rounds, artifact chunking beyond a simple size cap (results > 64 KB are
written to a file in run state and returned by path + sha256).

## Public-tree rules

Apache-2.0. No hostnames, IPs, ports other than defaults, home paths, company or
private project names, real handles. Demo content is fictional. See CONTRIBUTING.md.
