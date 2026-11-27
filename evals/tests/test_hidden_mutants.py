"""Adequacy of the hidden tests: each mutant below is a plausible wrong solution that an earlier
version of a test suite let through (see evals/AUDIT-FIXES.md). Every mutant must fail the suite,
and the concurrency suites must give the same verdict on every run."""
import os

import pytest

from evals import tasks_io
from evals.grading.deterministic import grade_fix

TASKS = {t.id: t for t in tasks_io.load_tasks(kinds=["code"])}


def base(task_id):
    t = TASKS[task_id]
    return t.lead_answer if t.control else t.reference_fix


def _unindent(text):
    """Patterns below are written with two extra leading spaces (as they appear in the YAML)."""
    return "\n".join(line[2:] if line.startswith("  ") else line for line in text.split("\n"))


def swap(src, old, new):
    old, new = _unindent(old), _unindent(new)
    assert src.count(old) == 1, (old, src.count(old))
    return src.replace(old, new)


def _code_01_without_page_fields(s):
    return swap(s, '          "page": page,\n          "per_page": per_page,\n', "")


def _code_01_wrong_total_items(s):
    return swap(s, '"total_items": total_items,', '"total_items": total_items + 0 * page - 1,')


def _code_03_cutoff_arithmetic(s):
    s = swap(s, "while self._events and now - self._events[0] >= self.window:",
             "cutoff = now - self.window\n          while self._events and self._events[0] <= cutoff:")
    return s


def _code_05_lock_around_everything(s):
    return swap(s, "          if self._needs_refresh():\n              with self._lock:\n                  if self._needs_refresh():  # another thread may have refreshed\n                      token, ttl = self._refresh()\n                      self._token = token\n                      self._expires_at = self._clock() + ttl\n",
                "          with self._lock:\n              if self._needs_refresh():\n                  token, ttl = self._refresh()\n                  self._token = token\n                  self._expires_at = self._clock() + ttl\n")


def _code_05_expiry_before_refresh(s):
    return swap(s, "                      token, ttl = self._refresh()\n                      self._token = token\n                      self._expires_at = self._clock() + ttl\n",
                "                      started = self._clock()\n                      token, ttl = self._refresh()\n                      self._token = token\n                      self._expires_at = started + ttl\n")


def _code_06_no_recursion_handler(s):
    return swap(s, "except (OSError, ValueError, RecursionError):", "except (OSError, ValueError):")


def _code_06_no_permission_handler(s):
    return swap(s, "except (OSError, ValueError, RecursionError):",
                "except (FileNotFoundError, IsADirectoryError, ValueError, RecursionError):")


def _code_08_root_doubles_the_separator(s):
    return swap(s, "if os.path.commonpath([base_real, candidate]) != base_real:",
                "if candidate != base_real and not candidate.startswith(base_real + os.sep):")


def _code_09_touches_the_file_system_first(s):
    return swap(s, '      if not filename:\n          raise ValueError("empty file name")\n',
                '      os.makedirs(root, exist_ok=True)\n      if not filename:\n          raise ValueError("empty file name")\n')


def _code_09_backslash_not_a_separator(s):
    return swap(s, 'normalized = filename.replace("\\\\", "/")', "normalized = filename")


def _code_10_strict_age(s):
    return swap(s, "age >= ?", "age > ?")


def _code_10_like(s):
    return swap(s, "name = ? COLLATE NOCASE", "name LIKE ? COLLATE NOCASE")


def _code_10_order_by_age(s):
    return swap(s, "ORDER BY id", "ORDER BY age DESC")


def _ctl_1_rejects_every_colon(s):
    return swap(s, 'if len(member) > 1 and member[1] == ":" and member[0].isascii() and member[0].isalpha():',
                'if ":" in member:')


def _ctl_2_failed_consume_spends_tokens(s):
    return swap(s, "          if self._tokens >= n:\n              self._tokens -= n\n              return True\n          return False",
                "          ok = self._tokens >= n\n          self._tokens -= n\n          return ok")


