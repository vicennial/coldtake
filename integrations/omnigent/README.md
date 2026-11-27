# coldtake-omnigent

Omnigent Extension API v1 package contributing a **Coldtake Scorecard** sidebar page to Omnigent.

## Design (conflict-free with `polly` & `debby`)

- **Zero overlap with `polly`:** `coldtake` runs as a read-only MCP tool inside single-harness sessions (and optionally at `polly`'s plan gate before worktree fan-out). It never invokes `sys_session_send` / `sys_session_create` and never conflicts with `polly`'s `spawn_bounds` or post-diff `cross-review`.
- **Zero overlap with `debby`:** `debby` is an intentional two-headed brainstorming agent; `coldtake` adds blind checks and conflict telemetry to regular coding sessions.
- **No extra HTTP API on `coldtake`:** Respects Omnigent's strict extension iframe CSP (`connect-src 'none'`). Uses only declared Omnigent Extension API v1 host RPC capabilities (`navigation`, `sessions.read`, `storage.user`, `theme`).

## Conformance & Installation

Validate against Omnigent's own conformance checker:

```python
from pathlib import Path
from omnigent.extensions import check_extension_package
from coldtake_omnigent import manifest

root = Path("integrations/omnigent")
check_extension_package(
    manifest(),
    package_root=root / "src" / "coldtake_omnigent",
    project_root=root,
)
```

Install into an Omnigent environment:

```bash
uv pip install ./integrations/omnigent
```
