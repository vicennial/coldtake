# Extending coldtake

coldtake has a few small extension points so another package can sit on top of it
without forking. An extension adds behaviour; it never needs to patch coldtake.
The tests exercise every point below with a toy extension (`tests/toy_ext.py`).

## 1. Providers

A provider executes routes. It has the same interface as the built-in `claude`,
`codex`, `gemini` and `openrouter` providers:

```python
class MyProvider:
    name = "mine"  # unique; an entry point cannot shadow a built-in name

    def __init__(self, config, env): ...  # config: coldtake.config.Config, env: a mapping

    def available(self) -> bool: ...  # False hides its routes from planning

    def complete(self, prompt, schema, route, timeout) -> coldtake.providers.Completion: ...
```

`complete` returns `Completion(data=<parsed JSON object>, cost_usd=<float or None>)` or
raises `coldtake.failures.ProviderFailure(reason, kind, message)`. The `kind` is the
fallback classification, and the engine acts on nothing else:

| kind | meaning | engine action |
|---|---|---|
| `fallback` | usage limit, expired login, rate limit, overload or unavailability before the request was accepted | try the next route |
| `repair` | answered, but not valid for the schema | one repair attempt, then stop with `schema_exhausted` (fallback only if the contract opts in) |
| `terminal` | refusal, policy, unknown | stop the job (`failed`) |
| `timed_out` | a subscription attempt ran past its deadline | stop the job (`timed_out`) |
| `charge_unknown` | a PAID attempt that may have been sent and billed | stop the job (`charge_unknown`), keep the allowance |

A failure of a PAID route is trusted as safe only when the provider can prove the
request never left: it sets `ProviderFailure(..., proven_unsent=True)` (a connection that
was never made, an HTTP rejection before inference). The built-in OpenRouter provider
claims it only for the statuses 400, 401, 402, 403, 404, 413, 422 and 429, and for the
503 whose body is OpenRouter's documented "no available model provider meets your routing
requirements" error (matched on that message, kept in the failure). Every other failure
after the request was sent (any other 503, 502, 408, other 5xx) is ambiguous by policy and
is `charge_unknown`. The engine treats any other paid
`fallback` or `terminal` failure as `charge_unknown`: it keeps the allowance, stops the
ladder and never releases it. A failure that carries a reply (`accepted=True`, or kind
`repair`) is committed. `proven_unsent` is the explicit allow-list; reason codes and
causes are never trusted on their own.
The engine also sets `ProviderFailure.route_billing` on a failure that leaves a ladder.

`coldtake.failures.classify(text)` classifies CLI error text the same way the
built-ins do. Billing is a property of the route: `Route.billing` is `"subscription"`
or `"paid"`. For a paid route the engine authorizes a worst-case cost against the
ledger before calling `complete`, so the route must carry its price ceilings
(`prompt_usd_per_mtok`, `completion_usd_per_mtok`, `max_output_tokens`) and the
provider must follow the paid rules: `charge_unknown` for anything ambiguous after the
request may have left, and `fallback` or `terminal` (with `accepted=False`) only when
the request provably was not accepted. `charge_unknown` is never valid for a
subscription route.

Register a provider in one of two ways:

```toml
# the extension's pyproject.toml
[project.entry-points."coldtake.providers"]
mine = "mypackage.providers:MyProvider"
```

```yaml
# or in coldtake's config.yaml
providers:
  mine: mypackage.providers:MyProvider
```

The entry point or config value is a class or factory called as `factory(config, env)`.

A provider registered through the config mapping MAY replace a built-in one (`claude`,
`codex` or `openrouter`) by using that name as its key; the instance's `name` must equal
the key. This is how an operator overrides the built-in failure classification. An entry
point can never shadow a built-in name, because a package installed in the environment
must not silently take over a provider.

## 2. Route planner

By default the ladder comes from `subscription:` and `openrouter:` in the config. To
take model ids and ladders from elsewhere, name a resolver:

```yaml
routing:
  resolver: mypackage.routing:resolve
```

```python
def resolve(ctx: RouteContext) -> Plan | list[Route]: ...
```

`RouteContext` carries `lead_family`, `mode`, `packet` (the contract's validated packet,
so stakes and reversibility are visible), `contract`, `namespace`, `prompt_bytes`,
`config` and `providers`. A class is instantiated with the config and then called the
same way. The result is a plain list of routes (the ladder; panel members are its
families in order) or a `Plan`:

