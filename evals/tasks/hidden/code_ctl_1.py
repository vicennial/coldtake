import os

import pytest

from solution import resolve_member


@pytest.fixture
def layout(tmp_path):
    dest = tmp_path / "out"
    sibling = tmp_path / "out-private"
    outside = tmp_path / "elsewhere"
    for d in (dest, sibling, outside):
        d.mkdir()
    return dest, sibling, outside


def test_valid(layout):
    dest, _, _ = layout
    real = os.path.realpath(dest)
    assert resolve_member(str(dest), "a/b.txt") == os.path.join(real, "a", "b.txt")
    assert resolve_member(str(dest), "a/../c.txt") == os.path.join(real, "c.txt")


@pytest.mark.parametrize("name", ["1:report.txt", "logs/a:b.txt", "ab:c", "a/C:/x", "\u00e9:x"])
def test_colons_that_are_not_a_drive_prefix_are_valid(layout, name):
    dest, _, _ = layout
    assert resolve_member(str(dest), name) == os.path.join(os.path.realpath(dest), *name.split("/"))


@pytest.mark.parametrize("name", ["", ".", "a/..", "../x", "a/../../x", "../out-private/x", "/etc/x", "\\x", "C:/x", "c:x", "Z:"])
def test_rejected(layout, name):
    dest, _, _ = layout
    with pytest.raises(ValueError):
        resolve_member(str(dest), name)


def test_absolute_elsewhere(layout):
    dest, _, outside = layout
    with pytest.raises(ValueError):
        resolve_member(str(dest), str(outside / "x"))


def test_symlink_inside_dest_is_followed_and_resolved(layout):
    dest, _, _ = layout
    (dest / "real").mkdir()
    try:
        (dest / "alias").symlink_to(dest / "real", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert resolve_member(str(dest), "alias/f") == os.path.join(os.path.realpath(dest), "real", "f")


def test_symlink_escape(layout):
    dest, _, outside = layout
    try:
        (dest / "link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError):
        resolve_member(str(dest), "link/x")
