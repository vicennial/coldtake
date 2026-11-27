"""OpenRouter budget: fail closed, atomic across threads and processes, charge ambiguity
only for paid attempts."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading

import httpx
import pytest

from coldtake.budget import BudgetExceeded, Ledger
from coldtake.providers import Route, worst_case_cost

from .conftest import PACKET
from .fakes import FakeRouter, memo

QUOTA = [{"error": "You've hit your usage limit", "code": 1}]


def _ledger(store, per_request=1.0, daily=1.0):
    return Ledger(store, per_request, daily)


def test_worst_case_cost_is_computed_from_bytes_and_output_allowance():
    route = Route("openrouter", "x", "m", None, "paid", 1.0, 2.0, 1000)
    # (1000 bytes + 4000 reserve) * $1/M + 1000 * $2/M
    assert worst_case_cost(route, 1000) == pytest.approx(0.007, rel=1e-6)
    assert worst_case_cost(route, 1000) >= 0.007  # rounded outwards, never below


def test_request_over_the_cap_is_refused_before_spawn(make_rig):
    router = FakeRouter(memo())
    rig = make_rig({"budget": {"per_request_usd": 0.0001, "daily_usd": 5}}, router)
    rig.codex.script(advisor=QUOTA)
    out = rig.run(PACKET)
    assert out["status"] == "failed" and out["reason"] == "budget_exceeded"
    assert router.requests == []


def test_daily_cap_is_enforced_across_requests(make_rig):
    router = FakeRouter(memo())
    rig = make_rig({"budget": {"per_request_usd": 1.0, "daily_usd": 1.0}}, router)
    rig.codex.script(advisor=QUOTA)
    rig.engine.ledger.authorize("earlier", "a0", "m", 0.999)
    out = rig.run(PACKET)
    assert out["status"] == "failed" and out["reason"] == "budget_exceeded"
    assert router.requests == []


def test_authorization_is_atomic_across_threads(store):
    ledger = _ledger(store, per_request=1.0, daily=1.0)
    results = []

    def attempt(i):
        try:
            ledger.authorize(f"req-{i}", "a0", "m", 0.3)
            results.append(True)
        except BudgetExceeded:
            results.append(False)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(True) == 3
    assert ledger.spent_today() == pytest.approx(0.9)


def test_authorization_is_atomic_across_processes(tmp_path, store):
    script = textwrap.dedent(
        """
        import sys
        from coldtake.budget import BudgetExceeded, Ledger
        from coldtake.store import Store
        ledger = Ledger(Store(sys.argv[1]), 1.0, 1.0)
        try:
            ledger.authorize("req-" + sys.argv[2], "a0", "m", 0.3)
            print("yes")
        except BudgetExceeded:
            print("no")
        """
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(store.root), str(i)],
            stdout=subprocess.PIPE,
            text=True,
        )
        for i in range(8)
    ]
    answers = [p.communicate()[0].strip() for p in procs]
    assert answers.count("yes") == 3


def test_released_attempts_free_their_allowance(store):
    ledger = _ledger(store)
    ledger.authorize("r", "a0", "m", 0.9)
    ledger.finalize("r", "a0", "released")
    ledger.authorize("r", "a1", "m", 0.9)  # fits again


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadTimeout("slow"),
        httpx.RemoteProtocolError("dropped"),
        httpx.Response(500, text="oops"),
        httpx.Response(504, text="gateway"),
        httpx.Response(200, json={"unexpected": True}),
    ],
)
def test_paid_ambiguity_is_charge_unknown_and_keeps_its_allowance(make_rig, failure):
    router = FakeRouter(failure, memo("never used"))
    rig = make_rig(router=router)
    rig.codex.script(advisor=QUOTA)
    out = rig.run(PACKET)
    assert out["status"] == "charge_unknown"
    assert out["reason"] == "charge_unknown" and out["cause"]["type"]
    assert len(router.requests) == 1  # no fallback, no retry
    with rig.engine.store.connect() as db:
        row = db.execute("SELECT status, amount FROM ledger").fetchone()
    assert row["status"] == "charge_unknown" and row["amount"] > 0
    assert rig.engine.ledger.spent_today() == pytest.approx(row["amount"])
    assert [(a["billing"], a["outcome"]) for a in rig.attempts()] == [
        ("subscription", "quota"),
        ("paid", "charge_unknown"),
    ]


def test_connect_error_proves_nothing_was_sent(make_rig):
    router = FakeRouter(httpx.ConnectError("refused"), memo("second"))
    rig = make_rig(router=router)
    rig.codex.script(advisor=QUOTA)
    out = rig.run(PACKET)
    assert out["status"] == "complete"
    with rig.engine.store.connect() as db:
        assert [r[0] for r in db.execute("SELECT status FROM ledger ORDER BY rowid")] == [
            "released",
            "committed",
        ]


def test_committed_cost_uses_the_provider_reported_cost(make_rig):
    router = FakeRouter(memo())
    rig = make_rig(router=router)
    rig.codex.script(advisor=QUOTA)
    out = rig.run(PACKET)
    assert out["result"]["paid"]["committed_usd"] == pytest.approx(0.01)
    assert rig.engine.ledger.spent_today() == pytest.approx(0.01)


@pytest.mark.parametrize(
    "spec",
    [
        {"sleep": 6},
        {"error": "boom", "code": 2},
        {"error": "refused: safety", "code": 1},
        {"garbage": True},
    ],
)
def test_subscription_failures_never_become_charge_unknown(make_rig, spec):
    rig = make_rig({"limits": {"attempt_seconds": 1}})
    rig.codex.script(advisor=[spec])
    out = rig.run(PACKET)
    assert out["status"] in {"failed", "timed_out"}
    assert out["status"] != "charge_unknown"
    attempts = rig.attempts()
    assert attempts and {a["billing"] for a in attempts} == {"subscription"}
    assert "charge_unknown" not in {a["outcome"] for a in attempts}
    with rig.engine.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0
