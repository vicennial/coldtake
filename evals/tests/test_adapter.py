"""The coldtake adapter, end to end through the real coldtake engine with in-process fake providers.

No CLI and no model is called. ``fake_coldtake`` replaces the ``claude`` and ``codex`` providers
through coldtake's ``providers`` config mapping, so planning, prompts, validation, the sealed
comparison and the store are all real.
"""
import copy
import json
import threading

import pytest

pytest.importorskip("coldtake")

from evals import run, tasks_io
from evals.adapters import coldtake_adapter as adapter
from evals.advisor import build_request
from evals.grading.llm import build_grader_prompt
from evals.tasks_io import Task

from . import fake_coldtake as fake

ALL = {t.id: t for t in tasks_io.load_tasks()}
LEADS = ("anthropic", "openai")
PROVIDER = {"anthropic": "claude", "openai": "codex"}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("COLDTAKE_CONFIG", "COLDTAKE_STATE_DIR", "COLDTAKE_LEAD_FAMILY", "COLDTAKE_NAMESPACE",
                 "XDG_CONFIG_HOME", "XDG_STATE_HOME", "OPENROUTER_API_KEY", "CODEX_HOME", "CLAUDE_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    fake.reset()
    yield
    adapter.close_all()
    fake.reset()


def make_config(tmp_path, **oh):
    cfg = json.loads(json.dumps(run.DEFAULT_CONFIG))
    cfg["lead_families"] = list(LEADS)
    cfg["families"] = {"anthropic": {"model": None, "effort": None, "provider": "claude"},
                       "openai": {"model": None, "effort": None, "provider": "codex"}}
    cfg["graders"] = {"anthropic": {"model": None}, "openai": {"model": None}}
    cfg["coldtake"] = {"state_dir": str(tmp_path / "oh-state"),
                        "providers": {"claude": "evals.tests.fake_coldtake:FakeClaude",
                                      "codex": "evals.tests.fake_coldtake:FakeCodex"},
                        "concurrency_per_provider": 2, **oh}
    return cfg


def with_canary(task_id, marker="CANARY-7f3a91"):
    t = ALL[task_id]
    raw = copy.deepcopy(t.raw)
    if t.kind == "code":
        raw["lead_answer"] = raw["lead_answer"].rstrip("\n") + "\n# %s sealed marker\n" % marker
    else:
        raw["lead_answer"] = raw["lead_answer"].rstrip("\n") + "\n%s sealed marker.\n" % marker
    return Task(raw=raw, path=t.path)


def one(task, cell, cfg, graders=()):
    adv = adapter.advisor(cell.lead_family, cfg)
    return run.execute_job(run.Job(task, cell, 0), adv, list(graders), run_id="t", seed=0, framing="neutral")


# ------------------------------------------------------------------ condition -> routing
@pytest.mark.parametrize("lead", LEADS)
@pytest.mark.parametrize("cond,mode,same", [("self_review", "review", True), ("cross_review", "review", False),
                                            ("blind_same", "alternate", True), ("blind_other", "alternate", False)])
def test_condition_maps_to_mode_and_family(tmp_path, lead, cond, mode, same):
    cfg = make_config(tmp_path)
    cell = run.build_matrix(cfg, [cond], [lead])[0]
    other = "openai" if lead == "anthropic" else "anthropic"
    want = lead if same else other
    rec = one(ALL["dec-01"], cell, cfg)
    resp = rec["response"]
    assert resp["error"] is None, resp
    assert resp["family"] == want and resp["mode"] == mode
    assert resp["meta"]["same_family"] is same          # recorded by coldtake's memo record
    assert resp["meta"]["lead_family"] == lead and resp["meta"]["condition"] == cond
    adv_calls = fake.calls("advisor")
    assert len(adv_calls) == 1
    assert adv_calls[0]["family"] == want and adv_calls[0]["provider"] == PROVIDER[want]
    assert "COLDTAKE_MODE: %s" % mode in adv_calls[0]["prompt"]
    # nothing was sent to the other provider, and nothing paid
    assert fake.calls(provider=PROVIDER[other if same else lead]) == []
    assert resp["cost_usd"] == 0.0
    # attempt provenance: provider, model, latency, outcome
    att = resp["meta"]["attempts"]
    assert att and att[0]["provider"] == PROVIDER[want] and att[0]["outcome"] == "ok" and att[0]["seconds"] is not None
    assert resp["meta"]["request_ids"]["coldtake"].startswith("ev-dec-01-%s-%s-s0-" % (lead, cond))
    assert resp["latency_s"] >= 0


def test_never_falls_back_to_another_family(tmp_path):
    """If the forced family cannot answer, the cell is an error, not an answer from the other family."""
    from coldtake.failures import ProviderFailure

    def boom(prompt, route):
        raise ProviderFailure("usage_limit", "fallback", "limit reached")
    fake.BEHAVIOUR["advisor"] = boom
    cfg = make_config(tmp_path)
    cell = run.build_matrix(cfg, ["cross_review"], ["anthropic"])[0]     # advisor family: openai
    rec = one(ALL["dec-01"], cell, cfg)
    assert rec["response"]["error"] and "usage_limit" in rec["response"]["error"]
    assert {c["provider"] for c in fake.calls()} == {"codex"}
    assert rec["outcome"]["valid"] is False


def test_eval_resolver_refuses_use_outside_the_adapter():
    with pytest.raises(RuntimeError):
        adapter.eval_resolver(object())


def test_paid_routes_are_never_planned(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    cfg = make_config(tmp_path)
    rt = adapter.runtime(cfg)
    assert rt.config.openrouter.models == ()
    assert rt.providers["openrouter"].available() is False
    assert rt.config.budget.per_request_usd == 0.0


def test_engine_is_shared_per_state_dir(tmp_path):
    cfg = make_config(tmp_path)
    a, b = adapter.advisor("anthropic", cfg), adapter.advisor("openai", cfg)
    gs = adapter.graders("graders", cfg)
    assert a.rt is b.rt is gs[0].rt is gs[1].rt                       # a second Engine would raise
    assert [g.family for g in gs] == ["anthropic", "openai"]


# ------------------------------------------------------------------ sealed answer canary
@pytest.mark.parametrize("task_id", ["dec-01", "claim-01", "code-ctl-1"])
@pytest.mark.parametrize("lead", LEADS)
@pytest.mark.parametrize("cond", ["blind_same", "blind_other"])
def test_sealed_answer_never_reaches_a_blind_advisor_prompt(tmp_path, task_id, lead, cond):
    task = with_canary(task_id)
    cfg = make_config(tmp_path)
    cell = run.build_matrix(cfg, [cond], [lead])[0]
    rec = one(task, cell, cfg)
    assert rec["response"]["error"] is None, rec["response"]
    advisor_prompts = [c["prompt"] for c in fake.calls("advisor")]
    assert advisor_prompts and all("CANARY-7f3a91" not in p for p in advisor_prompts)
    # the lead's answer is absent from the advisor prompt in any form (first lines of it)
    first = [l for l in task.lead_answer.splitlines() if len(l.split()) > 6][0]
    assert all(first not in p for p in advisor_prompts)
    # and it is stored only as a hash in the record
    assert "CANARY-7f3a91" not in json.dumps(rec["request"]) and rec["request"]["sealed_sha256"]
    # the one place the sealed answer is rendered is the comparison judge (code and claim)
    judge_prompts = [c["prompt"] for c in fake.calls("judge")]
    if task.kind == "decision":
        assert judge_prompts == []                                    # options path: no model call
        assert rec["response"]["choice"] is not None
    else:
        assert len(judge_prompts) == 1 and "CANARY-7f3a91" in judge_prompts[0]
        assert rec["response"]["comparison"]["relation"] == "conflict"


@pytest.mark.parametrize("cond", ["self_review", "cross_review"])
def test_review_prompt_does_show_the_answer(tmp_path, cond):
    task = with_canary("claim-01")
    cfg = make_config(tmp_path)
    rec = one(task, run.build_matrix(cfg, [cond], ["anthropic"])[0], cfg)
    assert rec["response"]["verdict"] == "defect_found"
    assert "CANARY-7f3a91" in fake.calls("advisor")[0]["prompt"]
    assert fake.calls("judge") == []


def test_decision_sealed_choice_is_compared_by_option(tmp_path):
    task = ALL["dec-01"]
    cfg = make_config(tmp_path)
    cell = run.build_matrix(cfg, ["blind_other"], ["anthropic"])[0]
    fake.BEHAVIOUR["advisor"] = lambda p, r: fake.memo("x", choice=task.lead_option.lower(), choice_reason="r")
    rec = one(task, cell, cfg)
    assert rec["response"]["choice"] == task.lead_option          # upper-cased option id
    assert rec["response"]["meta"]["comparison"]["method"] == "options"
    assert rec["response"]["meta"]["comparison"]["sealed"]["sealed_choice"] == task.lead_option.lower()
    assert rec["outcome"]["label"] == "missed" and rec["outcome"]["reproduces_wrong"] is True


# ------------------------------------------------------------------ response mapping
def test_review_verdict_and_fix_are_extracted(tmp_path):
    cfg = make_config(tmp_path)
    fake.BEHAVIOUR["advisor"] = lambda p, r: fake.memo(
        "**VERDICT: defect found**\nThe bound is wrong.\n```python\ndef f():\n    return 1\n```",
        failure_modes=["off by one in f"])
    rec = one(ALL["code-01"], run.build_matrix(cfg, ["cross_review"], ["anthropic"])[0], cfg)
    r = rec["response"]
    assert r["verdict"] == "defect_found" and r["proposed_fix"].strip() == "def f():\n    return 1"
    assert "```" not in r["text"] and "VERDICT" not in r["text"]
    assert r["findings"][0]["summary"] == "off by one in f"
    fake.BEHAVIOUR["advisor"] = lambda p, r: fake.memo("VERDICT: sound\nNothing wrong.\n```python\nx=1\n```")
    cfg2 = make_config(tmp_path / "second")
    rec = one(ALL["code-ctl-1"], run.build_matrix(cfg2, ["self_review"], ["openai"])[0], cfg2)
    assert rec["response"]["verdict"] == "sound" and rec["response"]["proposed_fix"] is None
    assert rec["response"]["findings"] == []


def test_missing_verdict_is_recorded_as_invalid_not_guessed(tmp_path):
    cfg = make_config(tmp_path)
    fake.BEHAVIOUR["advisor"] = lambda p, r: fake.memo("It looks fine to me.")
    rec = one(ALL["claim-01"], run.build_matrix(cfg, ["cross_review"], ["anthropic"])[0], cfg)
    assert rec["response"]["verdict"] is None and rec["outcome"]["valid"] is False


def test_submission_carries_the_answer_only_where_it_belongs():
    from coldtake.schemas import Submission

    t = ALL["dec-02"]
    for cond_mode in ("review", "alternate"):
        req = build_request(t, cond_mode, "openai")
        data = adapter.to_submission(req)
        sub = Submission.model_validate(data)                      # the real coldtake packet validator
        packet, sealed = sub.split()
        if cond_mode == "review":
            assert packet.primary_answer == req.primary_answer and not sealed.present
        else:
            assert packet.primary_answer is None and sealed.answer == req.sealed_answer
            assert sealed.choice == req.sealed_choice.lower()
            assert req.sealed_answer not in json.dumps(packet.model_dump())


@pytest.mark.parametrize("task", list(ALL.values()), ids=lambda t: t.id)
def test_every_task_is_a_valid_coldtake_packet_in_both_modes(task):
    from coldtake.schemas import Submission

    for mode in ("review", "alternate"):
        Submission.model_validate(adapter.to_submission(build_request(task, mode, "anthropic")))


# ------------------------------------------------------------------ deterministic ids and resumability
def test_request_ids_are_deterministic_per_cell(tmp_path):
    t = ALL["claim-01"]
    a = build_request(t, "review", "openai", run_id="run-1", seed=1)
    b = build_request(t, "review", "openai", run_id="run-2", seed=99)
    assert a.request_id != b.request_id                               # the harness id depends on the run
    assert adapter.request_id_for(a, "anthropic") == adapter.request_id_for(b, "anthropic")
    ids = {adapter.request_id_for(build_request(t, m, f, sample=s), lead)
           for m in ("review", "alternate") for f in LEADS for s in (0, 1) for lead in LEADS}
    assert len(ids) == 2 * 2 * 2 * 2                                  # distinct per cell, sample and lead


def run_subset(cfg, tasks, out, **kw):
    cells = run.build_matrix(cfg)
    factory = lambda lead, c: adapter.advisor(lead, c)               # noqa: E731
    return run.run_matrix(tasks, cells, factory, adapter.graders("graders", cfg), cfg, out, run_id="r1",
                          log=lambda m: None, **kw)


def keys_in(out):
    return [json.loads(l)["key"] for l in out.read_text().splitlines()[1:]]


def test_resume_skips_finished_cells_and_makes_no_duplicate_calls(tmp_path):
    cfg = make_config(tmp_path)
    out = tmp_path / "res.jsonl"
    first = [ALL["dec-01"]]
    assert len(run_subset(cfg, first, out)) == 8                     # 8 cells, interrupted here
    assert len(fake.calls("advisor")) == 8
    both = [ALL["dec-01"], ALL["claim-01"]]
    recs = run_subset(cfg, both, out, resume=True, workers=3)
    assert len(recs) == 8                                            # only the new task's cells
    assert len(fake.calls("advisor")) == 16                          # never 24
    keys = keys_in(out)
    assert len(keys) == len(set(keys)) == 16
    assert {k.split("|")[0] for k in keys} == {"dec-01", "claim-01"}
    # a third, complete resume asks for nothing
    assert run_subset(cfg, both, out, resume=True) == []
    assert len(fake.calls("advisor")) == 16
    # the resumed run keeps the run id from the file header
    assert {json.loads(l)["run_id"] for l in out.read_text().splitlines()} == {"r1"}


def test_state_store_replays_finished_cells_in_a_new_process(tmp_path):
    cfg = make_config(tmp_path)
    t = ALL["claim-01"]
    cell = run.build_matrix(cfg, ["blind_other"], ["anthropic"])[0]
    first = one(t, cell, cfg)
    assert len(fake.calls("advisor")) == 1 and len(fake.calls("judge")) == 1
    adapter.close_all()                                               # "process restart": new Engine, same state dir
    second = one(t, cell, cfg)
    assert len(fake.calls("advisor")) == 1 and len(fake.calls("judge")) == 1   # replayed, no new call
    assert second["response"]["meta"]["request_ids"] == first["response"]["meta"]["request_ids"]
    assert second["response"]["comparison"] == first["response"]["comparison"]


def test_retry_errors_asks_again_under_a_new_request_id(tmp_path):
    from coldtake.failures import ProviderFailure

    cfg = make_config(tmp_path)
    out = tmp_path / "res.jsonl"
    state = {"fail": True}

    def flaky(prompt, route):
        if state["fail"]:
            raise ProviderFailure("provider_error", "terminal", "boom")
    fake.BEHAVIOUR["advisor"] = flaky
    t = [ALL["dec-01"]]
    cells = run.build_matrix(cfg, ["cross_review"], ["anthropic"])
    factory = lambda lead, c: adapter.advisor(lead, c)               # noqa: E731
    recs = run.run_matrix(t, cells, factory, [], cfg, out, run_id="r1", log=lambda m: None)
    assert recs[0]["response"]["error"]
    state["fail"] = False
    assert run.run_matrix(t, cells, factory, [], cfg, out, run_id="r1", resume=True, log=lambda m: None) == []
    recs = run.run_matrix(t, cells, factory, [], cfg, out, run_id="r1", resume=True, retry_errors=True,
                          log=lambda m: None)
    assert len(recs) == 1 and recs[0]["response"]["error"] is None
    assert recs[0]["response"]["meta"]["request_ids"]["coldtake"].endswith("-r1")
    assert len(fake.calls("advisor")) == 2


# ------------------------------------------------------------------ concurrency
def test_concurrency_is_limited_per_provider(tmp_path):
    cfg = make_config(tmp_path, concurrency_per_provider=2)
    fake.DELAY["seconds"] = 0.15
    out = tmp_path / "res.jsonl"
    tasks = [ALL[i] for i in ("dec-01", "dec-02", "dec-03")]
    cells = run.build_matrix(cfg)
    factory = lambda lead, c: adapter.advisor(lead, c)               # noqa: E731
    recs = run.run_matrix(tasks, cells, factory, [], cfg, out, run_id="r1", workers=8, log=lambda m: None)
    assert len(recs) == 24 and all(r["response"]["error"] is None for r in recs)
    assert fake.PEAK["claude"] == 2 and fake.PEAK["codex"] == 2      # parallel, but never above the limit
    assert len(set(keys_in(out))) == 24                               # the file stays consistent


def test_concurrency_one_serialises(tmp_path):
    cfg = make_config(tmp_path, concurrency_per_provider=1)
    fake.DELAY["seconds"] = 0.05
    cells = run.build_matrix(cfg, ["cross_review", "self_review"], ["anthropic"])
    factory = lambda lead, c: adapter.advisor(lead, c)               # noqa: E731
    run.run_matrix([ALL["dec-01"], ALL["dec-02"]], cells, factory, [], cfg, None, run_id="r1", workers=4,
                   log=lambda m: None)
    assert fake.PEAK["claude"] == 1 and fake.PEAK["codex"] == 1


# ------------------------------------------------------------------ grader
def graded_matrix(tmp_path, tasks):
    cfg = make_config(tmp_path)
    out = tmp_path / "res.jsonl"
    recs = run_subset(cfg, tasks, out)
    return cfg, recs


def test_grader_is_cross_family_and_blind_to_the_condition(tmp_path):
    cfg, recs = graded_matrix(tmp_path, [ALL["claim-01"], ALL["claim-ctl-1"]])
    grader_calls = fake.calls("grader")
    assert len(grader_calls) == len(recs) == 16
    for r in recs:
        g = r["grades"]["grader"]
        assert g["family"] != r["cell"]["advisor_family"]          # never the advisor's own family
        assert r["grades"]["llm"]["ok"]
    # the grader family that was called is the one recorded (one call per record)
    fams = sorted(c["family"] for c in grader_calls)
    assert fams == sorted(r["grades"]["grader"]["family"] for r in recs)
    # no condition, family or provider vocabulary reaches the grader
    banned = ["self_review", "cross_review", "blind_same", "blind_other", "anthropic", "openai",
              "claude", "codex", "same_family", "family_a", "condition"]
    for c in grader_calls:
        low = c["prompt"].lower()
        assert [w for w in banned if w in low] == []
    # identical advisor output, different condition => identical grader prompt
    by_task = {}
    for c in grader_calls:
        by_task.setdefault(c["prompt"], []).append(c["family"])
    review_prompts = [c["prompt"] for c in grader_calls if "shown the task and the lead's answer" in c["prompt"]]
    blind_prompts = [c["prompt"] for c in grader_calls if "shown ONLY the task" in c["prompt"]]
    assert len(review_prompts) == 8 and len(blind_prompts) == 8
    # self_review and cross_review of the same task give the same text to grade -> same prompt
    assert len(set(review_prompts)) == 2                               # one per task, not one per condition/family
    assert len(set(p for p in blind_prompts)) <= 4                    # blind: differ only by the model's (fake) output


def test_grader_prompt_is_exactly_the_harness_prompt(tmp_path):
    cfg = make_config(tmp_path)
    g = adapter.graders("graders", cfg)[0]
    t = ALL["claim-01"]
    resp = {"mode": "review", "family": "x", "text": "memo", "verdict": "defect_found", "findings": [], "choice": None,
            "proposed_fix": None, "comparison": None}
    prompt = build_grader_prompt(t, resp)
    out = g.complete(prompt)
    assert json.loads(out)["label"] == "identified"
    (call,) = fake.calls("grader")
    assert call["prompt"] == prompt                                    # nothing added, nothing removed
    assert call["family"] == "anthropic" and call["provider"] == "claude"


def test_grader_schema_is_provider_safe_and_replies_are_validated(tmp_path):
    schema = adapter.provider_grader_schema()
    text = json.dumps(schema)
    for word in ("maxLength", "maxItems", "minimum", "$schema", "description"):
        assert word not in text
    assert schema["additionalProperties"] is False and "label" in schema["required"]
    cfg = make_config(tmp_path)
    g = adapter.graders("graders", cfg)[0]
    fake.BEHAVIOUR["grader"] = lambda p, r: {"label": "identified"}   # missing fields: repaired once, then fails
    with pytest.raises(Exception):
        g.complete(build_grader_prompt(ALL["claim-01"], {"mode": "review", "family": "x", "text": "m"}))
    assert len(fake.calls("grader")) == 2                              # the original and one repair


def test_a_failed_grader_call_is_recorded_not_raised(tmp_path):
    from coldtake.failures import ProviderFailure

    def down(prompt, route):
        raise ProviderFailure("login_expired", "fallback", "login expired")
    fake.BEHAVIOUR["grader"] = down
    cfg, recs = graded_matrix(tmp_path, [ALL["claim-01"]])
    assert recs and all(r["grades"]["llm"]["ok"] is False for r in recs)
    assert all(r["outcome"]["graded"] is False for r in recs if not r["task"]["control"])    # counted, not guessed
    assert all("grader call failed" in r["grades"]["llm"]["error"] for r in recs)
    assert len(fake.calls("grader")) == len(recs)                      # one try each, no retry storm


# ------------------------------------------------------------------ dry run
def test_estimate_calls_for_the_full_matrix(tmp_path):
    cfg = make_config(tmp_path)
    tasks = list(ALL.values())
    cells = run.build_matrix(cfg)
    est = run.estimate_calls(tasks, cells, 1, cfg)
    assert est["per_provider"]["claude"] == {"advisor": 216, "judge": 72, "grader": 144, "expected": 432, "max": 1152}
    assert est["per_provider"]["codex"] == est["per_provider"]["claude"]
    assert est["expected"] == 864 and est["max"] == 2304
    assert run.estimate_calls(tasks, cells, 3, cfg)["expected"] == 3 * 864


def test_dry_run_prints_per_provider_counts(tmp_path, capsys):
    import yaml

    cfg = make_config(tmp_path)
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.safe_dump({k: cfg[k] for k in ("lead_families", "families", "graders", "coldtake")}))
    assert run.main(["--dry-run", "--config", str(p), "--samples", "2"]) == 0
    out = capsys.readouterr().out
    assert "claude: advisor 432, judge 144, grader 288 = 864 expected, up to 2304 with repairs" in out
    assert "codex: advisor 432" in out
    assert "total: 1728 expected, up to 4608 with repairs (no paid calls)" in out
    assert fake.calls() == []                                          # a dry run calls nothing


# ------------------------------------------------------------------ the runner's command line
def test_command_line_run_with_the_real_adapter_and_resume(tmp_path, capsys):
    import yaml

    cfg = make_config(tmp_path)
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.safe_dump({k: cfg[k] for k in ("lead_families", "families", "graders", "coldtake")}))
    out = tmp_path / "res.jsonl"
    argv = ["--allow-real", "--config", str(p), "--advisor", "evals.adapters.coldtake_adapter:advisor",
            "--grader", "evals.adapters.coldtake_adapter:graders", "--ids", "dec-01,claim-01", "--out", str(out)]
    assert run.main(argv) == 0
    lines = out.read_text().splitlines()
    assert len(lines) == 1 + 16
    assert len(fake.calls("advisor")) == 16 and len(fake.calls("judge")) == 4 and len(fake.calls("grader")) == 8
    assert run.main(argv + ["--resume"]) == 0
    assert len(out.read_text().splitlines()) == 17 and len(fake.calls("advisor")) == 16
    with pytest.raises(SystemExit):                       # no --allow-real: refuses
        run.main([a for a in argv if a != "--allow-real"])


