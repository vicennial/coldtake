"""The public surface for extensions, in one import. See docs/extending.md."""

from __future__ import annotations

from . import API_VERSION, __version__
from .budget import BudgetExceeded, Ledger
from .check import (
    CONTRACT_CHECK,
    CheckContract,
    StoredCheckPacket,
    allowed_context_path,
    extract_json_object_strict,
    load_context_files,
    render_check_prompt,
)
from .contracts import (
    AttemptContext,
    CompareContext,
    Contract,
    CoreContract,
    JudgeRunner,
    Parsed,
    RunView,
)
from .engine import (
    AttemptId,
    Engine,
    InvalidRequest,
    JobFailure,
    RecordSource,
    format_attempt_id,
    parse_attempt_id,
    plan_view,
    split_ledger_key,
)
from .failures import ProviderFailure, classify
from .providers import (
    CLIProvider,
    Completion,
    GeminiProvider,
    ProcessGroup,
    Provider,
    Route,
    live_process_group,
    worst_case_cost,
)
from .routing import Plan, RouteContext, detect_lead_family
from .schemas import (
    BlindPacket,
    CheckAttempt,
    CheckContextFile,
    CheckOutput,
    CheckPacket,
    CheckResult,
    ReviewPacket,
    Submission,
)
from .server import ExtensionAPI
from .store import Store

__all__ = [
    "API_VERSION",
    "CONTRACT_CHECK",
    "AttemptContext",
    "AttemptId",
    "BlindPacket",
    "BudgetExceeded",
    "CLIProvider",
    "CheckAttempt",
    "CheckContextFile",
    "CheckContract",
    "CheckOutput",
    "CheckPacket",
    "CheckResult",
    "CompareContext",
    "Completion",
    "Contract",
    "CoreContract",
    "Engine",
    "ExtensionAPI",
    "GeminiProvider",
    "InvalidRequest",
    "JobFailure",
    "JudgeRunner",
    "Ledger",
    "Parsed",
    "Plan",
    "ProcessGroup",
    "Provider",
    "ProviderFailure",
    "RecordSource",
    "ReviewPacket",
    "Route",
    "RouteContext",
    "RunView",
    "Store",
    "StoredCheckPacket",
    "Submission",
    "__version__",
    "allowed_context_path",
    "classify",
    "detect_lead_family",
    "extract_json_object_strict",
    "format_attempt_id",
    "live_process_group",
    "load_context_files",
    "parse_attempt_id",
    "plan_view",
    "render_check_prompt",
    "split_ledger_key",
    "worst_case_cost",
]