| field | meaning |
|---|---|
| `chain` | review/alternate ladder; also the judge ladder |
| `slots` | panel members, one ladder each (same-family continuity routes are tried in order inside one member) |
| `reserve` | replacement pool, used when a member's whole ladder failed safely |
| `escalation`, `escalate(memos)` | extra member(s), run after round 0 when the predicate is true, with their own main allowance |
| `parallel` | members run at once (default: all) |
| `preflight_ladder_budget` | refuse before ACK (`BudgetExceeded`) when the whole paid ladder (initial and repair attempt of every paid route in the chain, the members, the replacement pool and the escalation pool) cannot fit `budget.per_request_usd`; the judge is planned apart from the advisors, see below |
| `prune_paid_if_unaffordable` | in single-advisor modes, drop the paid routes instead of refusing |
| `judge` | the comparison (judge) ladder; `None` means `chain`. Planned apart from the advisors, see below |

Routes whose provider is unknown or unavailable are dropped. Routes of the lead family
are dropped too, except that one may remain as the LAST ladder entry or the LAST panel
member: the lead's own family is a last resort, used only when the other families
cannot answer. Its memo record carries `same_family: true` (every memo record has the
field), and the result shows it, so a reader can tell that independence was not
achieved. A panel with fewer than two planned `slots` is refused before the ACK; the replacement
pool never counts as a member. A panel has at
most 8 members (`panel.max_members` cannot exceed 8); extra planned members are
ignored and escalation is skipped when the panel is full.

The judge is planned separately from the advisors. The advisors are checked against the
cap first (a paid ladder that cannot fit is pruned or refused as above, whatever the
judge would cost). Then, with `preflight_ladder_budget` on (an explicit judge ladder is left
as it is when only `prune_paid_if_unaffordable` is set; with no judge ladder the judge uses
the possibly pruned chain), each paid judge route is kept only if its worst case, with
the sealed answer and an estimate of the memos, fits beside the advisors' worst case. The
others are removed from the judge ladder and listed in `Plan.judge_dropped`; with no judge
route left the comparison is `unavailable` with cause `judge_unaffordable`. A sealed
answer therefore never removes an advisor route. `plan_view(plan)` (the stored
`plan_json`, `RunView.plan`) has `judge` and `judge_dropped` next to `chain`.

Replacement (`reserve`) and `escalation` pools are filtered when they are used: a route of
a family that already has a memo in the job, and a route that already failed in it, are
skipped, so a panel never seats a family twice. Order the pools by preference; the
engine does the rest.

A paid route carries its price ceilings and optional `input_reserve_tokens`,
`context_tokens` and `endpoints_only` (sent as `provider.only`). The engine refuses a
plan (before the ACK, with `BudgetExceeded`) when a paid route has a non-finite or
negative price, a completion price or `max_output_tokens` that is not above zero, or a
worst case that is not finite and above zero. A provider may define
`worst_case_cost(route, prompt_bytes)` and `context_fits(route, prompt_bytes)` to
replace the default estimates.

The plan the engine admits is stored on the run (`plan_json`, shown as `RunView.plan`)
and that stored plan is what the job runs. A replayed submission is answered from the
stored run and never calls the resolver again.

## 3. Extra MCP tools

An extension is a callable `register(api)`, found through the `coldtake.extensions`
entry-point group or the `extensions:` config list. `api` is an
`coldtake.server.ExtensionAPI`:

```python
def register(api):
    @api.tool("coldtake_mine_count", "Count stored runs.")
    async def count() -> dict:
        with api.engine.store.connect() as db:
            return {"runs": db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]}
```

- `api.mcp`: the `FastMCP` server.
- `api.engine`: `engine.store` (job store), `engine.ledger` (budget ledger),
  `engine.providers`, `engine.submit/status/result/wait`, `engine.has_run(request_id)`,
  `engine.run_view(request_id, include_result=True)`, and
  `engine.run_chain(request_id, routes, prompt, schema, validate)`, which runs a prompt
  down a route ladder with the same fallback rules, budget authorization and attempt
  records as a job, and shows up in `coldtake stats`.
