import threading

import pytest

from solution import LoadingCache


class RendezvousKey:
    """A key whose hash() makes two threads meet, so the test needs no sleeping.

    The ``nth`` call of hash() made by each thread waits at a barrier for the other
    thread. An implementation that lets two threads both run the check-then-act
    sequence at once reaches the barrier with both threads, so both pass it together.
    An implementation that serialises the sequence with a lock leaves the second
    thread waiting for the lock, the barrier times out (``timed_out`` is set) and the
    first thread carries on alone. A correct solution therefore pays one timeout; a
    racy one is caught every time both threads get to run within the timeout.
    """

    def __init__(self, nth, timeout=1.5):
        self.timed_out = threading.Event()
        self._barrier = threading.Barrier(2)
        self._local = threading.local()
        self._nth = nth
        self._timeout = timeout

    def __hash__(self):
        n = getattr(self._local, "n", 0) + 1
        self._local.n = n
        if n == self._nth:
            try:
                self._barrier.wait(self._timeout)
            except threading.BrokenBarrierError:
                self.timed_out.set()
        return 7


def test_loader_called_once_sequentially():
    calls = []
    cache = LoadingCache(lambda k: calls.append(k) or k * 2)
    assert cache.get(3) == 6
    assert cache.get(3) == 6
    assert calls == [3]
    assert len(cache) == 1


def test_a_cached_none_is_a_hit():
    calls = []
    cache = LoadingCache(lambda k: calls.append(k))  # returns None
    assert cache.get("k") is None
    assert cache.get("k") is None
    assert calls == ["k"]
    assert len(cache) == 1


def test_exception_not_cached():
    attempts = []

    def loader(key):
        attempts.append(key)
        if len(attempts) == 1:
            raise RuntimeError("boom")
        return "ok"

    cache = LoadingCache(loader)
    with pytest.raises(RuntimeError):
        cache.get("k")
    assert len(cache) == 0
    assert cache.get("k") == "ok"
    assert attempts == ["k", "k"]


def test_invalidate_reloads():
    counter = {"n": 0}

    def loader(key):
        counter["n"] += 1
        return counter["n"]

    cache = LoadingCache(loader)
    assert cache.get("a") == 1
    cache.invalidate("a")
    cache.invalidate("missing")
    assert cache.get("a") == 2


def test_concurrent_misses_load_once():
    key = RendezvousKey(nth=1)  # both threads reach the cache lookup before either loads
    second_call = threading.Event()
    count_lock = threading.Lock()
    calls = []

    def loader(k):
        with count_lock:
            calls.append(k)
            n = len(calls)
        if n == 1 and not key.timed_out.is_set():
            # Hold the first load open until a second loader call shows up (the bug),
            # or until we know the other thread is waiting behind a lock (no bug).
            second_call.wait(5)
        else:
            second_call.set()
        return "value-%d" % n

    cache = LoadingCache(loader)
    results = []

    def worker():
        results.append(cache.get(key))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)

    assert len(results) == 2 and all(r == results[0] for r in results)
    assert len(calls) == 1, "loader must run at most once per key"
