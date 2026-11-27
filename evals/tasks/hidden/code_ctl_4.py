import threading

import pytest

from solution import HitCounter


class RendezvousKey:
    """A key whose hash() makes two threads meet, so the test needs no sleeping.

    The ``nth`` call of hash() made by each thread waits at a barrier for the other
    thread. With nth=2 that is the point between reading a count and writing it back,
    so an implementation without a lock around the read-check-write sequence lets both
    threads read before either writes. A correct implementation holds a lock there: the
    other thread waits for it, the barrier times out and the first thread carries on.
    """

    def __init__(self, nth, timeout=1.5):
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
                pass
        return 11


def test_counts_and_limit():
    c = HitCounter(limit=3)
    assert [c.hit("a") for _ in range(5)] == [True, True, True, False, False]
    assert c.count("a") == 3
    assert c.count("never") == 0


def test_keys_are_independent():
    c = HitCounter(limit=1)
    assert c.hit("a") is True
    assert c.hit("b") is True
    assert c.hit("a") is False


def test_snapshot_is_a_copy():
    c = HitCounter(limit=5)
    c.hit("a")
    snap = c.snapshot()
    assert snap == {"a": 1} and type(snap) is dict
    snap["a"] = 99
    snap["b"] = 1
    assert c.snapshot() == {"a": 1}


def test_snapshot_holds_every_key():
    c = HitCounter(limit=5)
    for key, n in (("a", 1), ("b", 3), ("c", 5), ("d", 2)):
        for _ in range(n):
            c.hit(key)
    assert c.snapshot() == {"a": 1, "b": 3, "c": 5, "d": 2}


@pytest.mark.parametrize("limit", [0, -1])
def test_constructor_validation(limit):
    with pytest.raises(ValueError):
        HitCounter(limit=limit)


def test_default_limit_is_1000():
    c = HitCounter()
    assert sum(c.hit("k") for _ in range(1200)) == 1000


def test_two_simultaneous_hits_on_the_last_slot_cannot_both_win():
    key = RendezvousKey(nth=2)  # both threads have read the count before either writes it
    c = HitCounter(limit=1)
    results = []

    def worker():
        results.append(c.hit(key))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    assert sorted(results) == [False, True]
    assert c.count(key) == 1


def test_concurrent_hits_never_exceed_the_limit():
    c = HitCounter(limit=1000)
    wins = []

    def worker():
        mine = 0
        for _ in range(500):
            if c.hit("k"):
                mine += 1
        wins.append(mine)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(wins) == 1000
    assert c.count("k") == 1000