- `api.settings`: the per-extension dict from `extensions_config.<name>` in `config.yaml`
  (`api.extension_config(name)` reads any named entry).
- `api.tool(name, description)` registers the tool under `name` and any configured
  aliases of it.
- `api.lead_family(ctx)` detects the caller's family.
- `api.set_instructions(text)` replaces the server instructions shown to the client
  (`api.instructions` reads them).
- `api.register_contract(c)` and `api.register_record_source(s)`: see sections 7 and 8.

## 4. Tool surface

```yaml
tools:
  builtins: true                     # false hides every built-in coldtake_* tool
  extra_prefixes: ["legacy_"]        # coldtake_review is also legacy_review, etc.
  aliases:
    second_opinion: coldtake_alternate
```

Aliases are the same functions under extra names, so behaviour is identical. When an
extension's tools take different schemas, set `tools.builtins: false` and register its own
with `api.tool(...)`. Server settings:

```yaml
server: {name: my-server, stateless_http: true}
limits: {max_request_bytes: 4000000}   # HTTP transport body limit
```

## 5. Instructions and prompt templates

```yaml
instructions: file:/path/to/instructions.txt   # or inline text
prompts:
  review: file:/path/to/review.txt
  alternate: "...{mode_rules}\n{data_rule}\n{request_json}"
```

Templates use `str.format` fields `{mode_rules}`, `{options_rule}`, `{data_rule}` and
`{request_json}`; `{request_json}` is required, other fields are rejected at startup.
A template only ever receives the advisor-visible packet, so a sealed answer cannot be
templated into an advisor prompt. The role and mode header lines are added by coldtake.

## 6. Lead family and namespace

`lead_family` (`COLDTAKE_LEAD_FAMILY`) overrides detection from the MCP client name.
`namespace` (`COLDTAKE_NAMESPACE`) is optional; when set, request ids and the
per-request budget are scoped to it. Left unset there is a single namespace.

## 7. Contracts

A contract defines one kind of job; the engine owns idempotency, leases, routes, budget
and recovery. `coldtake.contracts.Contract` has five required hooks and several
optional ones:

```python
class MyContract(Contract):
    name = "my_contract_v1"                 # stored on the run; part of its identity
    packet_model = MyPacket
    def parse(self, data, mode) -> Parsed   # validate; split off anything sealed
    def mode(self, packet) -> str           # review | alternate | panel | check
    def render_prompt(self, packet) -> str
    def memo_schema(self, packet) -> dict
    def validate_memo(self, data, packet, attempt: AttemptContext | None = None) -> dict
    # optional
    def quick_identity(self, data, mode) -> Any | None    # cheap replay check before disk I/O
    def attempt_seconds(self, packet) -> float | None     # per-attempt timeout override
    def uses_judge(self, packet, *, has_sealed=False) -> bool
    def repair_prompt(self, prompt, prior_raw, error, attempt: AttemptContext | None = None) -> str
    def judge_repair_prompt(self, prompt, prior_raw, error) -> str  # repair of a judge reply
    def compare(self, cx: CompareContext) -> dict | None   # cx.judge(...) runs a tool-free call
    def shape_result(self, run: RunView) -> dict
    def shape_status(self, row, view) -> dict
    def repair_overhead_bytes(self, prompt, route) -> int | None   # planning bound of the repair prompt
    max_repairs = 1                         # 0 disables repair (one-turn contracts like CheckContract)
    fallback_on_schema_exhausted = False    # True: fall back to the next route instead of stopping
    fallback_after_repair_failure = False   # False: a repair attempt failure is terminal, not fallback
    shared_repair = False                   # True: ONE repair per job, not one per route
    max_output_bytes = None                 # optional CLI output ceiling override
    deadline_seconds = None                 # replaces limits.job_seconds
    repair_seconds = None                   # separate allowance for the repair attempt
    inline_result_bytes = None
```

Register it with `api.register_contract(contract)` (or `engine.register_contract`) and
start jobs with `engine.submit(request_id, data, lead_family=..., contract="my_contract_v1")`.
`CoreContract(compare="judge" | "none" | callable)` and `CheckContract(config)` are
built-in contracts and convenient bases to reuse.

