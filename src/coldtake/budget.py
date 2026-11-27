"""Atomic UTC-day ledger for paid attempts. Fails closed: no authorization, no spawn."""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime

from .store import Store


class BudgetExceeded(RuntimeError):
    """A paid attempt's worst case would exceed a cap; nothing was sent."""


class LeaseLost(RuntimeError):
    """The job no longer owns a running record (it was recovered or finished)."""


def clean_cost(value: object) -> float | None:
    """A reported cost is usable only if it is a finite number >= 0; anything else is
    unknown, and an unknown cost commits the attempt's full allowance."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


class Ledger:
    """One row per paid attempt. `authorized`, `committed` and `charge_unknown` rows all
    count against the caps (a charge_unknown attempt keeps its full allowance);
    `released` rows count for nothing because the request provably never ran."""

    def __init__(self, store: Store, per_request_usd: float, daily_usd: float):
        self.store = store
        self.per_request = per_request_usd
        self.daily = daily_usd
        # Optional hook: USD already spent today in another ledger (for example the
        # record of a system this one replaces). Counted against the daily cap.
        self.external_daily_spend: Callable[[str], float] | None = None

    def _external(self, day: str) -> float:
        return float(self.external_daily_spend(day)) if self.external_daily_spend else 0.0

    def _day(self) -> str:
        return datetime.fromtimestamp(self.store.clock(), UTC).date().isoformat()

    def authorize(
        self,
        request_id: str,
        attempt_id: str,
        model: str,
        amount: float,
        *,
        instance: str | None = None,
    ) -> None:
        """Authorize one paid attempt. With `instance`, the same transaction also checks that
        the job is still running under that owner, so a recovered job cannot spend."""
        if not math.isfinite(amount) or not 0 < amount <= self.per_request:
            raise BudgetExceeded("worst-case attempt cost exceeds the per-request cap")
        key, day = f"{request_id}:{attempt_id}", self._day()
        with self.store.transaction() as db:
            if instance is not None:
                run = db.execute(
                    "SELECT status, owner_instance FROM runs WHERE request_id=?", (request_id,)
                ).fetchone()
                if run is None or run["status"] != "running" or run["owner_instance"] != instance:
                    raise LeaseLost("the job no longer owns a running record")
            existing = db.execute(
                "SELECT status FROM ledger WHERE attempt_key=?", (key,)
            ).fetchone()
            if existing is not None and existing["status"] != "released":
                raise BudgetExceeded("this paid attempt was already authorized")
            spent = "status IN ('authorized','committed','charge_unknown')"
            request_total = db.execute(
                f"SELECT COALESCE(SUM(COALESCE(actual_cost, amount)), 0) FROM ledger"  # noqa: S608
                f" WHERE request_id=? AND {spent}",
                (request_id,),
            ).fetchone()[0]
            day_total = db.execute(
                f"SELECT COALESCE(SUM(COALESCE(actual_cost, amount)), 0) FROM ledger"  # noqa: S608
                f" WHERE day=? AND {spent}",
                (day,),
            ).fetchone()[0]
            if request_total + amount > self.per_request:
                raise BudgetExceeded("request would exceed the per-request cap")
            if day_total + self._external(day) + amount > self.daily:
                raise BudgetExceeded("request would exceed the daily cap")
            db.execute(
                "INSERT OR REPLACE INTO ledger (attempt_key, request_id, day, model, amount,"
                " status, authorized_at) VALUES (?,?,?,?,?,'authorized',?)",
                (key, request_id, day, model, amount, self.store.clock()),
            )

    def finalize(
        self,
        request_id: str,
        attempt_id: str,
        status: str,
        actual_cost: float | None = None,
    ) -> None:
        """status: committed (cost known or bounded), charge_unknown, or released."""
        if status not in {"committed", "charge_unknown", "released"}:
            raise ValueError("invalid ledger status")
        actual_cost = clean_cost(actual_cost)
        with self.store.transaction() as db:
            db.execute(
                "UPDATE ledger SET status=?, actual_cost=?, finalized_at=?"
                " WHERE attempt_key=? AND status='authorized'",
                (status, actual_cost, self.store.clock(), f"{request_id}:{attempt_id}"),
            )

    def spent_today(self) -> float:
        with self.store.connect() as db:
            return (
                self._external(self._day())
                + db.execute(
                    "SELECT COALESCE(SUM(COALESCE(actual_cost, amount)), 0) FROM ledger"
                    " WHERE day=? AND status IN ('authorized','committed','charge_unknown')",
                    (self._day(),),
                ).fetchone()[0]
            )

    def request_totals(self, request_id: str) -> dict[str, float]:
        """Committed spend and the allowance held by charge_unknown attempts."""
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT status, COALESCE(SUM(COALESCE(actual_cost, amount)), 0) AS usd"
                " FROM ledger WHERE request_id=? GROUP BY status",
                (request_id,),
            ).fetchall()
        usd = {row["status"]: row["usd"] for row in rows}
        return {
            "committed_usd": usd.get("committed", 0.0),
            "charge_unknown_held_usd": usd.get("charge_unknown", 0.0),
        }

    def request_state(self, request_id: str) -> dict[str, bool]:
        """Billing facts of one request. `paid` is true once any paid attempt was dispatched
        (authorized, committed or charge_unknown; a released one never ran), and
        `charge_unknown` is true while any paid attempt may have been billed unseen."""
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT DISTINCT status FROM ledger WHERE request_id=?", (request_id,)
            ).fetchall()
        seen = {row["status"] for row in rows}
        return {
            "paid": bool(seen & {"authorized", "committed", "charge_unknown"}),
            "charge_unknown": "charge_unknown" in seen,
            "unfinalized": "authorized" in seen,
        }

    def attempts(self, request_id: str) -> list[dict[str, object]]:
        """Ledger rows of one request key, ordered by authorization time, with parsed attempt_id."""
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT attempt_key, request_id, day, model, amount, status, actual_cost,"
                " authorized_at, finalized_at FROM ledger WHERE request_id=?"
                " ORDER BY authorized_at, attempt_key",
                (request_id,),
            ).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            _, _, attempt_id = str(item["attempt_key"]).rpartition(":")
            item["attempt_id"] = attempt_id
            result.append(item)
        return result

    def hold_unfinalized(self, request_id: str) -> int:
        """A job ended while a paid attempt was still `authorized`: nobody saw its outcome,
        so it keeps its full allowance as `charge_unknown`. Returns how many rows changed."""
        with self.store.transaction() as db:
            cursor = db.execute(
                "UPDATE ledger SET status='charge_unknown', finalized_at=?"
                " WHERE request_id=? AND status='authorized'",
                (self.store.clock(), request_id),
            )
        return cursor.rowcount
