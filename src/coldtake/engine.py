"""Job lifecycle: idempotent submission, concurrency bounds, event-driven wait, recovery.

Planning lives in `planner.py` and attempt/panel execution lives in `runner.py`. What a job
*means* (packet, prompt, memo schema, comparison, result shape) lives in a `Contract`; this
module owns the lifecycle and public Engine API.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
import weakref
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ValidationError

from .budget import Ledger
from .check import CheckContract
from .config import Config
from .contracts import Contract, CoreContract, RunView, contract_uses_judge
from .failures import ProviderFailure, redact
from .planner import (
    MEMO_ESTIMATE_BYTES,
    InvalidRequest,
    admit_plan,
    plan_view,
    price_problem,
    repair_bytes,
)
from .providers import Provider, Route, build_providers, terminate_live_processes
from .routing import Plan
from .runner import (
    PARTIAL_RAW_BYTES,
    AttemptId,
    Context,
    Job,
    JobFailure,
    Runner,
    format_attempt_id,
    job_failure_from_provider,
    normalize_failure,
    parse_attempt_id,
    split_ledger_key,
    summarize_error,
)
from .schemas import REQUEST_ID_PATTERN, Sealed, canonical_json, sha256_hex
from .store import TERMINAL, CollisionError, Store

INLINE_RESULT_BYTES = 64_000
POLL_SECONDS = 0.25

_RESUBMIT = "Resubmit under a new request_id if still needed."
RECOVERY = {
    "charge_unknown": (
        "A paid attempt may have been billed. Do not resubmit; its allowance stays held."
    ),
    "timed_out": f"A subscription attempt ran past its deadline. {_RESUBMIT}",
    "lost": f"The worker stopped before finishing. {_RESUBMIT}",
    "failed": "Terminal. Report the cause and return to the task.",
}

# Former private aliases kept for callers/tests that import them from coldtake.engine.
_plan_view = plan_view
_normalize = normalize_failure


class RecordSource(Protocol):
    """Read-only source of records core does not own (for example an older store).
    Consulted by status/result when core has no run for an id. Optional `claims(request_id,
    packet_hash)` lets a source distinguish an identical replay from a collision; otherwise
    `reserved(request_id)` rejects any reuse."""

    def status(self, request_id: str) -> dict[str, Any] | None: ...

    def result(self, request_id: str) -> dict[str, Any] | None: ...

    def reserved(self, request_id: str) -> bool: ...

    # Optional:
    # def claims(self, request_id: str, packet_hash: str) -> Literal["replay", "collision"] | None:


_ENGINES: dict[str, weakref.ref[Engine]] = {}
_ENGINES_LOCK = threading.Lock()


class Engine:
    """One engine per state directory per process. A second one would treat the first's
    running jobs as its own orphans at startup, so it is refused; `close()` releases the
    directory."""

    def __init__(
        self,
        config: Config,
        providers: Mapping[str, Provider] | None = None,
        store: Store | None = None,
    ):
        self.config = config
        self.providers = dict(providers) if providers is not None else build_providers(config)
        self.store = store or Store(config.resolved_state_dir())
        self._claim_state_dir()
        self.ledger = Ledger(self.store, config.budget.per_request_usd, config.budget.daily_usd)
        self.instance = uuid.uuid4().hex
        self.namespace = config.namespace
        self.accepting_new_jobs = config.accepting_new_jobs
        self.contracts: dict[str, Contract] = {}
        self.record_sources: list[RecordSource] = []
        self.register_contract(CoreContract(config))
        self.register_contract(CheckContract(config))
        self._runner = Runner(self.config, self.providers, self.store, self.ledger, self.instance)
        self._slots = threading.Semaphore(config.limits.max_concurrent_jobs)
        self._threads: dict[str, threading.Thread] = {}
        self._done: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self.started_at = self.store.clock()
        self.store.reconcile(startup_instance=self.instance, startup_time=self.started_at)

    def _claim_state_dir(self) -> None:
        key = str(self.store.root.resolve())
        with _ENGINES_LOCK:
            holder = _ENGINES.get(key)
            if holder is not None and holder() is not None:
                raise RuntimeError(
                    f"an Engine already exists for state directory {key!r} in this process"
                )
            _ENGINES[key] = weakref.ref(self)
        self._state_key = key

    def _release_state_dir(self) -> None:
        with _ENGINES_LOCK:
            holder = _ENGINES.get(self._state_key)
            if holder is not None and holder() in (self, None):
                del _ENGINES[self._state_key]

    def register_contract(self, contract: Contract) -> None:
        self.contracts[contract.name] = contract

    def register_record_source(self, source: RecordSource) -> None:
        self.record_sources.append(source)

    def key(self, request_id: str) -> str:
        return f"{self.namespace}:{request_id}" if self.namespace else request_id

    def public_id(self, key: str) -> str:
        return key.removeprefix(f"{self.namespace}:") if self.namespace else key

    def _key(self, request_id: str) -> str:  # former private names
        return self.key(request_id)

    def _public_id(self, key: str) -> str:
        return self.public_id(key)

    def has_run(self, request_id: str) -> bool:
        """True if core owns a stored run for `request_id` in this namespace."""
        return self.store.get_run(self.key(request_id)) is not None

    # -- submission --------------------------------------------------------------
    def _check_sources(self, request_id: str, submission_hash: str) -> dict[str, Any] | None:
        """Consult record sources: replay an identical legacy job, reject a collision/reserve."""
        for source in self.record_sources:
            claims = getattr(source, "claims", None)
            if callable(claims):
                verdict: Literal["replay", "collision"] | None = claims(request_id, submission_hash)
                if verdict == "replay":
                    found = source.status(request_id)
                    if found is not None:
                        return found
                elif verdict == "collision":
                    raise InvalidRequest("request_id was already used with a different packet")
                continue
            if source.reserved(request_id):
                raise InvalidRequest("request_id is reserved")
        return None

    def submit(
        self,
        request_id: str,
        data: dict[str, Any],
        *,
        lead_family: str,
        mode: str | None = None,
        contract: str = "coldtake_v1",
    ) -> dict[str, Any]:
        """Validate, plan, store and start a job; return its ACK."""
        if not re.fullmatch(REQUEST_ID_PATTERN, request_id):
            raise InvalidRequest("request_id must match " + REQUEST_ID_PATTERN)
        if contract not in self.contracts:
            raise InvalidRequest(f"unknown contract {contract!r}")
        handler = self.contracts[contract]
        key = self.key(request_id)

        # Side-effect-free replay check first (e.g. coldtake_check replays without re-reading
        # context_paths from disk, and RecordSource.claims can replay identical legacy packets).
        try:
            quick = handler.quick_identity(data, mode)
        except ValidationError as exc:
            raise InvalidRequest(summarize_error(exc)) from exc
        except ValueError as exc:
            raise InvalidRequest(redact(str(exc), 800)) from exc
        if quick is not None:
            quick_hash = sha256_hex(canonical_json(quick))
            quick_identity = sha256_hex(canonical_json({"contract": contract, "submission": quick}))
            replayed = self._check_sources(request_id, quick_hash)
            if replayed is not None:
                return replayed
            existing = self.store.get_run(key)
            if existing is not None:
                if existing["packet_hash"] != quick_identity:
                    raise InvalidRequest("request_id was already used with a different packet")
                return self.status(request_id)

        # For sources without `claims()`, enforce `reserved()` before parsing as before.
        if quick is None:
            for source in self.record_sources:
                if not callable(getattr(source, "claims", None)) and source.reserved(request_id):
                    raise InvalidRequest("request_id is reserved")

        try:
            parsed = handler.parse(data, mode)
        except ValidationError as exc:
            raise InvalidRequest(summarize_error(exc)) from exc
        except ValueError as exc:
            raise InvalidRequest(redact(str(exc), 800)) from exc

        if quick is None:
            replayed = self._check_sources(request_id, sha256_hex(canonical_json(parsed.identity)))
            if replayed is not None:
                return replayed

        prompt = handler.render_prompt(parsed.packet)
        if len(prompt.encode()) > self.config.limits.max_prompt_bytes:
            raise InvalidRequest("packet exceeds limits.max_prompt_bytes")
        identity = sha256_hex(canonical_json({"contract": contract, "submission": parsed.identity}))
        existing = self.store.get_run(key)
        if existing is not None:
            if existing["packet_hash"] != identity:
                raise InvalidRequest("request_id was already used with a different packet")
            return self.status(request_id)
        if not self.accepting_new_jobs:
            raise InvalidRequest("new jobs are not being accepted")

        plan = self.plan(
            handler, parsed.packet, lead_family, prompt, len(parsed.sealed.answer or "")
        )
        limits = self.config.limits
        uses_judge = contract_uses_judge(handler, parsed.packet, has_sealed=parsed.sealed.present)
        reserved = (
            self._runner.main_seconds(handler)
            + (self._runner.repair_seconds(handler) or 0)
            + (limits.judge_seconds if uses_judge else 0)
        )
        try:
            row, created = self.store.create_run(
                key,
                contract=contract,
                mode=handler.mode(parsed.packet),
                packet_hash=identity,
                packet_json=handler.dump_packet(parsed.packet),
                sealed_answer=parsed.sealed.answer,
                sealed_choice=parsed.sealed.choice,
                sealed_sha256=parsed.sealed.sha256(),
                lead_family=lead_family,
                warnings=parsed.warnings,
                plan_json=json.dumps(plan_view(plan)),
                instance=self.instance,
                lease_seconds=limits.lease_seconds,
                job_seconds=reserved,
            )
        except CollisionError as exc:
            raise InvalidRequest(str(exc)) from exc
        if created:
            self._start(row, handler, plan)
        return self.status(request_id)

    def _start(self, row: dict[str, Any], handler: Contract, plan: Plan) -> None:
        job = Job(
            request_id=row["request_id"],
            contract=handler,
            instance=self.instance,
            plan=plan,
            packet=handler.load_packet(row["packet_json"]),
            sealed=Sealed(answer=row["sealed_answer"], choice=row["sealed_choice"]),
            lead=row["lead_family"],
            advisor_deadline=row["created_at"] + self._runner.main_seconds(handler),
        )
        done = threading.Event()
        thread = threading.Thread(target=self._worker, args=(job, done), daemon=True)
        with self._lock:
            self._threads[job.request_id] = thread
            self._done[job.request_id] = done
        thread.start()

    def drain(self, timeout: float | None = None) -> bool:
        with self._lock:
            threads = list(self._threads.values())
        end = None if timeout is None else time.monotonic() + timeout
        for thread in threads:
            thread.join(None if end is None else max(0.0, end - time.monotonic()))
        return not any(t.is_alive() for t in threads)

    # -- planning ----------------------------------------------------------------
    def _plan(self, *args: Any, **kwargs: Any) -> Plan:
        return self.plan(*args, **kwargs)

    def _repair_bytes(
        self, handler: Contract, prompt: str, route: Route, kind: str = "memo"
    ) -> int:
        return repair_bytes(handler, prompt, route, kind)

    def _price_problem(self, route: Route, prompt_bytes: int) -> str | None:
        return price_problem(self.providers, route, prompt_bytes)

    def _execute(self, job: Job) -> dict[str, Any]:
        return self._runner.execute(job)

    def plan(
        self, handler: Contract, packet: BaseModel, lead: str, prompt: str, sealed_bytes: int = 0
    ) -> Plan:
        return admit_plan(
            self.config, self.providers, self.namespace, handler, packet, lead, prompt, sealed_bytes
        )

    # -- worker ------------------------------------------------------------------
    def _worker(self, job: Job, done: threading.Event) -> None:
        lease = self.config.limits.lease_seconds
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(max(1.0, lease / 3)):
                try:
                    renewed = self.store.heartbeat(job.request_id, self.instance, lease)
                except Exception:  # noqa: BLE001 - unrenewable lease means stop spending
                    renewed = False
                if not renewed:
                    job.cancel.set()
                    return

        threading.Thread(target=beat, daemon=True).start()
        acquired = False
        try:
            while not acquired:
                if job.cancel.is_set():
                    raise JobFailure(
                        "failed", "cancelled", {"type": "Cancelled", "message": "lease lost"}
                    )
                acquired = self._slots.acquire(timeout=0.25)
            self._finish_complete(job, self._execute(job))
        except ProviderFailure as failure:
            self._fail(job, job_failure_from_provider(failure))
        except JobFailure as failure:
            self._fail(job, failure)
        except Exception as exc:  # noqa: BLE001 - every failure must leave a stored cause
            self._fail(
                job,
                JobFailure(
                    "failed", "internal_error",
                    {"type": type(exc).__name__, "message": redact(str(exc))},
                ),
            )  # fmt: skip
        finally:
            if acquired:
                self._slots.release()
            stop.set()
            done.set()
            with self._lock:
                self._threads.pop(job.request_id, None)
                self._done.pop(job.request_id, None)

    def _fail(self, job: Job, failed: JobFailure) -> None:
        self.store.finish(
            job.request_id, self.instance, failed.status, reason=failed.reason,
            cause=failed.cause, failed_route_billing=failed.failed_route_billing,
            charge_precedence=True,
        )  # fmt: skip

    def run_chain(
        self,
        request_id: str,
        routes: tuple[Route, ...] | list[Route],
        prompt: str,
        schema: dict[str, Any],
        validate: Any,
        *,
        role: str = "extension",
        slot: int = 0,
        timeout: float | None = None,
    ) -> tuple[Any, Route]:
        """Extension entry point: run a prompt down a route ladder without a job."""
        seconds = float(timeout or self.config.limits.attempt_seconds)
        return self._runner.chain(
            Context(self.key(request_id)), role, slot, tuple(routes), prompt, schema,
            lambda data, _ctx: validate(data), seconds, self.store.clock() + seconds * 2,
        )  # fmt: skip

    # -- completion --------------------------------------------------------------
    def _finish_complete(self, job: Job, outcome: dict[str, Any]) -> None:
        handler = job.contract
        assert handler is not None  # noqa: S101
        self.ledger.hold_unfinalized(job.request_id)
        row = self.store.get_run(job.request_id) or {}
        billing = self.ledger.request_state(job.request_id)
        base = {
            "request_id": self.public_id(job.request_id),
            "mode": handler.mode(job.packet),
            "status": "complete",
            "contract": handler.name,
            "lead_family": job.lead,
            "advisor_count": len(outcome["memos"]),
            "memos": outcome["memos"],
            "comparison": outcome["comparison"],
            "sealed_sha256": row.get("sealed_sha256"),
            "warnings": json.loads(row.get("warnings_json") or "[]"),
            "paid": self.ledger.request_totals(job.request_id),
            "billing": "paid" if billing["paid"] else "subscription",
            "charge_unknown": billing["charge_unknown"],
        }
        view = self._run_view(row, job.packet, base)
        text = canonical_json(handler.shape_result(view))
        sha = sha256_hex(text)
        path: str | None = None
        inline: str | None = text
        limit = handler.inline_result_bytes or INLINE_RESULT_BYTES
        if len(text.encode()) > limit:
            path, inline = str(self.store.write_result_file(job.request_id, text)), None
        self.store.finish(
            job.request_id, self.instance, "complete", comparison=outcome["comparison"],
            result_json=inline, result_path=path, result_sha256=sha,
        )  # fmt: skip

    def _run_view(self, row: dict[str, Any], packet: BaseModel, base: dict[str, Any]) -> RunView:
        with self.store.connect() as db:
            attempts = [
                dict(a)
                for a in db.execute(
                    "SELECT * FROM attempts WHERE request_id=? ORDER BY id", (row["request_id"],)
                )
            ]
        return RunView(
            request_id=self.public_id(row["request_id"]),
            contract=row["contract"],
            mode=row["mode"],
            status=row["status"],
            lead_family=row["lead_family"],
            packet=packet,
            memos=json.loads(row["memos_json"]),
            comparison=base.get("comparison"),
            sealed_sha256=row["sealed_sha256"],
            warnings=json.loads(row["warnings_json"]),
            paid=self.ledger.request_totals(row["request_id"]),
            attempts=attempts,
            row=row,
            base_result=base,
            plan=json.loads(row["plan_json"]) if row["plan_json"] else None,
            progress=json.loads(row["progress_json"]),
        )

    def run_view(self, request_id: str, *, include_result: bool = True) -> RunView | None:
        """Row, memos, attempts and ledger totals of a run, or None if core has no such run.
        Pass `include_result=False` to avoid loading large complete result files on status polls."""
        row = self.store.get_run(self.key(request_id))
        if row is None:
            return None
        handler = self.contracts[row["contract"]]
        base: dict[str, Any] = {}
        if include_result and (row["result_json"] or row["result_path"]):
            base = self.load_result(request_id)
        else:
            base["comparison"] = json.loads(row["comparison_json"] or "null")
        return self._run_view(row, handler.load_packet(row["packet_json"]), base)

    # -- reading -----------------------------------------------------------------
    def _row(self, request_id: str) -> dict[str, Any] | None:
        key = self.key(request_id)
        self.store.reconcile(key)
        return self.store.get_run(key)

    def _view(self, request_id: str, row: dict[str, Any]) -> dict[str, Any]:
        view: dict[str, Any] = {
            "request_id": request_id,
            "mode": row["mode"],
            "contract": row["contract"],
            "status": row["status"],
            "lead_family": row["lead_family"],
            "sealed_sha256": row["sealed_sha256"],
            "warnings": json.loads(row["warnings_json"]),
            "advisors_done": len(json.loads(row["memos_json"])),
            "partial_advice_available": bool(row["partial_json"]),
        }
        billing = self.ledger.request_state(row["request_id"])
        view["billing"] = "paid" if billing["paid"] else "subscription"
        view["charge_unknown"] = billing["charge_unknown"]
        status = row["status"]
        view["next_action"] = {
            "running": "poll_status_same_request_id",
            "complete": "get_result_same_request_id",
        }.get(status, "stop_and_report")
        if status == "running":
            view["progress"] = json.loads(row["progress_json"])
        if status not in {"running", "complete"}:
            view["reason"] = row["reason"]
            view["cause"] = {"type": row["cause_type"], "message": row["cause_message"]}
            view["recovery"] = RECOVERY[status]
            view["failed_route_billing"] = row.get("failed_route_billing")
        return view

    def _shape(self, row: dict[str, Any], view: dict[str, Any]) -> dict[str, Any]:
        handler = self.contracts.get(row["contract"])
        return handler.shape_status(row, view) if handler else view

    def _legacy(self, request_id: str, method: str) -> dict[str, Any] | None:
        for source in self.record_sources:
            found = getattr(source, method)(request_id)
            if found is not None:
                return found
        return None

    def status(self, request_id: str) -> dict[str, Any]:
        row = self._row(request_id)
        if row is None:
            return self._legacy(request_id, "status") or {
                "request_id": request_id,
                "status": "not_found",
                "next_action": "submit_once",
            }
        return self._shape(row, self._view(request_id, row))

    def result(self, request_id: str) -> dict[str, Any]:
        row = self._row(request_id)
        if row is None:
            return self._legacy(request_id, "result") or {
                "request_id": request_id,
                "status": "not_found",
                "next_action": "submit_once",
            }
        view = self._view(request_id, row)
        if row["status"] == "complete":
            if row["result_path"]:
                view.update(
                    result_path=row["result_path"],
                    result_sha256=row["result_sha256"],
                    result_bytes=Path(row["result_path"]).stat().st_size,
                    note="result exceeds the inline limit; use load_result or read the file",
                )
            else:
                view["result"] = json.loads(row["result_json"])
        elif row["status"] != "running":
            view["partial_memos"] = json.loads(row["memos_json"])
            if row["partial_json"]:
                view["partial_advice"] = json.loads(row["partial_json"])
        return self._shape(row, view)

    def load_result(self, request_id: str) -> dict[str, Any]:
        row = self.store.get_run(self.key(request_id))
        if row is None or row["status"] != "complete":
            raise LookupError("no complete result for this request_id")
        text = (
            Path(row["result_path"]).read_text(encoding="utf-8")
            if row["result_path"]
            else row["result_json"]
        )
        if sha256_hex(text) != row["result_sha256"]:
            raise ValueError("stored result does not match its recorded SHA-256")
        return json.loads(text)

    def wait(self, request_id: str, timeout: float | None) -> dict[str, Any]:
        """Wait until the stored job is terminal or `timeout` passes (None: until terminal).
        Uses the local worker's completion Event when owned by this process (no busy polling),
        falling back to polling only for cross-process runs."""
        key = self.key(request_id)
        end = None if timeout is None else time.monotonic() + timeout
        while True:
            row = self._row(request_id)
            if row is None or row["status"] in TERMINAL:
                return self.result(request_id)
            remaining = None if end is None else max(0.0, end - time.monotonic())
            if remaining is not None and remaining <= 0:
                return self.result(request_id)
            with self._lock:
                done = self._done.get(key)
            if done is not None:
                done.wait(timeout=remaining)
            else:
                time.sleep(POLL_SECONDS if remaining is None else min(POLL_SECONDS, remaining))

    def close(self, drain_seconds: float | None = None) -> bool:
        drained = self.drain(drain_seconds)
        terminate_live_processes()
        self._release_state_dir()
        return drained


__all__ = [
    "INLINE_RESULT_BYTES",
    "MEMO_ESTIMATE_BYTES",
    "PARTIAL_RAW_BYTES",
    "POLL_SECONDS",
    "RECOVERY",
    "AttemptId",
    "Context",
    "Engine",
    "InvalidRequest",
    "Job",
    "JobFailure",
    "RecordSource",
    "format_attempt_id",
    "parse_attempt_id",
    "plan_view",
    "split_ledger_key",
]
