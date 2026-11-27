import pytest

from solution import redirect_headers


def test_valid():
    assert redirect_headers("https://example.test/a?b=1") == [
        ("Location", "https://example.test/a?b=1"),
        ("Cache-Control", "no-store"),
    ]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_valid_statuses(status):
    assert redirect_headers("/x", status)[0] == ("Location", "/x")


@pytest.mark.parametrize("status", [200, 304, 404, 0])
def test_invalid_status(status):
    with pytest.raises(ValueError):
        redirect_headers("/x", status)


def test_empty_url():
    with pytest.raises(ValueError):
        redirect_headers("")


@pytest.mark.parametrize("code", list(range(0x00, 0x20)) + [0x7F])
def test_every_control_character_rejected(code):
    with pytest.raises(ValueError):
        redirect_headers("/ok" + chr(code) + "Set-Cookie: a=b")


def test_cr_only_injection():
    with pytest.raises(ValueError):
        redirect_headers("/home\rSet-Cookie: session=evil")
