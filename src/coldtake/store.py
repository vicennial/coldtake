"""Run state in one private SQLite file: runs, attempts, sealed relations, budget ledger.

SQLite gives us atomic cross-thread and cross-process transactions (its write lock is a
file lock), which is what idempotent submission, the lease and the budget ledger need.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

RECORD_VERSION = 1
# PRAGMA user_version; 2 added runs.plan_json and runs.progress_json, 3 runs.failed_route_billing
SCHEMA_VERSION = 3
_ADDED_COLUMNS = (
    ("plan_json", "TEXT"),
    ("progress_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("failed_route_billing", "TEXT"),
)
DEADLINE_GRACE_SECONDS = 30
TERMINAL = {"complete", "failed", "timed_out", "lost", "charge_unknown"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  request_id TEXT PRIMARY KEY,
  record_version INTEGER NOT NULL DEFAULT 1,
  contract TEXT NOT NULL DEFAULT 'coldtake_v1',
  mode TEXT NOT NULL,
  status TEXT NOT NULL,
  reason TEXT, cause_type TEXT, cause_message TEXT,
  packet_hash TEXT NOT NULL,
  packet_json TEXT NOT NULL,
  sealed_answer TEXT, sealed_choice TEXT, sealed_sha256 TEXT,
  lead_family TEXT,
  warnings_json TEXT NOT NULL DEFAULT '[]',
  created_at REAL NOT NULL, updated_at REAL NOT NULL, finished_at REAL,
  deadline_at REAL NOT NULL,
  owner_instance TEXT, owner_pid INTEGER, heartbeat_at REAL, lease_expires_at REAL,
  memos_json TEXT NOT NULL DEFAULT '[]',
  partial_json TEXT,
  plan_json TEXT,
  progress_json TEXT NOT NULL DEFAULT '{}',
  failed_route_billing TEXT,
  comparison_json TEXT,
  result_json TEXT, result_path TEXT, result_sha256 TEXT
);
CREATE INDEX IF NOT EXISTS runs_status ON runs(status);
CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL,
  role TEXT NOT NULL,
  slot INTEGER NOT NULL,
  attempt INTEGER NOT NULL,
  provider TEXT NOT NULL, model TEXT, family TEXT NOT NULL,
  billing TEXT NOT NULL CHECK (billing IN ('subscription', 'paid')),
  outcome TEXT NOT NULL,
  reason TEXT, cause_type TEXT, cause_message TEXT,
  started_at REAL NOT NULL, seconds REAL NOT NULL,
  cost_usd REAL,
  input_tokens INTEGER, output_tokens INTEGER,
  fallback INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS attempts_request ON attempts(request_id);
CREATE TABLE IF NOT EXISTS sealed_relations (
  request_id TEXT NOT NULL, mode TEXT NOT NULL, memo_id TEXT NOT NULL,
  family TEXT NOT NULL, relation TEXT NOT NULL, method TEXT NOT NULL,
  PRIMARY KEY (request_id, memo_id)
);
CREATE TABLE IF NOT EXISTS ledger (
  attempt_key TEXT PRIMARY KEY,
  request_id TEXT NOT NULL, day TEXT NOT NULL, model TEXT NOT NULL,
  amount REAL NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('authorized', 'committed', 'charge_unknown', 'released')),
  actual_cost REAL,
  authorized_at REAL NOT NULL, finalized_at REAL
);
"""


def apply_charge_precedence(db: sqlite3.Connection, request_id: str, now: float) -> bool:
    """The one place that decides whether a run's paid attempts outrank its other causes.
    Inside the caller's transaction: every attempt still `authorized` (nobody saw its
    outcome) is held as `charge_unknown`, and the result is True when the run has any
    `charge_unknown` row, whether it was just held or finalized earlier. A run that returns
    True must end `charge_unknown`, whatever else went wrong. Used by recovery and by the
    engine's failure path."""
    db.execute(
        "UPDATE ledger SET status='charge_unknown', finalized_at=?"
        " WHERE request_id=? AND status='authorized'",
        (now, request_id),
    )
    found = db.execute(
        "SELECT 1 FROM ledger WHERE request_id=? AND status='charge_unknown' LIMIT 1",
        (request_id,),
    ).fetchone()
    return found is not None


