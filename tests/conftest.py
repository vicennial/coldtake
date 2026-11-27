from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from coldtake.config import load_config
from coldtake.engine import Engine
from coldtake.providers import build_providers
from coldtake.store import Store

from .fakes import FakeCLI

TERMINAL = {"complete", "failed", "timed_out", "lost", "charge_unknown"}
PACKET = {"mode": "alternate", "question": "Which database should we use?", "goals": ["ship"]}
OPTIONS = [{"id": "pg", "label": "Postgres"}, {"id": "lite", "label": "SQLite"}]


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Never touch a real home, login or user config."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in (
        "CODEX_HOME", "CLAUDE_CONFIG_DIR", "OPENROUTER_API_KEY", "COLDTAKE_CONFIG",
        "COLDTAKE_STATE_DIR", "COLDTAKE_LEAD_FAMILY", "COLDTAKE_NAMESPACE",
        "XDG_CONFIG_HOME", "XDG_STATE_HOME",
    ):  # fmt: skip
        monkeypatch.delenv(name, raising=False)


class Rig:
    """Config + fake providers + engine, wired to a temp state dir."""

    def __init__(
        self, tmp_path: Path, overrides: dict[str, Any] | None = None, router=None,
        same_family: bool = True,
    ):  # fmt: skip
        self.tmp = tmp_path
        # one Engine per state directory per process: every rig in a test gets its own
        taken = len(list(tmp_path.glob("state*")))
        state = tmp_path / ("state" if taken == 0 else f"state-{taken}")
        self.claude = FakeCLI(tmp_path / "fake-claude", "claude")
        self.codex = FakeCLI(tmp_path / "fake-codex", "codex")
        self.router = router
        raw = {
            "state_dir": str(state),
            "binaries": {
                # same_family=False makes the lead's own family unavailable as a last resort
                "claude": str(self.claude.path) if same_family else "/nonexistent/claude",
                "codex": str(self.codex.path),
            },
            "limits": {"attempt_seconds": 20, "job_seconds": 40, "judge_seconds": 20},
        }
        for key, value in (overrides or {}).items():
            raw[key] = {**raw.get(key, {}), **value} if isinstance(value, dict) else value
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(raw))
        self.config_path = path
        self.config = load_config(path)
        env = {"OPENROUTER_API_KEY": "test-key"} if router is not None else {}
        self.providers = build_providers(
            self.config, env=env, transport=router.transport if router else None
        )
        self.engine = Engine(self.config, self.providers)

    def run(self, packet: dict[str, Any], request_id: str = "req-1", lead: str = "anthropic",
            mode: str | None = None, timeout: float = 30) -> dict[str, Any]:  # fmt: skip
        self.engine.submit(request_id, packet, lead_family=lead, mode=mode)
        return self.engine.wait(request_id, timeout)

    def attempts(self, request_id: str = "req-1") -> list[dict[str, Any]]:
        with self.engine.store.connect() as db:
            rows = db.execute("SELECT * FROM attempts WHERE request_id=?", (request_id,))
            return [dict(r) for r in rows]


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path, same_family=False)


@pytest.fixture
def make_rig(tmp_path):
    def build(overrides=None, router=None, same_family=False):
        return Rig(tmp_path, overrides, router, same_family)

    return build


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "store-state")
