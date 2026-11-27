import pytest

from solution import parse_duration


@pytest.mark.parametrize(
    "text,expected",
    [
        ("90s", 90),
        ("1h30m", 5400),
        ("2d4h", 2 * 86400 + 4 * 3600),
        ("  5M ", 300),
        ("1m1m", 120),
        ("30m1h", 5400),
        ("0s", 0),
    ],
)
def test_valid(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "5x", "1hh", "10", "h", "1h 30m", "1h30", "-5s", "5s!"])
def test_invalid_raises_value_error(text):
    with pytest.raises(ValueError):
        parse_duration(text)