Failure semantics apply to every contract. The repair is one attempt, after a reply the
provider returned but that failed validation; `repair_prompt` receives that reply. If it
fails too, the job stops with `schema_exhausted` (unless the contract opts into fallback)
and the last invalid reply is kept as labelled `partial_advice`. `repair_seconds` is
granted only for that attempt. A running job holds a liveness lease, and a lost or timed
out job is recovered on the next read or start.

`repair_prompt` is for memos. A failed comparison (judge) reply is repaired with
`judge_repair_prompt`, whose default asks for the comparison schema; override it if your
`compare` uses its own judge schema. `repair_overhead_bytes` is for memo repairs; a judge
repair is bounded with `judge_repair_prompt`'s own framing.

A contract hook that raises `ProviderFailure` (`validate_memo`, a judge validator)
is treated like a provider failure of that attempt: the attempt is recorded with the
route's billing and the reply's cost, a `fallback` moves to the next route, anything else
stops the job. A hook may also raise `JobFailure` (its status must be `failed`,
`timed_out`, `lost` or `charge_unknown`, else `ValueError`): from `validate_memo` it ends the job with
that status and reason and stops the other panel members; from a judge validator it
becomes the comparison's `compare_error` and the job stays `complete`. Any other exception
from a hook is recorded and ends the job as `internal_error`, with the route's billing in
`failed_route_billing`.

`repair_overhead_bytes(prompt, route)` returns what the repair prompt adds to the original
prompt, so that the budget preflight and the paid-route size check can bound it. The
default is the size of the contract's repair framing plus 4 bytes for every
`max_output_tokens` of the prior reply (a paid route keeps at most that much of it).
Return an integer when your `repair_prompt` is larger or smaller than that. With
`shared_repair = True` the job gets one repair in total: the first attempt that needs it
takes it, and any later attempt that fails validation ends with `schema_exhausted`.
The default `repair_prompt` names the validation error (at most 800 characters), so a
model can correct itself; a custom hook receives the same `error` text.
A paid repair that the budget blocks is also schema exhaustion (terminal), and the last
invalid reply is still kept as `partial_advice`.

Status and result are decided from the stored run status and the contract's shaping is
applied last, so `shape_status` and `shape_result` cannot change when a job counts as
finished. While a job runs, the default status view has `progress`: for each branch
(`advisor1`, `advisor2`, `judge0`, ...) the `phase`, `slot`, `route_index`,
`attempt_index`, `provider`, `model`, `billing`, `dispatched` and `started_at` of the
attempt in flight. `dispatched` is written before the provider is called, so a recovery
or a shaper can tell that a request may have left. When the attempt returns or fails the
entry is kept with `dispatched: false`, `finished_at` and an `outcome`. The same data is `RunView.progress`,
and the stored row (`RunView.row`) also holds `plan_json` and `progress_json`.

### Trust boundary

A contract, a `compare` callable, a resolver, a provider, a `run_chain` caller and
anything with access to the store or `engine` run in the coldtake process with full
access to a job. The sealed answer is stored with the run. A custom `compare` receives it
in `CompareContext.sealed`, and a custom contract, a record source or an extension that
reads the store can see it too. coldtake keeps the sealed text out of every prompt it
builds (advisor, repair, template and escalation prompts) and the tests check that with
canaries, but it cannot stop extension code from sending sealed text anywhere. Extension
code is trusted code: install only extensions you would give the sealed answer to, and
have a contract's own `repair_prompt` and prompt builders work from the packet only.

## 8. Reading results and old records

- `engine.has_run(request_id)`: fast existence check in the core store.
- `engine.load_result(request_id)`: the stored result, inline or from its file, with the
  SHA-256 verified (`LookupError` if not complete, `ValueError` on a mismatch).
- `engine.run_view(request_id, include_result=True)`: a `RunView` with the row, memos,
  attempts and ledger totals (set `include_result=False` to skip reading large artifacts).
- `engine.ledger.attempts(request_id)`: parsed ledger rows (with `attempt_id`) for a run.
- `api.register_record_source(source)`: a read-only `RecordSource` with `status`, `result`,
  `reserved` and optional `claims(request_id, packet_hash)` returning `"replay"`,
  `"collision"`, or `None`. `engine.status/result` consult sources for ids core has no run
  for, and `submit` consults `claims` / `reserved` before admitting a new run.

## 9. Operations and budget

