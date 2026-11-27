"""Matrix, blindness, provenance and end-to-end behaviour with fake advisors."""
import copy
import json

import pytest

from evals import report, run, tasks_io
from evals.advisor import AdvisorResponse, BlindnessViolation, FakeAdvisor, build_request
from evals.grading.llm import FakeGrader
from evals.tasks_io import Task

ALL = tasks_io.load_tasks()
SUBSET_CODE = {"code-01", "code-04", "code-ctl-1"}
SUBSET = [t for t in ALL if t.kind != "code" or t.id in SUBSET_CODE]
CONFIG = run.load_config(None)


def run_fake(tmp_path, profile, tasks=SUBSET, samples=1, seed=0, **kw):
    out = tmp_path / "out.jsonl"
    cells = run.build_matrix(CONFIG)
    factory = lambda lead, cfg: FakeAdvisor(list(tasks), lead_family=lead, profile=profile, seed=seed)
    graders = [FakeGrader("family_a"), FakeGrader("family_b")]
    recs = run.run_matrix(tasks, cells, factory, graders, CONFIG, out, samples=samples, seed=seed,
                          run_id="test-run", log=lambda m: None, **kw)
    return recs, out


# ---------- matrix ----------
def test_matrix_is_four_conditions_by_lead_family():
    cells = run.build_matrix(CONFIG)
    assert len(cells) == 8
    by = {c.name: c for c in cells}
    assert by["family_a/self_review"].advisor_family == "family_a"
    assert by["family_a/cross_review"].advisor_family == "family_b"
    assert by["family_a/blind_same"].mode == "alternate" and by["family_a/blind_same"].advisor_family == "family_a"
    assert by["family_b/blind_other"].advisor_family == "family_a"
    assert {c.mode for c in cells if c.condition.endswith("review")} == {"review"}
    sub = run.build_matrix(CONFIG, conditions=["blind_same"], lead_families=["family_b"])
    assert [c.name for c in sub] == ["family_b/blind_same"]


