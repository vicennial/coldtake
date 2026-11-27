import pytest

from solution import make_cookie


def test_defaults_are_secure_httponly_lax():
    assert make_cookie("sid", "abc") == "sid=abc; Path=/; Secure; HttpOnly; SameSite=Lax"


def test_all_attributes_in_order():
    assert make_cookie("sid", "abc", max_age=60, path="/app") == (
        "sid=abc; Path=/app; Max-Age=60; Secure; HttpOnly; SameSite=Lax"
    )


def test_explicit_opt_out():
    out = make_cookie("t", "1", secure=False, httponly=False, samesite=None)
    assert out == "t=1; Path=/"


def test_samesite_none_requires_secure():
    with pytest.raises(ValueError):
        make_cookie("t", "1", secure=False, samesite="None")
    assert make_cookie("t", "1", samesite="None").endswith("SameSite=None")


@pytest.mark.parametrize("samesite", ["strict", "lax", "", "Foo"])
def test_invalid_samesite(samesite):
    with pytest.raises(ValueError):
        make_cookie("t", "1", samesite=samesite)


@pytest.mark.parametrize("name", ["", "a=b", "a;b", "a b", "a,b", "a\tb", "a\nb"])
def test_invalid_names(name):
    with pytest.raises(ValueError):
        make_cookie(name, "1")