class CollisionError(RuntimeError):
    """The same request id was submitted with a different packet."""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Store:
    def __init__(self, state_dir: Path, clock: Callable[[], float] = time.time):
        self.root = Path(state_dir)
        self.clock = clock
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        (self.root / "results").mkdir(mode=0o700, exist_ok=True)
        self.db_path = self.root / "state.db"
        previous = os.umask(0o077)
        try:
            with self.connect() as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.executescript(_SCHEMA)
                self._migrate(db)
        finally:
            os.umask(previous)

    @staticmethod
    def _migrate(db: sqlite3.Connection) -> None:
        """Bring an older state file up to SCHEMA_VERSION (idempotent; safe when several
        processes start at once, because the check is repeated inside the write lock)."""
        if db.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
            return
        db.execute("BEGIN IMMEDIATE")
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version < SCHEMA_VERSION:
                have = {row["name"] for row in db.execute("PRAGMA table_info(runs)")}
                for name, ddl in _ADDED_COLUMNS:
                    if name not in have:
                        db.execute(f"ALTER TABLE runs ADD COLUMN {name} {ddl}")  # noqa: S608
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")

    @contextlib.contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

    # -- runs ------------------------------------------------------------------
    def create_run(
        self,
        request_id: str,
        *,
        contract: str = "coldtake_v1",
        mode: str,
        packet_hash: str,
        packet_json: str,
        sealed_answer: str | None,
        sealed_choice: str | None,
        sealed_sha256: str | None,
        lead_family: str,
        warnings: list[str],
        plan_json: str | None = None,
        instance: str,
        lease_seconds: float,
        job_seconds: float,
    ) -> tuple[dict[str, Any], bool]:
        """Insert a new running job, or return the existing one for the same packet."""
        now = self.clock()
        with self.transaction() as db:
            row = db.execute("SELECT * FROM runs WHERE request_id=?", (request_id,)).fetchone()
            if row is not None:
                if row["packet_hash"] != packet_hash:
                    raise CollisionError("request_id was already used with a different packet")
                return dict(row), False
            db.execute(
                "INSERT INTO runs (request_id, record_version, contract, mode, status,"
                " packet_hash, packet_json, sealed_answer, sealed_choice, sealed_sha256,"
                " lead_family,"
                " warnings_json, plan_json, created_at, updated_at, deadline_at, owner_instance,"
                " owner_pid, heartbeat_at, lease_expires_at)"
                " VALUES (?,?,?,?,'running',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    request_id, RECORD_VERSION, contract, mode, packet_hash, packet_json,
                    sealed_answer,
                    sealed_choice, sealed_sha256, lead_family, json.dumps(warnings), plan_json, now,
                    now,
                    now + job_seconds, instance, os.getpid(), now, now + lease_seconds,
                ),
            )  # fmt: skip
            return dict(
                db.execute("SELECT * FROM runs WHERE request_id=?", (request_id,)).fetchone()
            ), True

    def get_run(self, request_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE request_id=?", (request_id,)).fetchone()
        return dict(row) if row else None

    def heartbeat(self, request_id: str, instance: str, lease_seconds: float) -> bool:
        """Renew the lease. False means this worker no longer owns a running job."""
        now = self.clock()
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE runs SET heartbeat_at=?, lease_expires_at=?, updated_at=?"
                " WHERE request_id=? AND status='running' AND owner_instance=?",
                (now, now + lease_seconds, now, request_id, instance),
            )
        return cursor.rowcount == 1

    def append_memo(self, request_id: str, instance: str, memo: dict[str, Any]) -> bool:
        """Append a final memo. Memos are immutable once stored."""
        with self.transaction() as db:
            row = db.execute(
                "SELECT memos_json FROM runs WHERE request_id=? AND status='running'"
                " AND owner_instance=?",
                (request_id, instance),
            ).fetchone()
            if row is None:
                return False
            memos = json.loads(row["memos_json"])
            memos.append(memo)
            db.execute(
                "UPDATE runs SET memos_json=?, updated_at=? WHERE request_id=?",
                (json.dumps(memos), self.clock(), request_id),
            )
        return True

    def set_partial(self, request_id: str, partial: dict[str, Any]) -> None:
        """Keep the last invalid candidate of a running job as labelled partial advice."""
        with self.connect() as db:
            db.execute(
                "UPDATE runs SET partial_json=? WHERE request_id=? AND status='running'",
                (json.dumps(partial), request_id),
            )

    def set_progress(self, request_id: str, key: str, state: dict[str, Any] | None) -> None:
        """In-progress attempt state per branch ("advisor1", "judge0", ...), for status
        shapers: phase, route_index, attempt_index, model, dispatched. None clears a branch."""
        with self.transaction() as db:
            row = db.execute(
                "SELECT progress_json FROM runs WHERE request_id=? AND status='running'",
                (request_id,),
            ).fetchone()
            if row is None:
                return
            progress = json.loads(row["progress_json"])
            if state is None:
                progress.pop(key, None)
            else:
                progress[key] = state
            db.execute(
                "UPDATE runs SET progress_json=? WHERE request_id=?",
                (json.dumps(progress), request_id),
            )

    def extend_deadline(self, request_id: str, instance: str, deadline_at: float) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE runs SET deadline_at=MAX(deadline_at, ?) WHERE request_id=?"
                " AND status='running' AND owner_instance=?",
                (deadline_at, request_id, instance),
            )

    def finish(
        self,
        request_id: str,
        instance: str,
        status: str,
        *,
        reason: str | None = None,
        cause: dict[str, str] | None = None,
        failed_route_billing: str | None = None,
        comparison: dict[str, Any] | None = None,
        result_json: str | None = None,
        result_path: str | None = None,
        result_sha256: str | None = None,
        charge_precedence: bool = False,
    ) -> bool:
        """Terminal transition, fenced by owner so a late worker cannot overwrite a recovery.
        With `charge_precedence` a run that has any `charge_unknown` (or still `authorized`)
        paid attempt ends `charge_unknown` instead of `status`, decided in the same
        transaction as the transition (see `apply_charge_precedence`)."""
        now = self.clock()
        cause = cause or {}
        with self.transaction() as db:
            if charge_precedence:
                owned = db.execute(
                    "SELECT 1 FROM runs WHERE request_id=? AND status='running'"
                    " AND owner_instance=?",
                    (request_id, instance),
                ).fetchone()
                if owned is not None and apply_charge_precedence(db, request_id, now):
                    status = "charge_unknown"
            cursor = db.execute(
                "UPDATE runs SET status=?, reason=?, cause_type=?, cause_message=?,"
                " comparison_json=?, result_json=?, result_path=?, result_sha256=?,"
                " failed_route_billing=?, finished_at=?, updated_at=?, lease_expires_at=NULL"
                " WHERE request_id=? AND status='running' AND owner_instance=?",
                (
                    status, reason, cause.get("type"), cause.get("message"),
                    json.dumps(comparison) if comparison is not None else None,
                    result_json, result_path, result_sha256, failed_route_billing, now, now,
                    request_id, instance,
                ),
            )  # fmt: skip
        return cursor.rowcount == 1

    def record_attempt(self, request_id: str, **fields: Any) -> None:
        columns = ", ".join(["request_id", *fields])
        marks = ", ".join("?" * (len(fields) + 1))
        with self.connect() as db:
            db.execute(
                f"INSERT INTO attempts ({columns}) VALUES ({marks})",  # noqa: S608 - fixed keys
                (request_id, *fields.values()),
            )

    def record_sealed_relations(
        self, request_id: str, mode: str, method: str, rows: list[tuple[str, str, str]]
    ) -> None:
        with self.connect() as db:
            db.executemany(
                "INSERT OR REPLACE INTO sealed_relations VALUES (?,?,?,?,?,?)",
                [(request_id, mode, memo_id, family, rel, method) for memo_id, family, rel in rows],
            )

    def write_result_file(self, request_id: str, text: str) -> Path:
        path = self.root / "results" / f"{request_id.replace(':', '__')}.json"
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
        return path

    # -- recovery --------------------------------------------------------------
    def reconcile(
        self,
        request_id: str | None = None,
        *,
        startup_instance: str | None = None,
        startup_time: float | None = None,
    ) -> list[str]:
        """Make stale `running` jobs terminal. Safe to call on every read and at startup.

        A job is stale when its lease expired, its owner process is gone, or it ran past
        its deadline. `startup_instance` additionally claims jobs that an OTHER instance in
        this same process created before `startup_time` (older instances only: a job a
        different live instance started later is left alone). The cause is `charge_unknown`
        only when a paid attempt was authorized and never finalized; otherwise `timed_out`
        or `lost`.
        """
        now = self.clock()
        with self.connect() as db:
            if request_id is None:
                rows = db.execute("SELECT * FROM runs WHERE status='running'").fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM runs WHERE status='running' AND request_id=?", (request_id,)
                ).fetchall()
        recovered = []
        for row in rows:
            expired = row["lease_expires_at"] is None or row["lease_expires_at"] < now
            late = now > row["deadline_at"] + DEADLINE_GRACE_SECONDS
            orphaned = row["owner_pid"] is not None and (
                not _pid_alive(row["owner_pid"])
                or (startup_instance and row["owner_instance"] != startup_instance
                    and row["owner_pid"] == os.getpid()
                    and row["created_at"] <= (startup_time if startup_time is not None else now))
            )  # fmt: skip
            if not (expired or late or orphaned):
                continue
            if self._recover(row, "timed_out" if late and not expired else "lost", now):
                recovered.append(row["request_id"])
        return recovered

    def _recover(self, row: sqlite3.Row, base_status: str, now: float) -> bool:
        with self.transaction() as db:
            current = db.execute(
                "SELECT status, owner_instance FROM runs WHERE request_id=?", (row["request_id"],)
            ).fetchone()
            if current is None or current["status"] != "running":
                return False
            if current["owner_instance"] != row["owner_instance"]:
                return False  # re-claimed since we looked
            ambiguous = apply_charge_precedence(db, row["request_id"], now)
            status = "charge_unknown" if ambiguous else base_status
            why = {
                "lost": "the worker stopped renewing its lease or its process exited",
                "timed_out": "the job ran past its deadline",
                "charge_unknown": "a paid attempt may have been sent before the job stopped",
            }
            db.execute(
                "UPDATE runs SET status=?, reason=?, cause_type=?, cause_message=?,"
                " finished_at=?, updated_at=?, lease_expires_at=NULL WHERE request_id=?",
                (
                    status, status, "StaleJob",
                    f"{why[status]} (was {base_status})" if ambiguous else why[status],
                    now, now, row["request_id"],
                ),
            )  # fmt: skip
        return True