def test_config_validation(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("lead_families: [x]\nfamilies: {x: {}}\n")
    with pytest.raises(ValueError):
        run.load_config(str(p))


def test_dry_run_prints_the_plan_and_needs_no_output_path(capsys):
    assert run.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "tasks: 54 (18 controls)" in out and "advisor calls: 432" in out


def test_real_advisors_need_explicit_flag():
    with pytest.raises(SystemExit):
        run.make_advisor_factory("some.module:factory", ALL, allow_real=False)
    with pytest.raises(SystemExit):
        run.make_graders("some.module:factory", CONFIG, allow_real=False)
    assert run.make_graders("none", CONFIG, False) == []


# ---------- blindness ----------
def canary_task():
    raw = copy.deepcopy(next(t for t in ALL if t.id == "dec-01").raw)
    raw["lead_answer"] = raw["lead_answer"] + "\nCANARY-7f3a91 sealed marker."
    t = Task(raw=raw, path=next(t for t in ALL if t.id == "dec-01").path)
    return t


def test_sealed_answer_never_in_blind_payload_or_record(tmp_path):
    t = canary_task()
    req = build_request(t, "alternate", "family_b")
    assert req.sealed_answer and "CANARY-7f3a91" in req.sealed_answer
    assert "CANARY-7f3a91" not in json.dumps(req.visible_payload())
    assert req.sealed_choice == "C"
    assert "CANARY-7f3a91" not in json.dumps(req.provenance())      # only the hash is recorded
    assert req.provenance()["sealed_sha256"]
    fake = FakeAdvisor([t], lead_family="family_a")
    rec = run.execute_job(run.Job(t, run.build_matrix(CONFIG, ["blind_other"], ["family_a"])[0], 0), fake, [],
                          run_id="x", seed=0, framing="neutral")
    assert "CANARY-7f3a91" not in json.dumps(rec)
    assert all("CANARY-7f3a91" not in json.dumps(v) for v in fake.visible_seen)
    # review mode legitimately shows the answer
    rev = build_request(t, "review", "family_b")
    assert "CANARY-7f3a91" in json.dumps(rev.visible_payload()) and rev.sealed_answer is None


def test_leaky_blind_request_fails_closed():
    t = next(t for t in ALL if t.id == "dec-01")
    leaky_raw = copy.deepcopy(t.raw)
    leaky_raw["prompt"] = leaky_raw["prompt"] + "\n" + leaky_raw["lead_answer"]
    leaky = Task(raw=leaky_raw, path=t.path)
    cell = run.build_matrix(CONFIG, ["blind_other"], ["family_a"])[0]
    called = []

    class Spy:
        def ask(self, request):
            called.append(request)
            return AdvisorResponse(mode="alternate", family="x")

    with pytest.raises(BlindnessViolation):
        run.execute_job(run.Job(leaky, cell, 0), Spy(), [], run_id="x", seed=0, framing="neutral")
    assert called == []


def test_blind_requests_are_blind_for_every_task():
    for t in ALL:
        req = build_request(t, "alternate", "family_a")
        assert req.primary_answer is None and "primary_answer" not in req.visible_payload()
        assert (req.sealed_choice is not None) == (t.kind == "decision")


# ---------- end to end ----------
def test_oracle_profile_gives_perfect_scores(tmp_path):
    recs, out = run_fake(tmp_path, {"*": {"p_detect": 1.0, "p_alarm": 0.0}})
    assert len(recs) == len(SUBSET) * 8
    assert all(r["outcome"]["graded"] and r["outcome"]["valid"] for r in recs)
    rep = report.build_report(recs)
    for cond in report.CONDITION_ORDER:
        row = [r for r in rep["cells"] if (r["metric"], r["scope"], r["condition"], r["kind"]) == ("recall_strict", "pooled", cond, "all")][0]
        assert row["k"] == row["n"] == 2 * 26                     # 26 defect tasks x 2 lead families
        fa = [r for r in rep["cells"] if (r["metric"], r["scope"], r["condition"]) == ("false_alarm", "pooled", cond)][0]
        assert (fa["k"], fa["n"]) == (0, 2 * sum(t.control for t in SUBSET))
    assert not [r for r in rep["cells"] if r["metric"] == "wrong_persist" and r["k"]]


def test_anchored_reviewers_vs_independent_blind_advisors(tmp_path):
    profile = {"self_review": {"p_detect": 0.0, "p_alarm": 1.0},
               "cross_review": {"p_detect": 1.0, "p_alarm": 0.0},
               "blind_same": {"p_detect": 0.0, "p_alarm": 0.0},
               "blind_other": {"p_detect": 1.0, "p_alarm": 0.0}}
    recs, _ = run_fake(tmp_path, profile)
    rep = report.build_report(recs)

    def get(metric, cond, scope="pooled", kind="all"):
        return [r for r in rep["cells"] if (r["metric"], r["scope"], r["condition"], r["kind"]) == (metric, scope, cond, kind)][0]

    assert get("recall_strict", "self_review")["k"] == 0
    assert get("recall_strict", "cross_review")["k"] == get("recall_strict", "cross_review")["n"]
    assert get("recall_strict", "blind_same")["k"] == 0
    assert get("wrong_persist", "self_review")["k"] == get("wrong_persist", "self_review")["n"] == 52   # fully anchored
    assert get("wrong_persist", "cross_review")["k"] == 0
    assert get("wrong_persist", "blind_same")["k"] == get("wrong_persist", "blind_same")["n"]            # same mistake, unseen
    assert get("false_alarm", "self_review")["k"] == get("false_alarm", "self_review")["n"] == 2 * sum(t.control for t in SUBSET)
    assert get("false_alarm", "cross_review")["k"] == 0
    p = [p for p in rep["paired"] if (p["metric"], p["a"], p["b"], p["scope"]) == ("recall_strict", "self_review", "cross_review", "pooled")][0]
    assert (p["only_a"], p["only_b"], p["n_pairs"]) == (0, 52, 52)
    assert p["p_mcnemar_exact"] < 1e-12


def test_partial_label_path(tmp_path):
    recs, _ = run_fake(tmp_path, {"*": {"p_detect": 0.0, "p_partial": 1.0}}, tasks=[t for t in SUBSET if t.kind != "code"])
    labels = {r["outcome"]["label"] for r in recs if not r["task"]["control"]}
    assert labels == {"partial"}


def test_record_provenance(tmp_path):
    recs, out = run_fake(tmp_path, {"*": {"p_detect": 1.0}}, tasks=[t for t in SUBSET if t.kind == "claim"][:2])
    lines = [json.loads(l) for l in out.read_text().splitlines()]
    header, rec = lines[0], lines[1]
    assert header["schema"].endswith("/header") and header["harness"]["version"] == run.HARNESS_VERSION
    assert header["corpus_sha256"] and header["seed"] == 0
    for field in ("schema", "run_id", "key", "task", "cell", "sample", "seed", "request", "visible_payload", "response",
                  "grades", "outcome", "provenance"):
        assert field in rec, field
    assert rec["request"]["sha256"] and rec["request"]["request_id"].startswith("eval-")
    assert rec["task"]["sha256"] and rec["cell"]["advisor_family"] in ("family_a", "family_b")
    assert rec["grades"]["grader"]["family"] != rec["cell"]["advisor_family"]   # cross-family grader
    assert rec["grades"]["llm"]["ok"] and rec["grades"]["llm"]["prompt_sha256"]
    assert "[fake:detect]" in rec["response"]["raw"]


def test_deterministic_given_seed_and_resume(tmp_path):
    prof = {"*": {"p_detect": 0.5, "p_alarm": 0.5}}
    tasks = [t for t in SUBSET if t.kind != "code"]
    (tmp_path / "x").mkdir()
    (tmp_path / "y").mkdir()
    a, _ = run_fake(tmp_path / "x", prof, tasks=tasks, seed=3)
    b, out_b = run_fake(tmp_path / "y", prof, tasks=tasks, seed=3)
    pick = lambda recs: [(r["key"], r["outcome"]["label"], r["request"]["sha256"]) for r in recs]
    assert pick(a) == pick(b)
    # resume appends nothing when everything is done
    cells = run.build_matrix(CONFIG)
    factory = lambda lead, cfg: FakeAdvisor(tasks, lead_family=lead, profile=prof, seed=3)
    again = run.run_matrix(tasks, cells, factory, [FakeGrader("family_a"), FakeGrader("family_b")], CONFIG, out_b,
                           seed=3, run_id="test-run", resume=True, log=lambda m: None)
    assert again == []
    assert len([l for l in out_b.read_text().splitlines() if l.strip()]) == len(b) + 1


def _resume(tasks, out, **kw):
    cells = run.build_matrix(CONFIG)
    factory = lambda lead, cfg: FakeAdvisor(tasks, lead_family=lead, profile={"*": {"p_detect": 0.5, "p_alarm": 0.5}}, seed=3)
    return run.run_matrix(tasks, cells, factory, [FakeGrader("family_a"), FakeGrader("family_b")], CONFIG, out,
                          seed=3, run_id="test-run", resume=True, log=lambda m: None, **kw)


def test_resume_refuses_a_results_file_from_another_corpus_version(tmp_path):
    tasks = [t for t in SUBSET if t.kind != "code"]
    (tmp_path / "x").mkdir()
    _, out = run_fake(tmp_path / "x", {"*": {"p_detect": 0.5, "p_alarm": 0.5}}, tasks=tasks, seed=3)
    header = json.loads(out.read_text().splitlines()[0])
    assert header["corpus_version"] == tasks_io.CORPUS_VERSION
    lines = out.read_text().splitlines()
    del header["corpus_version"]          # what a file written before the version existed looks like
    out.write_text("\n".join([json.dumps(header)] + lines[1:]) + "\n")
    with pytest.raises(ValueError, match="corpus version"):
        _resume(tasks, out)


def test_resume_refuses_results_recorded_for_a_changed_task(tmp_path):
    tasks = [t for t in SUBSET if t.kind != "code"]
    (tmp_path / "x").mkdir()
    _, out = run_fake(tmp_path / "x", {"*": {"p_detect": 0.5, "p_alarm": 0.5}}, tasks=tasks, seed=3)
    lines = out.read_text().splitlines()
    rec = json.loads(lines[1])
    rec["task"]["sha256"] = "0" * 64      # the task text or its hidden test changed since this was recorded
    out.write_text("\n".join([lines[0], json.dumps(rec)] + lines[2:]) + "\n")
    with pytest.raises(ValueError, match=rec["task"]["id"]):
        _resume(tasks, out)


def test_corpus_hash_depends_on_the_corpus_version(monkeypatch):
    before = tasks_io.corpus_sha256(SUBSET)
    monkeypatch.setattr(tasks_io, "CORPUS_VERSION", "other")
    assert tasks_io.corpus_sha256(SUBSET) != before


def test_cost_cap_stops_the_run(tmp_path):
    tasks = [t for t in SUBSET if t.kind == "decision"]

    class Pricey:
        def ask(self, request):
            return AdvisorResponse(mode=request.mode, family=request.advisor_family, choice="A", cost_usd=1.0)

    cells = run.build_matrix(CONFIG)
    recs = run.run_matrix(tasks, cells, lambda l, c: Pricey(), [], CONFIG, None, seed=0, max_cost_usd=2.5,
                          log=lambda m: None)
    assert len(recs) == 3


def test_adapter_exception_becomes_an_invalid_response(tmp_path):
    tasks = [t for t in SUBSET if t.kind == "decision"][:2]

    class Boom:
        def ask(self, request):
            raise RuntimeError("provider exploded")

    recs = run.run_matrix(tasks, run.build_matrix(CONFIG, ["self_review"], ["family_a"]), lambda l, c: Boom(), [], CONFIG,
                          None, run_id="x", log=lambda m: None)
    assert len(recs) == 2
    assert all(r["outcome"]["valid"] is False and "RuntimeError" in r["response"]["error"] for r in recs)


def test_no_grader_leaves_claims_ungraded(tmp_path):
    tasks = [t for t in SUBSET if t.kind == "claim"][:3]
    recs = run.run_matrix(tasks, run.build_matrix(CONFIG, ["cross_review"], ["family_a"]),
                          lambda l, c: FakeAdvisor(tasks, l), [], CONFIG, None, run_id="x", log=lambda m: None)
    assert all(not r["outcome"]["graded"] for r in recs)
    assert report.build_report(recs)["meta"]["n_ungraded"] == 3


def test_code_fix_is_verified_by_hidden_tests(tmp_path):
    code = [t for t in SUBSET if t.id in ("code-01", "code-ctl-1")]
    recs = run.run_matrix(code, run.build_matrix(CONFIG, ["cross_review", "blind_other"], ["family_a"]),
                          lambda l, c: FakeAdvisor(code, l, profile={"*": {"p_detect": 1.0, "p_alarm": 0.0}}),
                          [FakeGrader("family_a"), FakeGrader("family_b")], CONFIG, None, run_id="x", log=lambda m: None)
    by = {(r["task"]["id"], r["cell"]["condition"]): r for r in recs}
    d = by[("code-01", "cross_review")]
    assert d["grades"]["fix_test"]["passed"] is True and d["outcome"]["label"] == "identified"
    b = by[("code-01", "blind_other")]
    assert b["outcome"]["independent_correct"] is True and b["outcome"]["label"] == "identified"
    assert by[("code-ctl-1", "blind_other")]["outcome"]["false_alarm"] is False
