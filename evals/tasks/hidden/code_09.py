import builtins
import io
import os

import pytest

from solution import store_upload


def test_nested_write(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    path = store_upload(str(root), "reports/2024/q1.csv", b"abc")
    assert os.path.realpath(path) == os.path.realpath(root / "reports" / "2024" / "q1.csv")
    assert (root / "reports" / "2024" / "q1.csv").read_bytes() == b"abc"


def test_overwrite(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    store_upload(str(root), "a.txt", b"one")
    store_upload(str(root), "a.txt", b"two")
    assert (root / "a.txt").read_bytes() == b"two"


@pytest.mark.parametrize("name", ["", "../x.txt", "a/../../x.txt", "a\\..\\..\\x.txt", ".."])
def test_rejects_empty_and_parent_segments(tmp_path, name):
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(ValueError):
        store_upload(str(root), name, b"x")
    assert list(tmp_path.iterdir()) == [root]
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("name", ["", "../x.txt", "a/../../x.txt", "a\\..\\..\\x.txt", "/abs/x.txt", "\\abs\\x.txt"])
def test_invalid_names_are_rejected_before_any_file_system_access(tmp_path, monkeypatch, name):
    root = tmp_path / "root"
    root.mkdir()

    def forbidden(*args, **kwargs):
        raise AssertionError("file system touched before the name was rejected")

    with monkeypatch.context() as m:
        for owner, attr in ((os, "makedirs"), (os, "mkdir"), (os, "stat"), (os, "lstat"),
                            (os, "open"), (os, "scandir"), (os, "listdir"), (os.path, "realpath"),
                            (builtins, "open"), (io, "open")):
            m.setattr(owner, attr, forbidden)
        with pytest.raises(ValueError):
            store_upload(str(root), name, b"x")


def test_rejects_absolute_path_without_writing(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    target = elsewhere / "evil.txt"
    with pytest.raises(ValueError):
        store_upload(str(root), str(target), b"x")
    assert not elsewhere.exists()
    assert list(root.iterdir()) == []
