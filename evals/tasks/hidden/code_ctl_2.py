import pytest

from solution import TokenBucket


def test_starts_full_and_drains():
    b = TokenBucket(1, 5)
    assert b.try_consume(0, 5) is True
    assert b.try_consume(0) is False


def test_refill_over_time():
    b = TokenBucket(2, 4)
    assert b.try_consume(0, 4) is True
    assert b.try_consume(0.5) is True   # 1 token earned
    assert b.try_consume(0.5) is False
    assert b.try_consume(1.0) is True   # 1 more earned


def test_cap_at_capacity():
    b = TokenBucket(1, 5)
    b.try_consume(0, 5)
    assert b.try_consume(100, 5) is True
    assert b.try_consume(100) is False


def test_failed_consume_removes_nothing():
    b = TokenBucket(1, 4)
    b.try_consume(0, 3)
    assert b.try_consume(0, 2) is False
    assert b.available() == 1.0
    assert b.try_consume(0, 1) is True


def test_failed_consume_leaves_the_bucket_usable_without_available():
    b = TokenBucket(1, 4)
    assert b.try_consume(0, 3) is True
    assert b.try_consume(0, 2) is False
    assert b.try_consume(0, 1) is True
    assert b.try_consume(0) is False


def test_available_reports_fractional_tokens():
    b = TokenBucket(1, 3)
    assert b.try_consume(0, 3) is True
    assert b.try_consume(0.5) is False
    assert b.available() == 0.5
    assert b.try_consume(1.25, 1) is True
    assert b.available() == 0.25


def test_available_reports_tokens_without_changing_them():
    b = TokenBucket(1, 4)
    assert b.available() == 4.0
    b.try_consume(0, 3)
    assert b.available() == 1.0
    assert b.available() == 1.0


def test_time_going_backwards():
    b = TokenBucket(1, 3)
    b.try_consume(10, 3)
    assert b.try_consume(5) is False    # earlier time: no refill, clock stays 10
    assert b.try_consume(11) is True    # one second after the *latest* time


def test_going_back_in_time_never_credits_extra_tokens():
    b = TokenBucket(1, 3)
    assert b.try_consume(10, 3) is True
    assert b.try_consume(5) is False          # earlier time: nothing earned, clock stays at 10
    assert b.try_consume(10) is False         # still no time has passed since the real clock
    assert b.try_consume(10.5) is False       # half a second earned since 10, not since 5
    assert b.try_consume(11) is True


def test_a_time_before_the_start_time_credits_nothing():
    b = TokenBucket(1, 2, now=100)
    assert b.try_consume(100, 2) is True
    assert b.try_consume(50) is False
    assert b.try_consume(100.5) is False
    assert b.try_consume(101) is True


def test_a_failed_consume_still_keeps_the_tokens_earned_so_far():
    b = TokenBucket(1, 4)
    assert b.try_consume(0, 4) is True
    assert b.try_consume(2, 3) is False       # 2 tokens earned, 3 wanted: nothing removed
    assert b.available() == 2.0
    assert b.try_consume(3, 3) is True        # the 2 earned plus 1 more


def test_start_time_respected():
    b = TokenBucket(1, 2, now=100)
    assert b.try_consume(100, 2) is True
    assert b.try_consume(101) is True


@pytest.mark.parametrize("rate,cap", [(0, 1), (-1, 1), (1, 0), (1, 0.5)])
def test_constructor_validation(rate, cap):
    with pytest.raises(ValueError):
        TokenBucket(rate, cap)


@pytest.mark.parametrize("n", [0, -1, 6])
def test_consume_validation(n):
    with pytest.raises(ValueError):
        TokenBucket(1, 5).try_consume(0, n)