```yaml
accepting_new_jobs: true        # false: refuse NEW jobs; replays and reads keep working
check:
  context_roots: []             # opt-in server-host directories for coldtake_check context_paths
  max_context_file_bytes: 150000
limits:
  max_concurrent_jobs: 8        # bounded active provider jobs per Engine process
  drain_seconds: 30             # null: `serve` waits for running jobs without a bound
  repair_seconds: null          # default separate repair allowance
```

`engine.ledger.external_daily_spend = fn(day) -> float` adds spend recorded elsewhere to
the daily cap. Paid attempts store `input_tokens` and `output_tokens` in the attempts
table.

The unusable-price checks also run inside the attempt gate, so `engine.run_chain` and the
judge are covered, not only planned routes; a refused attempt sends nothing and records
`budget_blocked`.

There is one `Engine` per state directory per process: a second one would take the first's
running jobs for orphans at startup, so it raises `RuntimeError`. `engine.close()` (or
dropping the last reference) releases the directory. The state file carries a schema
version (`PRAGMA user_version`) and an older file is migrated in place when opened.

A reported cost counts only if it is a finite number that is zero or more; anything
else is treated as unknown, and an unknown cost commits the attempt's full allowance.
Every paid attempt that got a reply (including an invalid one) is committed, and an
attempt that may have been billed without a reply is `charge_unknown`. The ledger checks,
in the same transaction as the authorization, that the job is still running under the
caller's instance, so a job that was recovered cannot spend. `engine.run_chain` attempts
get unique ledger keys, so several calls for one request id never collide.

`engine.close(drain_seconds)` waits for running jobs and then kills any CLI process group
that is still running. `coldtake serve` calls it after SIGTERM too (SIGTERM is a graceful
stop with exit status 0, or 1 if the drain timed out; a second SIGTERM during the drain
ends the process).

CLI output is read as a stream with a size bound; a process over the bound is killed and
the attempt fails with `output_too_large`.

## 10. The stable surface and versioning

`coldtake.extension` is the one import for everything an extension needs, and
`coldtake.API_VERSION` (an integer, also in `coldtake.extension`) grows when an extension
may have to change; assert `API_VERSION >= n` for the version you wrote against.
`coldtake.__version__` is the package version. The names below are public and covered by
tests; the former private spellings stay as aliases.

| public | was | use |
|---|---|---|
| `engine.key(request_id)`, `engine.public_id(key)` | `_key`, `_public_id` | the record key of an id (run row, ledger, attempts) and back |
| `engine.plan(handler, packet, lead, prompt, sealed_bytes)` | `_plan` | the admitted plan, as `submit` computes it |
| `plan_view(plan)` | `_plan_view` | the JSON form stored as `plan_json` |
| `parse_attempt_id(id)`, `format_attempt_id(...)`, `AttemptId` | (string parsing) | read an attempt id: role, slot, token, seq, route index, attempt (0 first try, 1 repair). A role must not end in a digit |
| `split_ledger_key(attempt_key)` | | a ledger `attempt_key` as (request key, attempt id) |
| `ProcessGroup`, `live_process_group(process)` | `providers._Group` | a CLI process and its process group, plus shutdown-tracked context manager |
| `CLIProvider.communicate(group, payload, timeout, *, max_output_bytes=...)` | `_communicate` | feed a CLI and collect its output with the size and time bounds; override it to change collection (`_communicate` still calls through) |
| `AttemptContext` | | passed to `validate_memo` / `repair_prompt`: `role`, `slot`, `route`, `attempt_index`, `is_repair`, `prior_data`, `prior_raw`, `raw` |
| `Plan.extra` | | resolver metadata persisted in `plan_json` (`plan_view(plan)["extra"]`) |
| `api.set_instructions(text)` | (`mcp._mcp_server.instructions`) | server instructions |
| `JobFailure(status, reason, cause, failed_route_billing)` | | a terminal job failure raised by a contract or planner |

The former private spellings are only aliases that call the public ones. The engine and
`CLIProvider._run` call the public names, so overriding `_key`, `_public_id`, `_plan` or
`_communicate` in a subclass no longer changes anything: override `key`, `public_id`,
`plan` or `communicate` instead.

Status views and results carry `billing`, `charge_unknown` and, for a failed job,
`failed_route_billing`; see the design note on budget.
