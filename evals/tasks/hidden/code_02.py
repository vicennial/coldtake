import pytest

from solution import windows


def test_basic():
    assert windows([1, 2, 3, 4], 2) == [[1, 2], [2, 3], [3, 4]]


def test_window_equals_length():
    assert windows([1, 2, 3, 4], 4) == [[1, 2, 3, 4]]


def test_step_two_keeps_last_window():
    assert windows(list(range(6)), 2, 2) == [[0, 1], [2, 3], [4, 5]]


def test_step_skips_partial_tail():
    assert windows(list(range(7)), 3, 3) == [[0, 1, 2], [3, 4, 5]]


def test_shorter_than_size():
    assert windows([1, 2], 3) == []


def test_other_sequence_types():
    assert windows("abcd", 3) == [["a", "b", "c"], ["b", "c", "d"]]
    assert windows(range(3), 1) == [[0], [1], [2]]
    assert windows((1, 2, 3), 2) == [[1, 2], [2, 3]]


@pytest.mark.parametrize("size,step", [(0, 1), (-1, 1), (2, 0), (2, -3)])
def test_invalid_arguments(size, step):
    with pytest.raises(ValueError):
        windows([1, 2, 3], size, step)
