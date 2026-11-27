"""Public-tree hygiene checks (see CONTRIBUTING.md).

Verifies that tracked files stay free of local home paths, tailnet domains, session markers,
credential prefixes, private project names and legacy pre-rename identifiers, and that package
version metadata stays synchronized.
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path

import coldtake

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()

FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("home directory path", re.compile(r"(?:/home/[a-zA-Z0-9._-]+|/Users/[a-zA-Z0-9._-]+)")),
    ("tailnet domain", re.compile(r"\b[a-zA-Z0-9-]+\.ts\.net\b")),
    ("session trailer", re.compile(r"Claude-Session:")),
    ("credential prefix", re.compile(r"\b(?:sk-or-v1-|sk-ant-|ghp_|gho_|AKIA[0-9A-Z]{16})")),
    ("former package name", re.compile(r"(?i)\botherhand\b")),
    ("internal legacy router name", re.compile(r"(?i)\badvisor-router\b")),
    ("private org name", re.compile(r"(?i)\bdoubld\b")),
)


def _tracked_files() -> list[Path]:
    out = subprocess.check_output(["git", "-C", str(ROOT), "ls-files"], text=True)
    return [ROOT / rel for rel in out.splitlines() if (ROOT / rel).is_file()]


def test_public_tree_has_no_private_paths_secrets_or_legacy_names():
    findings: list[str] = []
    for path in _tracked_files():
        if path.resolve() == SELF:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in FORBIDDEN_PATTERNS:
            for lineno, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    rel = path.relative_to(ROOT)
                    findings.append(f"{rel}:{lineno}: {label}: {line.strip()[:120]}")
    assert findings == []


def test_package_metadata_constants():
    assert isinstance(coldtake.__version__, str) and coldtake.__version__
    assert isinstance(coldtake.API_VERSION, int) and coldtake.API_VERSION >= 1

    server_meta = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    assert server_meta["version"] == coldtake.__version__
    assert server_meta["packages"][0]["version"] == coldtake.__version__

    omni_meta = tomllib.loads(
        (ROOT / "integrations" / "omnigent" / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert omni_meta["project"]["version"] == coldtake.__version__
