import os

import pytest

from solution import safe_join


@pytest.fixture
def layout(tmp_path):
    base = tmp_path / "data"
    sibling = tmp_path / "data-private"
    outside = tmp_path / "elsewhere"
    for d in (base, sibling, outside):
        d.mkdir()
    (base / "a").mkdir()
    (sibling / "secret.txt").write_text("secret")
    return base, sibling, outside


def test_normal_paths(layout):
    base, _, _ = layout
    real = os.path.realpath(base)
    assert safe_join(str(base), "a/b.txt") == os.path.join(real, "a", "b.txt")
    assert safe_join(str(base), "a/../a/c.txt") == os.path.join(real, "a", "c.txt")
    assert safe_join(str(base), ".") == real


def test_parent_escape_rejected(layout):
    base, _, _ = layout
    for bad in ("../x", "a/../../x", "../../etc/passwd"):
        with pytest.raises(ValueError):
            safe_join(str(base), bad)


def test_sibling_directory_with_shared_prefix_rejected(layout):
    base, _, _ = layout
    with pytest.raises(ValueError):
        safe_join(str(base), "../data-private/secret.txt")
    with pytest.raises(ValueError):
        safe_join(str(base), "../data-private")


def test_symlink_that_stays_inside_base_is_accepted(layout):
    base, _, _ = layout
    (base / "real").mkdir()
    try:
        (base / "alias").symlink_to(base / "real", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    real = os.path.realpath(base)
    assert safe_join(str(base), "alias/f.txt") == os.path.join(real, "real", "f.txt")


def test_absolute_path_inside_base_is_accepted(layout):
    base, _, _ = layout
    real = os.path.realpath(base)
    inside = os.path.join(real, "a", "b.txt")
    assert safe_join(str(base), inside) == inside


@pytest.mark.skipif(os.name != "posix", reason="needs a POSIX root")
def test_file_system_root_as_base():
    assert safe_join("/", "no-such-dir-oh-eval/f.txt") == "/no-such-dir-oh-eval/f.txt"
    assert safe_join("/", "no-such-dir-oh-eval/../other") == "/other"
    assert safe_join("/", "..") == "/"


def test_absolute_path_rejected(layout):
    base, _, outside = layout
    with pytest.raises(ValueError):
        safe_join(str(base), str(outside / "x.txt"))


def test_symlink_escape_rejected(layout):
    base, _, outside = layout
    link = base / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError):
        safe_join(str(base), "link/x.txt")
