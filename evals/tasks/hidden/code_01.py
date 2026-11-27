import pytest

from solution import paginate


def test_response_has_every_required_field_with_the_right_values():
    result = paginate(list(range(25)), 3, 10)
    for key in ("items", "page", "per_page", "total_items", "total_pages", "has_next"):
        assert key in result, key
    assert result["page"] == 3
    assert result["per_page"] == 10
    assert result["total_items"] == 25
    assert result["items"] == [20, 21, 22, 23, 24]
    assert isinstance(result["items"], list)


def test_page_and_per_page_are_echoed_for_an_empty_page():
    result = paginate([], 2, 7)
    assert (result["page"], result["per_page"], result["total_items"]) == (2, 7, 0)
    assert result["total_pages"] == 0


def test_fewer_items_than_one_page():
    result = paginate([1, 2, 3], 1, 10)
    assert result["items"] == [1, 2, 3]
    assert (result["total_items"], result["total_pages"], result["has_next"]) == (3, 1, False)
    past = paginate([1, 2, 3], 2, 10)
    assert past["items"] == [] and past["total_pages"] == 1 and past["has_next"] is False


def test_exact_multiple_has_no_extra_page():
    items = list(range(20))
    result = paginate(items, 2, 10)
    assert result["total_pages"] == 2
    assert result["has_next"] is False
    assert result["items"] == list(range(10, 20))


def test_partial_last_page():
    items = list(range(25))
    result = paginate(items, 3, 10)
    assert result["total_pages"] == 3
    assert result["items"] == [20, 21, 22, 23, 24]
    assert result["has_next"] is False


def test_has_next_is_true_on_every_page_but_the_last():
    items = list(range(25))
    assert [paginate(items, p, 10)["has_next"] for p in (1, 2, 3, 4)] == [True, True, False, False]
    assert [paginate(list(range(20)), p, 10)["has_next"] for p in (1, 2, 3)] == [True, False, False]


def test_first_page_has_next():
    result = paginate(list(range(25)), 1, 10)
    assert result["has_next"] is True
    assert result["items"] == list(range(10))
    assert result["total_items"] == 25


def test_empty_input():
    result = paginate([], 1, 10)
    assert result["total_pages"] == 0
    assert result["items"] == []
    assert result["has_next"] is False


def test_page_past_end_is_empty():
    result = paginate(list(range(5)), 4, 5)
    assert result["items"] == []
    assert result["has_next"] is False


@pytest.mark.parametrize("page,per_page", [(0, 10), (-1, 10), (1, 0), (1, -5)])
def test_invalid_arguments(page, per_page):
    with pytest.raises(ValueError):
        paginate([1, 2, 3], page, per_page)
