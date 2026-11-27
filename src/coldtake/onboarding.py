"""Onboarding helpers: `coldtake init` and `coldtake install <client>`."""

from __future__ import annotations

import json
import os
import re
import shlex
import tomllib
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

ClientName = Literal["claude", "codex", "gemini", "cursor"]
CLIENTS: tuple[ClientName, ...] = ("claude", "codex", "gemini", "cursor")


def _default_config_path(home: Path) -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else home / ".config"
    return base / "coldtake" / "config.yaml"


def render_init_yaml(detected: dict[str, dict[str, Any]]) -> str:
    """Build a clean, commented starter config.yaml from detected local CLIs."""
    lines = [
        "# coldtake user configuration (~/.config/coldtake/config.yaml)",
        "# Run `coldtake doctor` to verify detected CLIs and effective routing ladders.",
        "",
        "lead_family: auto",
        "",
        "binaries:",
    ]
    for name in ("claude", "codex", "gemini"):
        exe = (detected.get(name) or {}).get("executable")
        if exe:
            lines.append(f"  {name}: {exe}")
        else:
            lines.append(f"  {name}: null  # not detected on PATH")
    lines.extend(
        [
            "",
            "# Optional paid OpenRouter fallback (inactive unless OPENROUTER_API_KEY is set):",
            "budget:",
            "  per_request_usd: 1.0",
            "  daily_usd: 5.0",
            "",
            "# Optional allowlisted directories for `coldtake_check` context_paths (empty = off):",
            "check:",
            "  context_roots: []",
            "  max_context_file_bytes: 150000",
            "",
            "limits:",
            "  max_concurrent_jobs: 8",
            "",
        ]
    )
    return "\n".join(lines)


def write_init_config(
    detected: dict[str, dict[str, Any]],
    *,
    path: Path | None = None,
    force: bool = False,
    home: Path | None = None,
) -> Path:
    target = path if path is not None else _default_config_path(home or Path.home())
    if target.exists() and not force:
        raise FileExistsError(f"{target} already exists (pass --force to overwrite)")
    target.parent.mkdir(parents=True, exist_ok=True)
    text = render_init_yaml(detected)
    tmp = target.with_name(f".{target.name}.tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)
    return target


def _parse_command(command: str) -> tuple[str, list[str]]:
    parts = shlex.split(command)
    if not parts:
        raise ValueError("--command must not be empty")
    return parts[0], parts[1:]


def mcp_snippet(client: ClientName, *, command: str = "uvx coldtake") -> str:
    """Return the exact MCP configuration block for `client`."""
    exe, args = _parse_command(command)
    if client == "codex":
        args_toml = json.dumps(args)
        return f"[mcp_servers.coldtake]\ncommand = {json.dumps(exe)}\nargs = {args_toml}\n"
    entry: dict[str, Any] = {"command": exe, "args": args}
    if client == "claude":
        entry = {"type": "stdio", **entry}
    return json.dumps({"mcpServers": {"coldtake": entry}}, indent=2) + "\n"


def client_config_path(client: ClientName, *, home: Path | None = None) -> Path:
    root = home or Path.home()
    if client == "claude":
        return root / ".claude.json"
    if client == "codex":
        return root / ".codex" / "config.toml"
    if client == "gemini":
        return root / ".gemini" / "settings.json"
    if client == "cursor":
        return root / ".cursor" / "mcp.json"
    raise ValueError(f"unsupported client: {client!r}")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = (path.stat().st_mode & 0o777) if path.exists() else 0o600
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _skill_text() -> str:
    packaged = Path(__file__).resolve().parents[2] / "skills" / "coldtake" / "SKILL.md"
    if packaged.is_file():
        return packaged.read_text(encoding="utf-8")
    try:
        return files("coldtake").joinpath("SKILL.md").read_text(encoding="utf-8")
    except FileNotFoundError:
        return (
            "---\n"
            "name: coldtake\n"
            "description: Ask a different model family for a blind second opinion before "
            "presenting a recommendation, or critique an existing answer.\n"
            "---\n\n"
            "# coldtake\n\n"
            "Use `coldtake_check` for one-turn spec checks, `coldtake_review` to critique an "
            "existing answer, `coldtake_alternate` (with `sealed_answer`) for material decisions, "
            "and `coldtake_panel` for high-stakes irreversible decisions or conflicts.\n"
        )


def install_skill(client: ClientName, *, home: Path | None = None) -> Path:
    root = home or Path.home()
    if client == "claude":
        dest = root / ".claude" / "skills" / "coldtake" / "SKILL.md"
    elif client == "gemini":
        dest = root / ".gemini" / "skills" / "coldtake" / "SKILL.md"
    else:
        dest = root / ".agents" / "skills" / "coldtake" / "SKILL.md"
    _atomic_write(dest, _skill_text())
    return dest


def install_mcp(
    client: ClientName,
    *,
    command: str = "uvx coldtake",
    with_skill: bool = False,
    home: Path | None = None,
) -> dict[str, Any]:
    """Safely merge a `coldtake` stdio MCP server entry into `client`'s config file."""
    exe, args = _parse_command(command)
    target = client_config_path(client, home=home)

    if client == "codex":
        existing = target.read_text(encoding="utf-8") if target.is_file() else ""
        if existing:
            tomllib.loads(existing)  # fail closed if existing TOML is malformed
        block = mcp_snippet("codex", command=command)
        pattern = re.compile(r"(?ms)^\[mcp_servers\.coldtake\]\n.*?(?=^\[|\Z)")
        if pattern.search(existing):
            updated = pattern.sub(block + "\n", existing, count=1)
        else:
            sep = "\n" if existing and not existing.endswith("\n") else ""
            gap = "\n" if existing.strip() else ""
            updated = existing + sep + gap + block
        tomllib.loads(updated)  # verify resulting TOML parses cleanly
        _atomic_write(target, updated)
    else:
        data: dict[str, Any] = {}
        if target.is_file():
            loaded = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError(f"{target} must contain a JSON object")
            data = loaded
        servers = data.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            raise ValueError(f"{target}: mcpServers must be a JSON object")
        entry: dict[str, Any] = {"command": exe, "args": args}
        if client == "claude":
            entry = {"type": "stdio", **entry}
        servers["coldtake"] = entry
        _atomic_write(target, json.dumps(data, indent=2) + "\n")

    skill_path = install_skill(client, home=home) if with_skill else None
    return {
        "client": client,
        "config_path": str(target),
        "command": exe,
        "args": args,
        "skill_path": str(skill_path) if skill_path else None,
    }


__all__ = [
    "CLIENTS",
    "ClientName",
    "client_config_path",
    "install_mcp",
    "install_skill",
    "mcp_snippet",
    "render_init_yaml",
    "write_init_config",
]
