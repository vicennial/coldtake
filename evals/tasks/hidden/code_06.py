import json
import os

import pytest

import solution
from solution import load_settings

EXPECTED = {"timeout": 30, "retries": 3, "verbose": False}


def test_missing_file_returns_defaults(tmp_path):
    assert load_settings(str(tmp_path / "nope.json")) == EXPECTED


def test_valid_file_merges_over_defaults(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"timeout": 5, "extra": "x"}))
    result = load_settings(str(p))
    assert result == {"timeout": 5, "retries": 3, "verbose": False, "extra": "x"}
    p.write_text(json.dumps({"retries": 0, "verbose": True}))
    assert load_settings(str(p)) == {"timeout": 30, "retries": 0, "verbose": True}


def test_corrupt_json_returns_defaults(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{not json")
    assert load_settings(str(p)) == EXPECTED


def test_non_object_json_returns_defaults(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("[1, 2, 3]")
    assert load_settings(str(p)) == EXPECTED


def test_directory_path_returns_defaults(tmp_path):
    assert load_settings(str(tmp_path)) == EXPECTED


def test_unreadable_file_returns_defaults(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"timeout": 5}))
    p.chmod(0)
    try:
        if os.access(str(p), os.R_OK):
            pytest.skip("file permissions are not enforced for this user")
        assert load_settings(str(p)) == EXPECTED
    finally:
        p.chmod(0o600)


def test_absurdly_deep_nesting_returns_defaults(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("[" * 200000 + "]" * 200000)
    assert load_settings(str(p)) == EXPECTED
    deep_object = '{"a":' * 200000 + "1" + "}" * 200000
    p.write_text(deep_object)
    try:
        json.loads(deep_object)
    except RecursionError:
        # the standard decoder gives up, so the prompt requires the defaults
        assert load_settings(str(p)) == EXPECTED
    else:  # pragma: no cover - a Python whose decoder copes with this depth: merging is correct then
        assert load_settings(str(p))["timeout"] == 30


@pytest.mark.parametrize("content", [b"{not json", b"[1]", b"\xff\xfe\x00bad", b"", b"{" * 200000])
def test_every_fallback_is_a_fresh_copy(tmp_path, content):
    p = tmp_path / "s.json"
    p.write_bytes(content)
    first = load_settings(str(p))
    first["timeout"] = -1
    first["junk"] = 1
    assert load_settings(str(p)) == EXPECTED
    assert solution.DEFAULTS == EXPECTED


def test_undecodable_bytes_return_defaults(tmp_path):
    p = tmp_path / "s.json"
    p.write_bytes(b"\xff\xfe\x00bad")
    assert load_settings(str(p)) == EXPECTED


def test_defaults_not_mutated_and_result_is_a_copy(tmp_path):
    before = dict(solution.DEFAULTS)
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"timeout": 99}))
    load_settings(str(p))
    result = load_settings(str(tmp_path / "absent.json"))
    result["timeout"] = -1
    assert solution.DEFAULTS == before