def _ctl_4_no_lock_in_hit(s):
    return swap(s, "      def hit(self, key: str) -> bool:\n          \"\"\"Count one hit for ``key``; False if it is already at the limit.\"\"\"\n          with self._lock:\n",
                "      def hit(self, key: str) -> bool:\n          \"\"\"Count one hit for ``key``; False if it is already at the limit.\"\"\"\n          if True:\n")


def _ctl_5_no_nul_check(s):
    return swap(s, '      if not isinstance(term, str) or "\\x00" in term:\n          raise ValueError("term must be a string without NUL characters")\n', "")


def _ctl_5_backslash_not_escaped(s):
    return swap(s, 'term.replace("\\\\", "\\\\\\\\").replace("%"', 'term.replace("%"')


def _ctl_5_wildcards_not_escaped(s):
    return swap(s, '.replace("%", "\\\\%").replace("_", "\\\\_")', "")


def _ctl_5_default_limit_100(s):
    return swap(s, "limit: int = 20", "limit: int = 100")


def _ctl_5_isbn_not_searchable(s):
    return swap(s, '_COLUMNS = ("title", "author", "isbn", "year")', '_COLUMNS = ("title", "author", "year")')


def _ctl_6_unbounded_digits(s):
    return swap(s, "([0-9]{1,9})", "([0-9]+)")


def _code_01_has_next_only_on_page_one(s):
    return swap(s, '"has_next": page < total_pages,', '"has_next": page == 1 and page < total_pages,')


def _code_03_evicts_at_most_one(s):
    return swap(s, "while self._events and now - self._events[0] >= self.window:", "if self._events and now - self._events[0] >= self.window:")


def _code_03_remaining_zero_when_empty(s):
    return swap(s, "return self.limit - len(self._events)", "return self.limit - len(self._events) if self._events else 0")


def _code_06_returns_the_shared_defaults(s):
    return swap(s, "except (OSError, ValueError, RecursionError):\n          return settings", "except (OSError, ValueError, RecursionError):\n          return DEFAULTS")


def _code_10_unicode_folding(s):
    return swap(s, "return conn.execute(query, (name, int(min_age))).fetchall()",
                'rows = conn.execute("SELECT id, name, age FROM users WHERE age >= ? ORDER BY id", (int(min_age),)).fetchall()\n'
                "      return [r for r in rows if r[1].casefold() == name.casefold()]")


def _ctl_2_backwards_time_resets_the_clock(s):
    return swap(s, "          if now > self._last:\n              earned", "          if now < self._last:\n              self._last = now\n          if now > self._last:\n              earned")


def _ctl_4_snapshot_copies_one_key(s):
    return swap(s, "return dict(self._counts)", "return dict(list(self._counts.items())[:1])")


def _ctl_6_day_skips_the_order_check(s):
    return swap(s, "if rank <= last_rank:", 'if rank <= last_rank and unit != "d":')


def _ctl_6_total_length_cap(s):
    return swap(s, "      position = 0\n", "      if len(text) > 36:\n          raise ValueError(text)\n      position = 0\n")


def _code_01_ceiling_only_from_one_full_page(s):
    return swap(s, "total_pages = (total_items + per_page - 1) // per_page",
                "total_pages = (total_items + per_page - 1) // per_page if total_items >= per_page else 0")


def _code_04_none_is_a_miss(s):
    return swap(s, "              if key in self._data:\n                  return self._data[key]\n",
                "              cached = self._data.get(key)\n              if cached is not None:\n                  return cached\n")


def _ctl_2_available_truncates(s):
    return swap(s, "return self._tokens", "return int(self._tokens)")


def _ctl_5_selects_the_wrong_column(s):
    return swap(s, "SELECT id, title FROM books", "SELECT id, author FROM books")


