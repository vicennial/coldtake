"""Corpus audit tool, with the same in-process fake providers as the adapter tests."""
import hashlib
import json

import pytest

pytest.importorskip("coldtake")

from evals import audit, run, tasks_io
from evals.adapters import coldtake_adapter as adapter

from . import fake_coldtake as fake
from .test_adapter import ALL, isolated, make_config  # noqa: F401  (fixture re-export)

IDS = ["code-01", "dec-01", "claim-01", "code-ctl-1"]


def corpus_hash():
    return hashlib.sha256("".join(p.read_text("utf-8") for p in sorted(tasks_io.TASKS_DIR.rglob("*")) if p.is_file()).encode()).hexdigest()


def write_cfg(tmp_path):
    import yaml

    cfg = make_config(tmp_path)
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.safe_dump({k: cfg[k] for k in ("lead_families", "families", "graders", "coldtake")}))
    return p


def flagging(prompt, route):
    """Flag any task whose id appears in FLAG, say ok for the rest."""
    flag = "TASK dec-01 " in prompt or "TASK code-ctl-1 " in prompt
    head = "AUDIT: flag" if flag else "AUDIT: ok"
    return fake.memo(head + "\nreasoning", failure_modes=(["ground_truth: the rationale uses a fact not in the prompt"] if flag else []))


def test_material_holds_the_whole_answer_key():
    t = ALL["code-01"]
    m = audit.render_material(t, {"lead_answer": {"passed": False}})
    for needle in (t.prompt.strip()[:60], t.lead_answer.strip()[:60], t.reference_fix.strip()[:60], "Hidden test",
                   t.ground_truth["description"].strip()[:30], "rubric", "Local check"):
        assert needle in m
    d = audit.render_material(ALL["dec-01"])
    assert "Options" in d and "correct_option" in d


def test_audit_asks_a_reviewer_of_the_other_family_through_a_review_request(tmp_path):
    fake.BEHAVIOUR["advisor"] = flagging
    cfg = make_config(tmp_path)
    rt = adapter.runtime(cfg)
    rec = audit.audit_task(rt, ALL["dec-01"], "openai", "anthropic", with_checks=False)
    assert rec["status"] == "complete" and rec["verdict"] == "flag" and rec["error"] is None
    assert rec["issues"] == ["ground_truth: the rationale uses a fact not in the prompt"]
    assert rec["reviewer"] == {"family": "openai", "author_family": "anthropic", "provider": "codex", "model": None,
                               "same_family": False}
    (call,) = fake.calls("advisor")
    assert call["provider"] == "codex" and call["family"] == "openai"
    assert "COLDTAKE_MODE: review" in call["prompt"]
    assert "correct_option" in call["prompt"]                          # the reviewer sees the key
    assert rec["request_id"].startswith("au-dec-01-openai-") and rec["attempts"][0]["outcome"] == "ok"
    # same family is allowed but recorded
    rec2 = audit.audit_task(rt, ALL["dec-02"], "anthropic", "anthropic", with_checks=False)
    assert rec2["reviewer"]["same_family"] is True


def test_cli_writes_jsonl_and_a_summary_of_flagged_tasks_and_changes_nothing(tmp_path, capsys):
    fake.BEHAVIOUR["advisor"] = flagging
    cfgp = write_cfg(tmp_path)
    out, md = tmp_path / "audit.jsonl", tmp_path / "audit.md"
    before = corpus_hash()
    args = ["--config", str(cfgp), "--allow-real", "--out", str(out), "--summary", str(md), "--ids", ",".join(IDS),
            "--no-local-checks", "--concurrency", "2"]
    assert audit.main(args) == 0
    assert corpus_hash() == before                                     # nothing applied automatically
    recs = [json.loads(l) for l in out.read_text().splitlines()]
    assert sorted(r["task"]["id"] for r in recs) == sorted(IDS)
    assert {r["task"]["id"] for r in recs if r["verdict"] == "flag"} == {"dec-01", "code-ctl-1"}
    text = md.read_text()
    assert "### dec-01" in text and "### code-ctl-1" in text and "### claim-01" not in text
    assert "Nothing was changed" in text and "ground_truth: the rationale uses a fact" in text
    assert "claim-01" in text.split("## No problem found")[1]
    # resume asks for nothing; --resume re-asks only for tasks without a usable answer
    n = len(fake.calls("advisor"))
    assert audit.main(args + ["--resume"]) == 0
    assert len(fake.calls("advisor")) == n


