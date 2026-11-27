import pytest

from solution import page_bounds, page_count


def test_full_pages():
    items = list(range(10))
    assert items[slice(*page_bounds(10, 1, 4))] == [0, 1, 2, 3]
    assert items[slice(*page_bounds(10, 2, 4))] == [4, 5, 6, 7]


def test_partial_last_page():
    assert page_bounds(10, 3, 4) == (8, 10)


def test_exact_multiple():
    assert page_bounds(8, 2, 4) == (4, 8)
    assert page_bounds(8, 3, 4) == (8, 8)


def test_past_the_end_and_empty():
    assert page_bounds(10, 99, 4) == (10, 10)
    assert page_bounds(0, 1, 5) == (0, 0)


def test_page_count():
    assert page_count(0, 5) == 0
    assert page_count(1, 5) == 1
    assert page_count(5, 5) == 1
    assert page_count(6, 5) == 2
    assert page_count(10, 4) == 3


@pytest.mark.parametrize("args", [(-1, 1, 1), (5, 0, 1), (5, -2, 1), (5, 1, 0), (5, 1, -3)])
def test_page_bounds_validation(args):
    with pytest.raises(ValueError):
        page_bounds(*args)


@pytest.mark.parametrize("args", [(-1, 1), (5, 0)])
def test_page_count_validation(args):
    with pytest.raises(ValueError):
        page_count(*args)
