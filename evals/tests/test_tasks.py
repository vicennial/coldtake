"""Validate every task file: schema, semantics, hidden tests, hygiene."""
import re
from collections import Counter
from pathlib import Path

import pytest

from evals import tasks_io
from evals.advisor import assert_blind, build_request, longest_shared_word_span
from evals.grading.deterministic import grade_fix

TASKS = tasks_io.load_tasks()
EVALS = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def tasks():
    return TASKS


def by_kind(kind, control=None):
    return [t for t in TASKS if t.kind == kind and (control is None or t.control == control)]


def test_counts_by_kind():
    for kind in tasks_io.KINDS:
        assert len(by_kind(kind, False)) == 12, kind
        assert len(by_kind(kind, True)) == 6, kind
    assert len(TASKS) == 54
    assert sum(t.control for t in TASKS) == 18
    assert len({t.id for t in TASKS}) == 54


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
def test_schema_and_semantics(task):
    assert tasks_io.schema_errors(task.raw) == []
    assert tasks_io.semantic_errors(task) == []


@pytest.mark.parametrize("task", by_kind("code"), ids=lambda t: t.id)
def test_code_hidden_tests(task):
    lead = grade_fix(task, task.lead_answer)
    assert lead.timed_out is False
    if task.control:
        assert lead.passed, "hidden tests must pass on a correct control solution:\n" + lead.output_tail
        assert lead.n_passed > 0
    else:
        assert not lead.passed, "hidden tests must FAIL on the planted defect"
        assert lead.n_failed >= 1 and lead.n_errors == 0, lead.output_tail
        ref = grade_fix(task, task.reference_fix)
        assert ref.passed, "hidden tests must PASS on the reference fix:\n" + ref.output_tail
        assert ref.n_failed == 0 and ref.n_passed >= lead.n_passed


@pytest.mark.parametrize("task", by_kind("code", False), ids=lambda t: t.id)
def test_reference_fix_is_a_small_change(task):
    """One planted defect: the fix should touch only a few lines."""
    import difflib

    a = task.lead_answer.splitlines()
    b = task.reference_fix.splitlines()
    changed = [l for l in difflib.unified_diff(a, b, lineterm="", n=0) if l[:1] in "+-" and l[:3] not in ("+++", "---")]
    assert 1 <= len(changed) <= 14, changed


def test_decision_option_positions_are_balanced():
    """The correct and the lead's wrong option letters must not be guessable."""
    correct = Counter(t.correct_option for t in by_kind("decision", False))
    lead = Counter(t.lead_option for t in by_kind("decision", False))
    assert max(correct.values()) <= 4, correct
    assert max(lead.values()) <= 4, lead
    assert len(correct) >= 3 and len(lead) >= 3


def test_decision_lead_answers_reach_the_lead_option_on_the_first_line():
    for t in by_kind("decision"):
        first = t.lead_answer.strip().splitlines()[0]
        assert "Option %s" % t.lead_option in first, t.id


@pytest.mark.parametrize("task", by_kind("decision"), ids=lambda t: t.id)
def test_decision_rubric_labels_are_exclusive_and_match_the_ground_truth(task):
    """Decision tasks are graded by the final option alone, so each label names exactly one rule."""
    rubric = task.grading["rubric"]
    assert "final option" in rubric
    if task.control:
        assert "clean: the final recommendation is %s." % task.correct_option in rubric
        assert "false_alarm: the final recommendation is any other option" in rubric
    else:
        assert "identified: the final recommendation is %s." % task.correct_option in rubric
        assert "missed: the final recommendation is %s, the lead's option." % task.lead_option in rubric
        assert "partial: the final recommendation is any other option" in rubric


def test_corpus_version_is_part_of_the_hash():
    assert tasks_io.CORPUS_VERSION != "1"
    assert tasks_io.corpus_sha256(TASKS)


def test_claim_word_limit_and_span():
    for t in by_kind("claim"):
        assert len(t.lead_answer.split()) <= 250, t.id
        if not t.control:
            assert tasks_io.normalize_ws(t.ground_truth["error_span"]) in tasks_io.normalize_ws(t.lead_answer)


def test_lead_code_is_at_most_80_lines():
    for t in by_kind("code"):
        assert len(t.lead_answer.rstrip("\n").split("\n")) <= 80, t.id


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
def test_blind_requests_do_not_leak_the_lead_answer(task):
    req = build_request(task, "alternate", "family_b")
    assert req.primary_answer is None
    assert_blind(req, task)
    assert longest_shared_word_span(str(req.visible_payload()), task.lead_answer) < 12


def test_defect_types_cover_the_requested_classes():
    kinds = Counter(t.ground_truth["defect_type"] for t in by_kind("code", False))
    assert set(kinds) == {"off_by_one", "wrong_boundary", "race", "unhandled_error",
                          "path_traversal", "injection", "wrong_default"}
    assert kinds["race"] >= 2 and kinds["path_traversal"] >= 2


# Words that must not appear in a public tree. Built from pieces so this file
# does not match its own scan.
BANNED = [
    "doub" + "ld", "agent" + "box", "her" + "mes", "g" + "brain", "ak" + "hil",
    "/ho" + "me/", "/Us" + "ers/", "fs" + "n1", "claude-" + "session", "claude.ai" + "/code",
]


def _text_files():
    for p in EVALS.rglob("*"):
        if p.is_file() and p.suffix in {".py", ".yaml", ".yml", ".md", ".json", ".ini", ".txt"} \
                and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts:
            yield p


def test_public_tree_hygiene():
    hits = []
    for p in _text_files():
        low = p.read_text(encoding="utf-8").lower()
        for word in BANNED:
            if word.lower() in low:
                hits.append((str(p.relative_to(EVALS)), word))
        if re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", low):
            hits.append((str(p.relative_to(EVALS)), "ip-like literal"))
    assert hits == []