def _ctl_5_limit_bounds_exclusive(s):
    return swap(s, "not 1 <= limit <= 100", "not 1 < limit < 100")


def _ctl_6_lone_hour_rejected(s):
    return swap(s, "      return total\n", "      if text.endswith(\"h\"):\n          raise ValueError(text)\n      return total\n")


MUTANTS = [
    ("code-01", _code_01_ceiling_only_from_one_full_page),
    ("code-04", _code_04_none_is_a_miss),
    ("code-ctl-2", _ctl_2_available_truncates),
    ("code-ctl-5", _ctl_5_selects_the_wrong_column),
    ("code-ctl-5", _ctl_5_limit_bounds_exclusive),
    ("code-ctl-6", _ctl_6_lone_hour_rejected),
    ("code-01", _code_01_has_next_only_on_page_one),
    ("code-03", _code_03_evicts_at_most_one),
    ("code-03", _code_03_remaining_zero_when_empty),
    ("code-06", _code_06_returns_the_shared_defaults),
    ("code-10", _code_10_unicode_folding),
    ("code-ctl-2", _ctl_2_backwards_time_resets_the_clock),
    ("code-ctl-4", _ctl_4_snapshot_copies_one_key),
    ("code-ctl-6", _ctl_6_day_skips_the_order_check),
    ("code-ctl-6", _ctl_6_total_length_cap),
    ("code-01", _code_01_without_page_fields),
    ("code-01", _code_01_wrong_total_items),
    ("code-03", _code_03_cutoff_arithmetic),
    ("code-05", _code_05_lock_around_everything),
    ("code-05", _code_05_expiry_before_refresh),
    ("code-06", _code_06_no_recursion_handler),
    ("code-06", _code_06_no_permission_handler),
    ("code-08", _code_08_root_doubles_the_separator),
    ("code-09", _code_09_touches_the_file_system_first),
    ("code-09", _code_09_backslash_not_a_separator),
    ("code-10", _code_10_strict_age),
    ("code-10", _code_10_like),
    ("code-10", _code_10_order_by_age),
    ("code-ctl-1", _ctl_1_rejects_every_colon),
    ("code-ctl-2", _ctl_2_failed_consume_spends_tokens),
    ("code-ctl-4", _ctl_4_no_lock_in_hit),
    ("code-ctl-5", _ctl_5_no_nul_check),
    ("code-ctl-5", _ctl_5_backslash_not_escaped),
    ("code-ctl-5", _ctl_5_wildcards_not_escaped),
    ("code-ctl-5", _ctl_5_default_limit_100),
    ("code-ctl-5", _ctl_5_isbn_not_searchable),
    ("code-ctl-6", _ctl_6_unbounded_digits),
]


@pytest.mark.parametrize("task_id,mutate", MUTANTS, ids=lambda x: x if isinstance(x, str) else x.__name__.lstrip("_"))
def test_mutant_is_caught(task_id, mutate):
    if mutate is _code_06_no_permission_handler and hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("file permissions are not enforced for root")
    task = TASKS[task_id]
    source = base(task_id)
    mutant = mutate(source)
    assert mutant != source
    res = grade_fix(task, mutant)
    assert not res.timed_out
    assert not res.passed, "the hidden tests accept this wrong solution"
    assert res.n_errors == 0, res.output_tail
    # and the unmutated solution still passes
    assert grade_fix(task, source).passed


@pytest.mark.parametrize("task_id", ["code-04", "code-05", "code-ctl-4"])
def test_concurrency_suites_give_the_same_verdict_every_time(task_id):
    """No sleeping: the overlap is forced by events and barriers, so the verdict must not flap."""
    task = TASKS[task_id]
    if task_id == "code-ctl-4":
        bad, good = _ctl_4_no_lock_in_hit(task.lead_answer), task.lead_answer
    else:
        bad, good = task.lead_answer, task.reference_fix
    for _ in range(4):
        assert not grade_fix(task, bad).passed
        assert grade_fix(task, good).passed
