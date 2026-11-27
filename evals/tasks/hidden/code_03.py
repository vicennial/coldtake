import pytest

from solution import SlidingWindowLimiter


def test_request_exactly_window_old_no_longer_counts():
    lim = SlidingWindowLimiter(2, 10)
    assert lim.allow(0) is True
    assert lim.allow(1) is True
    assert lim.allow(5) is False
    # The request at t=0 is exactly 10 seconds old at t=10: it no longer counts.
    assert lim.allow(10) is True


def test_just_inside_window_still_counts():
    lim = SlidingWindowLimiter(1, 10)
    assert lim.allow(0) is True
    assert lim.allow(9.999) is False
    assert lim.allow(10) is True


def test_rejected_requests_are_not_recorded():
    lim = SlidingWindowLimiter(1, 10)
    assert lim.allow(0) is True
    assert lim.allow(5) is False
    assert lim.allow(10) is True


def test_boundary_is_decided_on_the_age_not_on_a_rounded_cutoff():
    # now - window rounds to a value equal to t0 here, but the request is only about
    # 1.1e-16 seconds old, which is less than the window of 1.5e-16.
    t0 = 1.0 - 2.0 ** -53
    lim = SlidingWindowLimiter(1, 1.5e-16)
    assert lim.allow(t0) is True
    assert lim.allow(1.0) is False


def test_several_requests_expiring_before_one_call_are_all_evicted():
    lim = SlidingWindowLimiter(3, 10)
    for t in (0, 1, 2):
        assert lim.allow(t) is True
    assert lim.remaining(12) == 3     # all three old requests are out of the window
    assert lim.allow(12) is True
    assert lim.allow(12) is True
    assert lim.allow(12) is True
    assert lim.allow(12) is False


def test_requests_at_the_same_instant_each_count():
    lim = SlidingWindowLimiter(2, 10)
    assert [lim.allow(5), lim.allow(5), lim.allow(5)] == [True, True, False]
    assert lim.remaining(5) == 0


def test_remaining_is_the_limit_when_nothing_counts():
    lim = SlidingWindowLimiter(3, 4)
    assert lim.remaining(0) == 3
    lim.allow(0)
    assert lim.remaining(4) == 3
    assert lim.remaining(100) == 3


def test_remaining_at_boundary():
    lim = SlidingWindowLimiter(3, 4)
    lim.allow(0)
    lim.allow(1)
    assert lim.remaining(3) == 1
    assert lim.remaining(4) == 2


@pytest.mark.parametrize("limit,window", [(0, 1), (-1, 1), (1, 0), (1, -2)])
def test_invalid_arguments(limit, window):
    with pytest.raises(ValueError):
        SlidingWindowLimiter(limit, window)
