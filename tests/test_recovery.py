"""Liveness leases, stale-job recovery, and distinct causes for timeouts, lost workers
and charge ambiguity."""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from coldtake.engine import Engine

from .conftest import PACKET
from .fakes import FakeRouter, memo


class Clock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now


def ghost(engine, name="ghost", lease=10, job=100, instance="ghost-instance"):
    """A running job owned by an instance that no longer exists."""
    engine.store.create_run(
        name, mode="alternate", packet_hash="h", packet_json="{}", sealed_answer=None,
        sealed_choice=None, sealed_sha256=None, lead_family="x", warnings=[],
        instance=instance, lease_seconds=lease, job_seconds=job,
    )  # fmt: skip


@pytest.fixture
def clocked(rig):
    rig.engine.store.clock = Clock()
    return rig


def test_expired_lease_becomes_lost_on_read(clocked):
    ghost(clocked.engine)
    assert clocked.engine.status("ghost")["status"] == "running"
    clocked.engine.store.clock.now += 11
    view = clocked.engine.status("ghost")
    assert view["status"] == "lost" and view["reason"] == "lost"
    assert view["cause"]["type"] == "StaleJob" and "lease" in view["cause"]["message"]
    assert "new request_id" in view["recovery"]
    assert view["next_action"] == "stop_and_report"


def test_lost_with_a_possibly_sent_paid_attempt_is_charge_unknown(clocked):
    ghost(clocked.engine)
    clocked.engine.ledger.authorize("ghost", "advisor1-r1-a0", "m", 0.2)  # never finalized
    clocked.engine.store.clock.now += 11
    view = clocked.engine.status("ghost")
    assert view["status"] == "charge_unknown"
    with clocked.engine.store.connect() as db:
        assert db.execute("SELECT status FROM ledger").fetchone()[0] == "charge_unknown"
    assert clocked.engine.ledger.spent_today() == pytest.approx(0.2)  # allowance kept


def test_a_finalized_paid_attempt_does_not_make_a_lost_job_charge_unknown(clocked):
    ghost(clocked.engine)
    clocked.engine.ledger.authorize("ghost", "a0", "m", 0.2)
    clocked.engine.ledger.finalize("ghost", "a0", "committed", 0.01)
    clocked.engine.store.clock.now += 11
    assert clocked.engine.status("ghost")["status"] == "lost"


def test_running_past_the_deadline_with_a_live_lease_is_timed_out(clocked):
    ghost(clocked.engine, lease=10_000, job=60)
    clocked.engine.store.clock.now += 60 + 31
    view = clocked.engine.status("ghost")
    assert view["status"] == "timed_out" and "deadline" in view["cause"]["message"]


def test_startup_recovers_jobs_of_an_earlier_instance_in_this_process(clocked):
    ghost(clocked.engine, lease=10_000)
    assert clocked.engine.store.get_run("ghost")["status"] == "running"
    clocked.engine.close(0)  # one Engine per state directory: the old server is gone
    restarted = Engine(clocked.config, clocked.providers, store=clocked.engine.store)
    assert restarted.store.get_run("ghost")["status"] == "lost"


def test_a_job_of_a_dead_process_is_recovered_even_with_a_fresh_lease(clocked):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    ghost(clocked.engine, lease=10_000)
    with clocked.engine.store.connect() as db:
        db.execute("UPDATE runs SET owner_pid=? WHERE request_id='ghost'", (dead.pid,))
    assert clocked.engine.status("ghost")["status"] == "lost"


def test_a_live_job_of_another_process_is_left_alone(clocked):
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        ghost(clocked.engine, lease=10_000)
        with clocked.engine.store.connect() as db:
            db.execute("UPDATE runs SET owner_pid=? WHERE request_id='ghost'", (other.pid,))
        clocked.engine.close(0)
        Engine(clocked.config, clocked.providers, store=clocked.engine.store)  # a second server
        assert clocked.engine.status("ghost")["status"] == "running"
    finally:
        other.kill()
        other.wait()


def test_old_records_without_a_lease_are_recovered_too(clocked):
    ghost(clocked.engine, lease=10_000)
    with clocked.engine.store.connect() as db:
        db.execute(
            "UPDATE runs SET record_version=0, lease_expires_at=NULL, owner_pid=NULL"
            " WHERE request_id='ghost'"
        )
    assert clocked.engine.status("ghost")["status"] == "lost"


def test_a_late_worker_cannot_overwrite_a_recovery(clocked):
    ghost(clocked.engine)
    clocked.engine.store.clock.now += 11
    clocked.engine.status("ghost")
    assert clocked.engine.store.finish("ghost", "ghost-instance", "complete") is False
    assert clocked.engine.store.heartbeat("ghost", "ghost-instance", 10) is False
    assert clocked.engine.status("ghost")["status"] == "lost"


def test_a_running_job_renews_its_lease(make_rig):
    rig = make_rig({"limits": {"lease_seconds": 5}})
    rig.codex.script(advisor=[{"sleep": 3}, {"json": memo()}])
    rig.engine.submit("req-1", PACKET, lead_family="anthropic")
    first = rig.engine.store.get_run("req-1")["lease_expires_at"]
    time.sleep(2.5)  # heartbeat interval is lease/3
    row = rig.engine.store.get_run("req-1")
    assert row["status"] == "running" and row["lease_expires_at"] > first
    assert row["heartbeat_at"] > row["created_at"]
    rig.engine.drain(30)


def test_subscription_timeout_is_timed_out_not_charge_unknown(make_rig):
    router = FakeRouter(memo("must not be used"))
    rig = make_rig({"limits": {"attempt_seconds": 1}}, router)
    rig.codex.script(advisor=[{"sleep": 8}])
    out = rig.run(PACKET)
    assert out["status"] == "timed_out" and out["reason"] == "timed_out"
    assert out["cause"]["type"] == "TimeoutExpired"
    assert router.requests == []  # a timeout is not a fallback signal
    attempts = rig.attempts()
    assert [(a["billing"], a["outcome"]) for a in attempts] == [("subscription", "timed_out")]
    with rig.engine.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0


def test_unexpected_errors_store_a_real_cause(rig, monkeypatch):
    def boom(job):
        raise RuntimeError("disk exploded in test")

    monkeypatch.setattr(rig.engine, "_execute", boom)
    out = rig.run(PACKET)
    assert out["status"] == "failed" and out["reason"] == "internal_error"
    assert out["cause"] == {"type": "RuntimeError", "message": "disk exploded in test"}


def test_every_failed_attempt_stores_type_and_message(make_rig):
    rig = make_rig()
    rig.codex.script(advisor=[{"error": "boom with details", "code": 2}])
    rig.run(PACKET)
    attempt = rig.attempts()[0]
    assert attempt["cause_type"] == "Unclassified"
    assert "boom with details" in attempt["cause_message"]


def test_distinct_terminal_statuses_are_counted_separately_in_stats(clocked):
    from coldtake.stats import collect

    ghost(clocked.engine, "g1")
    ghost(clocked.engine, "g2", lease=10_000, job=5)
    clocked.engine.ledger.authorize("g1", "a0", "m", 0.1)
    clocked.engine.store.clock.now += 100
    clocked.engine.status("g1")
    clocked.engine.status("g2")
    runs = collect(clocked.engine.store)["runs"]["alternate"]
    assert runs == {"charge_unknown": 1, "timed_out": 1}