# ---------- decision packets carry the options, so the memo has a `choice`
DECISIONS = [t for t in ALL.values() if t.kind == "decision"]


@pytest.mark.parametrize("task", DECISIONS, ids=lambda t: t.id)
@pytest.mark.parametrize("mode", ["review", "alternate"])
def test_decision_packets_carry_options_and_coldtake_asks_for_a_choice(task, mode):
    """Path: task.options -> AdvisorRequest.options -> to_submission()["options"] -> coldtake's Packet ->
    ChoiceMemo schema (``choice`` required, one of the option ids) -> map_response reads memo["choice"]."""
    from coldtake.contracts import CoreContract

    cell_mode = "review" if mode == "review" else "alternate"
    req = build_request(task, cell_mode, "openai")
    data = adapter.to_submission(req)
    assert [o["id"] for o in data["options"]] == [o["id"] for o in task.options]
    assert len(data["options"]) >= 3
    contract = CoreContract()
    parsed = contract.parse(data, None)                       # the real coldtake packet validation
    assert parsed.packet.options and parsed.packet.option_ids() == [o["id"].lower() for o in task.options]
    schema = contract.memo_schema(parsed.packet)
    assert "choice" in schema["properties"] and "choice" in schema["required"]
    if mode == "alternate":
        assert data["sealed_choice"] == task.lead_option.lower()   # compared by option id, no model call
    else:
        assert "sealed_choice" not in data and data["primary_answer"]


@pytest.mark.parametrize("task", DECISIONS, ids=lambda t: t.id)
def test_every_decision_task_yields_a_choice_through_the_engine(task, tmp_path):
    cfg = make_config(tmp_path)
    cell = run.build_matrix(cfg, ["cross_review"], ["anthropic"])[0]
    rec = one(task, cell, cfg)
    assert rec["response"]["error"] is None, rec["response"]
    assert rec["response"]["choice"] in {o["id"] for o in task.options}
    assert rec["outcome"]["valid"] is True
