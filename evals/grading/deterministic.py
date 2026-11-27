"""Deterministic graders.

* ``run_hidden_tests``: run a task's hidden pytest file against candidate
  source (the lead's code, a reference fix, or an advisor-proposed fix).
* ``normalize_option`` / ``grade_option``: exact option-id matching for
  decision tasks.
* ``key_phrase_hits``: cheap lexical hint recorded next to LLM grades. It is
  never used as a verdict.

SECURITY NOTE: advisor-proposed code is untrusted. ``run_hidden_tests`` runs it
in a fresh temporary directory, in a subprocess with a minimal environment, CPU
and file-size limits and a hard timeout. That limits accidents; it is not a
sandbox. Run real evaluations inside a throwaway container or VM.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..tasks_io import Task

_FENCE = re.compile(r"```[a-zA-Z0-9_+-]*\n(.*?)```", re.S)


def extract_code(text: Optional[str]) -> Optional[str]:
    """Return source from a reply that may wrap it in a Markdown fence.

    If there are several fenced blocks the longest one is used (advisors often
    show a short diff snippet and then the full module).
    """
    if text is None:
        return None
    blocks = _FENCE.findall(text)
    if blocks:
        return max(blocks, key=len).strip("\n") + "\n"
    stripped = text.strip("\n")
    return stripped + "\n" if stripped.strip() else None


@dataclass
class HiddenTestResult:
    passed: bool
    returncode: int
    n_passed: int
    n_failed: int
    n_errors: int
    timed_out: bool
    failed_tests: List[str]
    output_tail: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_SUMMARY_COUNT = re.compile(r"(\d+) (passed|failed|error|errors)")


def _limits() -> None:  # pragma: no cover - runs in the child process
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
        resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024))
    except Exception:
        pass


def run_hidden_tests(
    test_path: Path,
    source: str,
    module: str = "solution",
    timeout_s: int = 30,
) -> HiddenTestResult:
    """Run ``test_path`` against ``source`` saved as ``<module>.py``."""
    tmp = Path(tempfile.mkdtemp(prefix="oh-eval-"))
    try:
        (tmp / ("%s.py" % module)).write_text(source, encoding="utf-8")
        shutil.copy(str(test_path), str(tmp / "test_hidden.py"))
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp),
            "TMPDIR": str(tmp),
            "LANG": "C.UTF-8",
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        }
        cmd = [sys.executable, "-m", "pytest", "-q", "-rf", "--no-header", "-p", "no:cacheprovider",
               "--rootdir", str(tmp), "-c", os.devnull, "test_hidden.py"]
        kwargs: Dict[str, Any] = {}
        if os.name == "posix":
            kwargs["start_new_session"] = True
            kwargs["preexec_fn"] = _limits
        proc = subprocess.Popen(cmd, cwd=str(tmp), env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, **kwargs)
        timed_out = False
        try:
            out, _ = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "posix":
                import signal

                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:  # pragma: no cover
                proc.kill()
            out, _ = proc.communicate()
        counts = {"passed": 0, "failed": 0, "error": 0}
        for n, what in _SUMMARY_COUNT.findall(out or ""):
            counts["error" if what.startswith("error") else what] += int(n)
        return HiddenTestResult(
            passed=(proc.returncode == 0 and not timed_out),
            returncode=proc.returncode,
            n_passed=counts["passed"],
            n_failed=counts["failed"],
            n_errors=counts["error"],
            timed_out=timed_out,
            failed_tests=sorted(set(re.findall(r"^FAILED test_hidden\.py::(\S+)", out or "", re.M))),
            output_tail=(out or "")[-1500:],
        )
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


def grade_fix(task: Task, source: Optional[str]) -> Optional[HiddenTestResult]:
    """Run the task's hidden tests on ``source`` (None when no source given)."""
    if source is None or task.hidden_test_path is None:
        return None
    det = task.grading["deterministic"]
    return run_hidden_tests(task.hidden_test_path, source, det.get("module", "solution"),
                            int(det.get("timeout_s", 30)))


_OPTION_RE = re.compile(r"^\s*(?:option\s+)?([a-d])\s*[.)]?\s*$", re.I)


def normalize_option(choice: Any, option_ids: Sequence[str]) -> Optional[str]:
    """Map a raw advisor choice to an option id, ``"none"`` or None (invalid)."""
    if choice is None or not isinstance(choice, str):
        return None
    text = choice.strip()
    if text.lower() == "none":
        return "none"
    m = _OPTION_RE.match(text)
    if m and m.group(1).upper() in option_ids:
        return m.group(1).upper()
    return None


@dataclass
class OptionGrade:
    valid: bool
    choice: Optional[str]
    is_correct: bool
    is_lead_choice: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def grade_option(task: Task, raw_choice: Any) -> OptionGrade:
    ids = [o["id"] for o in task.options or []]
    choice = normalize_option(raw_choice, ids)
    if choice is None:
        return OptionGrade(False, None, False, False)
    return OptionGrade(True, choice, choice == task.correct_option, choice == task.lead_option)


def key_phrase_hits(task: Task, text: str) -> List[str]:
    """Key phrases (case-insensitive) that occur in ``text``. A hint only."""
    low = (text or "").lower()
    return [p for p in task.grading.get("key_phrases", []) if p.lower() in low]
