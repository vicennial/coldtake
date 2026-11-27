"""coldtake: a blind second opinion from the other model family."""

__version__ = "0.1.0"

# The extension API (coldtake.extension and docs/extending.md). It is an integer that grows
# when an extension may have to change: an extension can assert `API_VERSION >= n` for the
# first version it was written against.
# 1: initial versioned extension API
# 2: AttemptContext, Contract.uses_judge(packet, ...), max_repairs, attempt_seconds(packet),
#    CheckContract, live_process_group, Ledger.attempts, Engine.has_run, Plan.extra,
#    extensions_config, RecordSource.claims, typed MCP schemas
API_VERSION = 2