def test_local_checks_run_the_hidden_tests_for_code_tasks(tmp_path):
    fake.BEHAVIOUR["advisor"] = flagging
    cfg = make_config(tmp_path)
    rec = audit.audit_task(adapter.runtime(cfg), ALL["code-01"], "openai", "anthropic")
    assert rec["local_checks"]["lead_answer"]["passed"] is False       # the planted defect fails the test
    assert rec["local_checks"]["reference_fix"]["passed"] is True
    assert "Local check" in fake.calls("advisor")[0]["prompt"]


def test_a_failed_review_is_reported_not_dropped(tmp_path):
    from coldtake.failures import ProviderFailure

    def down(prompt, route):
        raise ProviderFailure("provider_error", "terminal", "boom")
    fake.BEHAVIOUR["advisor"] = down
    cfg = make_config(tmp_path)
    rec = audit.audit_task(adapter.runtime(cfg), ALL["claim-01"], "openai", "anthropic", with_checks=False)
    assert rec["status"] == "failed" and rec["error"] and "verdict" not in rec
    md = audit.render_summary([rec], "openai", "anthropic")
    assert "Failed call" in md and "claim-01" in md


def test_missing_audit_line_is_repaired_once_and_then_recorded_as_none_not_guessed(tmp_path):
    fake.BEHAVIOUR["advisor"] = lambda p, r: fake.memo("looks fine")
    cfg = make_config(tmp_path)
    rec = audit.audit_task(adapter.runtime(cfg), ALL["claim-01"], "openai", "anthropic", with_checks=False)
    assert rec["status"] == "complete" and rec["verdict"] == "none" and rec["repaired"] is True
    assert len(fake.calls("advisor")) == 2                              # one repair, no more
    assert "previous reply to this audit had no verdict line" in fake.calls("advisor")[1]["prompt"]
    md = audit.render_summary([rec], "openai", "anthropic")
    assert "## No verdict line" in md and "## Failed call" not in md
    # a failed call is a different state
    assert "| failed call | 0 |" in md and "| no verdict line (after one repair) | 1 |" in md


def test_a_repair_that_supplies_the_verdict_is_used(tmp_path):
    answers = iter([fake.memo("answering the task instead"), fake.memo("AUDIT: flag\nreason", failure_modes=["prompt: x"])])
    fake.BEHAVIOUR["advisor"] = lambda p, r: next(answers)
    cfg = make_config(tmp_path)
    rec = audit.audit_task(adapter.runtime(cfg), ALL["claim-01"], "openai", "anthropic", with_checks=False)
    assert rec["verdict"] == "flag" and rec["repaired"] is True and rec["issues"] == ["prompt: x"]
    assert rec["first_request_id"] != rec["request_id"]


def test_a_reply_with_the_verdict_line_is_not_repaired(tmp_path):
    fake.BEHAVIOUR["advisor"] = lambda p, r: fake.memo("AUDIT: ok\nfine")
    cfg = make_config(tmp_path)
    rec = audit.audit_task(adapter.runtime(cfg), ALL["claim-01"], "openai", "anthropic", with_checks=False)
    assert rec["verdict"] == "ok" and rec["repaired"] is False and len(fake.calls("advisor")) == 1


def test_dry_run_and_real_flag(tmp_path, capsys):
    assert audit.main(["--dry-run"]) == 0
    assert "54 tasks" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        audit.main(["--out", str(tmp_path / "x.jsonl")])
    assert fake.calls() == []
