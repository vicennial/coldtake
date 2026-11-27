import threading

import pytest

import solution
from solution import TokenProvider

_REAL_LOCK = threading.Lock
_REAL_RLOCK = threading.RLock


class FakeClock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


class Trace:
    """What the instrumented locks saw: every acquisition and the first contended one."""

    def __init__(self):
        self.contended = threading.Event()
        self.acquired = []


class TracingLock:
    """A lock that reports when a thread has to wait for it, so the test needs no sleeping."""

    def __init__(self, real, trace):
        self._real = real
        self._trace = trace

    def acquire(self, blocking=True, timeout=-1):
        self._trace.acquired.append(threading.get_ident())
        if self._real.acquire(False):
            return True
        if not blocking:
            return False
        self._trace.contended.set()  # a thread is about to wait behind the holder
        return self._real.acquire(True, timeout)

    def release(self):
        self._real.release()

    def locked(self):
        return self._real.locked()

    def __getattr__(self, name):
        # Condition uses these when its lock offers them (RLock does)
        if name in ("_is_owned", "_release_save", "_acquire_restore"):
            return getattr(self._real, name)
        raise AttributeError(name)

    def __enter__(self):
        self.acquire()
        return True

    def __exit__(self, *exc):
        self.release()


class _ThreadingProxy:
    def __init__(self, trace):
        self._trace = trace

    def Lock(self):  # noqa: N802
        return TracingLock(_REAL_LOCK(), self._trace)

    def RLock(self):  # noqa: N802
        return TracingLock(_REAL_RLOCK(), self._trace)

    def __getattr__(self, name):
        return getattr(threading, name)


@pytest.fixture
def trace(monkeypatch):
    """Make the solution module create instrumented locks (before the provider is built)."""
    t = Trace()
    proxy = _ThreadingProxy(t)
    for name, value in list(vars(solution).items()):
        if value is threading:
            monkeypatch.setattr(solution, name, proxy)
    for name in ("Lock", "RLock"):
        if name in vars(solution):
            monkeypatch.setattr(solution, name, getattr(proxy, name))
    return t


def test_refreshes_once_and_reuses_token():
    clock = FakeClock()
    calls = []

    def refresh():
        calls.append(1)
        return "tok-%d" % len(calls), 60

    provider = TokenProvider(refresh, clock=clock)
    assert provider.get() == "tok-1"
    assert provider.get() == "tok-1"
    assert len(calls) == 1


def test_refreshes_after_expiry():
    clock = FakeClock()
    calls = []

    def refresh():
        calls.append(1)
        return "tok-%d" % len(calls), 60

    provider = TokenProvider(refresh, clock=clock)
    assert provider.get() == "tok-1"
    clock.now += 59.9
    assert provider.get() == "tok-1"
    clock.now += 0.1  # exactly at expiry: expired
    assert provider.get() == "tok-2"
    assert len(calls) == 2


def test_expiry_is_measured_when_the_refresh_has_finished():
    clock = FakeClock()
    calls = []

    def refresh():
        calls.append(1)
        clock.now += 5  # the refresh itself takes 5 seconds
        return "tok-%d" % len(calls), 60

    provider = TokenProvider(refresh, clock=clock)
    assert provider.get() == "tok-1"  # refresh ends at 105, so the token expires at 165
    clock.now = 164.9
    assert provider.get() == "tok-1"
    assert len(calls) == 1
    clock.now = 165.0
    assert provider.get() == "tok-2"


def test_reading_a_valid_token_does_not_take_the_lock(trace):
    clock = FakeClock()
    provider = TokenProvider(lambda: ("tok", 60), clock=clock)
    assert provider.get() == "tok"
    taken = len(trace.acquired)
    for _ in range(5):
        assert provider.get() == "tok"
    assert len(trace.acquired) == taken, "valid-token reads must not acquire the lock"


@pytest.mark.parametrize("expired_first", [False, True], ids=["no_token_yet", "token_expired"])
def test_concurrent_callers_trigger_single_refresh(trace, expired_first):
    clock = FakeClock()
    entered = threading.Event()
    release = threading.Event()
    count_lock = _REAL_LOCK()
    calls = []
    slow = 2 if expired_first else 1  # the refresh that the concurrent callers race for

    def refresh():
        with count_lock:
            calls.append(1)
            n = len(calls)
        if n == slow:
            entered.set()
            release.wait(10)
        return "tok-%d" % n, 60

    provider = TokenProvider(refresh, clock=clock)
    if expired_first:
        assert provider.get() == "tok-1"
        clock.now += 100  # that token has now expired
    results = []

    def worker():
        results.append(provider.get())

    first = threading.Thread(target=worker)
    first.start()
    assert entered.wait(5)  # the first caller is now inside the slow refresh()
    second = threading.Thread(target=worker)
    second.start()
    # The second caller also finds the token missing or expired. It either waits behind the
    # lock (the tracing lock reports that as soon as it happens) or, for a lock-free design,
    # we give it a bounded time. Only then is the slow refresh allowed to finish.
    trace.contended.wait(3)
    release.set()
    first.join(10)
    second.join(10)

    assert results == ["tok-%d" % slow] * 2
    assert len(calls) == slow, "refresh() must run once for concurrent callers"
