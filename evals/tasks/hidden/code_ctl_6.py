import pytest

from solution import parse_duration


@pytest.mark.parametrize("text,seconds", [
    ("90s", 90), ("0s", 0), ("1h30m", 5400), ("2d", 172800),
    ("2d4h10m5s", 2 * 86400 + 4 * 3600 + 600 + 5), ("45m", 2700), ("1d1s", 86401),
    ("1h", 3600), ("24h", 86400), ("007s", 7), ("999999999s", 999999999),
    ("999999999d999999999h999999999m999999999s", 999999999 * (86400 + 3600 + 60 + 1)),
])
def test_valid(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", [
    "", " 1h", "1h ", "1 h", "h", "10", "1x", "1H", "1M", "-1h", "+1h", "1.5h", "1,5h",
    "1h1h", "30m1h", "1s1m", "1h30", "1h30m ", "١h", "1h\n",
    "1h1d", "1d1d", "1m1h", "1s1d", "1d1h1d",
    "1000000000s", "0000000001s", "9" * 5000 + "s", "1h" + "9" * 5000 + "m",
])
def test_invalid(text):
    with pytest.raises(ValueError):
        parse_duration(text)


@pytest.mark.parametrize("value", [None, 5, b"1h", ["1h"], 1.5])
def test_non_string(value):
    with pytest.raises(ValueError):
        parse_duration(value)
