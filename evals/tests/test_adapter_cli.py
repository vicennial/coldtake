"""The adapter against coldtake's REAL providers, with fake CLI executables (no real CLI, login or model).

This is the process boundary: the fake executables record exactly what the CLI would have received on
stdin, so the sealed-answer canary is checked on the bytes that would leave the machine.
"""
import json

import pytest

fakes = pytest.importorskip("tests.fakes")   # coldtake's own fake CLI helpers (repository root)

from evals import run, tasks_io
from evals.adapters import coldtake_adapter as adapter

from .test_adapter import ALL, isolated, make_config, with_canary  # noqa: F401  (fixture re-export)


def cli_config(tmp_path):
    claude = fakes.FakeCLI(tmp_path / "fake-claude", "claude")
    codex = fakes.FakeCLI(tmp_path / "fake-codex", "codex")
    cfg = make_config(tmp_path)
    cfg["coldtake"].pop("providers")                       # the built-in providers, running the fake executables
    cfg["coldtake"]["binaries"] = {"claude": str(claude.path), "codex": str(codex.path)}
    cfg["coldtake"]["limits"] = {"attempt_seconds": 20, "job_seconds": 40, "judge_seconds": 20}
    return cfg, claude, codex


@pytest.mark.parametrize("cond,advisor_cli,idle_cli", [("blind_other", "codex", "claude"),
                                                        ("blind_same", "claude", "codex")])
def test_canary_is_absent_from_the_bytes_sent_to_the_advisor_cli(tmp_path, cond, advisor_cli, idle_cli):
    cfg, claude, codex = cli_config(tmp_path)
    clis = {"claude": claude, "codex": codex}
    clis[advisor_cli].script(advisor=[{"json": fakes.memo("my own explanation")}],
                             judge=[{"judge_auto": {"sealed": "conflict"}}])
    task = with_canary("claim-01")
    cell = run.build_matrix(cfg, [cond], ["anthropic"])[0]
    adv = adapter.advisor("anthropic", cfg)
    rec = run.execute_job(run.Job(task, cell, 0), adv, [], run_id="t", seed=0, framing="neutral")
    assert rec["response"]["error"] is None, rec["response"]
    advisor_calls = clis[advisor_cli].calls("advisor")
    assert len(advisor_calls) == 1
    assert "CANARY-7f3a91" not in advisor_calls[0]["stdin"]
    assert "COLDTAKE_MODE: alternate" in advisor_calls[0]["stdin"]
    assert "--output-schema" in advisor_calls[0]["argv"] or "--json-schema" in advisor_calls[0]["argv"]
    judge_calls = clis[advisor_cli].calls("judge")
    assert len(judge_calls) == 1 and "CANARY-7f3a91" in judge_calls[0]["stdin"]
    assert clis[idle_cli].calls() == []                    # the other family's CLI was never started
    assert rec["response"]["comparison"]["relation"] == "conflict"
    assert rec["response"]["meta"]["same_family"] is (cond == "blind_same")
    assert rec["response"]["meta"]["comparison"]["judge"]["family"] == rec["response"]["family"]


def test_review_through_the_codex_cli_shows_the_answer(tmp_path):
    cfg, claude, codex = cli_config(tmp_path)
    codex.script(advisor=[{"json": fakes.memo("VERDICT: sound\nfine")}])
    task = with_canary("claim-01")
    cell = run.build_matrix(cfg, ["cross_review"], ["anthropic"])[0]
    rec = run.execute_job(run.Job(task, cell, 0), adapter.advisor("anthropic", cfg), [], run_id="t", seed=0,
                          framing="neutral")
    assert rec["response"]["verdict"] == "sound"
    assert "CANARY-7f3a91" in codex.calls("advisor")[0]["stdin"]
    assert claude.calls() == []
