# Corpus audit fixes (corpus version 2)

An independent reviewer of the other model family audited all 54 tasks (ground truth,
reference fix, hidden test, rubric, prompt, lead answer). This file records what was done
with each flagged task. 40 tasks were flagged: 35 with a `flag` verdict and 5 whose record
listed issues without a verdict (dec-09, dec-10, dec-ctl-1, dec-ctl-3, dec-ctl-5), which are
treated as flags. Tasks that were `ok` with no issues are unchanged.

Issue classes: (a) the planted lead answer contained a second unintended error; (b) the ground
truth or reference fix was wrong; (c) the hidden test was unfair, weak or timing dependent;
(d) the rubric labels overlapped; (e) a control's "correct" answer contained an error; (f) the
prompt or options were ambiguous.

All task ids, kinds, control status and planted error categories are unchanged. Every code task
still fails its hidden test on the lead and passes it on the reference fix (or, for controls,
passes on the lead); `tests/test_hidden_mutants.py` adds wrong solutions that the old suites
accepted and checks that each fails.

## Rubrics of all decision tasks (class d)

Decision tasks are graded by exact match of the final option id (`grading/combine.py`), never by
an LLM, so the rubrics now say exactly that. Every decision rubric has one rule per label and
the explanation never changes the label: `identified` = final option is the correct one,
`missed` = final option is the lead's, `partial` = any other option or none; controls: `clean` =
the correct option, `false_alarm` = any other option or none. `key_phrases` stay as logged hints.
This is the fix for the overlap flagged on dec-01, 03, 04, 05, 06, 07, 08, 09, 10, 11, 12 and
dec-ctl-1..6, and it also removes the "reasoning is materially wrong" false-alarm clause from the
controls. A test (`test_decision_rubric_labels_are_exclusive_and_match_the_ground_truth`) pins it.

## Per-task table

| task | class | action | change |
|---|---|---|---|
| claim-02 | c, a | fixed | "O(1)" became "fast" in the lead and error span; rubric requires rejecting "avoids clustering" or the no-resize conclusion, and says a "fixed load factor is formally O(1)" objection alone is not identification |
| claim-03 | a | fixed | "the cache must revalidate" became "normally revalidates" (stale reuse and unconditional refetch are legal) |
| claim-05 | a | fixed | smaller-partition remark now says "recursing into the smaller partition while looping on the larger one" |
| claim-06 | a, b | fixed | "every TLS 1.3 handshake" became "every certificate-authenticated" one; correct statement scoped to the certificate-authenticated (EC)DHE handshake |
| claim-07 | a | fixed | "Do not compare ... with ==" became "Do not rely on == to compare computed results" |
| claim-08 | a | fixed | commit rule now says "an entry from the leader's current term" |
| claim-09 | a | fixed | merge sentence now starts "Unless it can simply fast-forward," |
| claim-10 | a, b | fixed | lead and correct statement exclude the surrogate range U+D800 to U+DFFF from the 3-byte range; planted span unchanged |
| claim-ctl-4 | e | fixed | the double nearest 0.3 is said to be slightly less than 0.3 (checked with `Decimal`); "denominator, in lowest terms" |
| code-01 | c | fixed | new tests check `page`, `per_page`, `total_items` and the required keys, including an empty page |
| code-03 | b, c, f | fixed | prompt specifies `remaining(now)`; eviction compares age (`now - t0 >= window`) instead of a rounded cutoff, with a floating-point case at the boundary (the lead keeps the strict-comparison bug) |
| code-04 | c | fixed | sleep removed: a key whose `hash()` makes both threads meet at a barrier forces the overlap; loader waits for a second call or for the barrier to time out |
| code-05 | c, f | fixed | sleep removed: instrumented locks report contention; added tests for lock-free valid reads and expiry measured after the refresh; prompt states `ttl_seconds > 0` |
| code-06 | b, c, f | fixed | added PermissionError and deep-nesting tests; `RecursionError` added to the prompt and the reference handler |
| code-08 | b, c, f | fixed | reference uses `os.path.commonpath` (the `+ os.sep` form breaks for a root base); prompt says acceptance depends only on where the path resolves; tests for root base and absolute path inside base |
| code-09 | a, c, f | fixed | prompt: `/` and `\` are separators, assume no symlinks under root (removes the second defect); new test patches file-system calls and checks rejection happens before any access |
| code-10 | c | fixed | tests for inclusive `min_age`, LIKE wildcards, and id order differing from age order |
| code-ctl-1 | e, c | fixed | lead no longer rejects `1:report.txt`; the drive check needs an ASCII letter then a colon; prompt states it; tests for colon names |
| code-ctl-2 | c, f | fixed | prompt specifies `available()`; the test that needed it is split from one that does not |
| code-ctl-4 | c | fixed | race forced deterministically (barrier between the read and the write of the count); a lock-free mutant must fail every run |
| code-ctl-5 | e, c, f | fixed | NUL in the term is rejected (prompt, lead, tests); placeholder-count assertion relaxed; backslash, ISBN and default-limit cases added; prompt assumes a default connection |
| code-ctl-6 | e, c | fixed | prompt limits numbers to nine digits and the lead's pattern enforces it, so `int()` never meets the conversion limit; tests for ten digits and 5,000 digits |
| dec-01 | b, a, d | fixed | lead "completely"/"milliseconds" softened; ground truth no longer asserts the copy takes hours (any copy diverges under 900 writes/s) |
| dec-02 | b, f, a, d | fixed | traffic raised to 200,000 reads/s so a 5 s TTL needs about 9,500 database reads/s, above even primary plus 3 replicas; event bus delivery (about 1 s, retried) stated so C meets the policy; rationale and key phrases recomputed |
| dec-03 | f, b, d | fixed | prompt: a shed 503 creates nothing, so a retry with the same key is processed normally; rationale no longer claims retries "usually succeed" and counts the waits |
| dec-04 | b, d | fixed | rationale explains why B and C need coordination and when version 1 handling can go (7 days after the last version 1 event) |
| dec-05 | b, f, d | fixed | B uses 512 partitions (0.4 utilisation, headroom for skew); prompt adds a uniform-volume fact and "simplest design that keeps order without storage in the event path"; D's description corrected |
| dec-06 | b, d | fixed | prompt: pods open new connections with the password they hold; rationale limited to what that implies and to a successful fetch |
| dec-07 | b, d | fixed | option A says acknowledge only after commit and keep rows at least 3 days; rationale updated |
| dec-08 | a, b, f, d | fixed | prompt: deletes of about 5 rows/s and the export membership rule; rationale explains duplicates (inserts) and skipped rows (deletes) and drops "same cost on every page" and "snapshot goes stale"; lead's covering-index claim replaced |
| dec-09 | a, b, d | fixed | lead no longer asserts a cache throughput; rationale shows overselling without any restart |
| dec-10 | b, d | fixed | prompt: every request finishes within 30 s, so tail sampling's buffer keeps all qualifying traces |
| dec-11 | b, d | fixed | prompt: logs reach object storage within about a minute; the 35-minute test includes choosing the time, verifying and returning to service |
| dec-12 | a, b, d | fixed | lead no longer says D has no end date; rationale does not claim usage logging proves response-field use |
| dec-ctl-1 | e | fixed | "50 matching index entries"; D no longer said to force an index merge |
| dec-ctl-2 | e, f | fixed | claim about type limits removed; option B caps size only; rubric option-only |
| dec-ctl-3 | e, f | fixed | prompt: `id` unique, readers see all or nothing; A criticised for deleting records absent from the files |
| dec-ctl-4 | e | fixed | "typically" within seconds; battery claim restated as avoiding polling and an own connection |
| dec-ctl-5 | e, f | fixed | "many" failures recovered; option D gives each attempt only the remaining time |
| dec-ctl-6 | e, f | fixed | prompt: collision probability must be negligible and asks for the best of the four; "ids created later generally sort after" |

## Flags rejected, or only partly accepted

| task | flag | reason |
|---|---|---|
| dec-02 | a 5 s TTL cannot guarantee visibility within 5 s of commit | An entry filled at time t before the commit expires at t + 5, which is before commit + 5, and an entry filled after the commit holds the new price. The lead's freshness claim for A is correct; its error is capacity. Not changed. |
| dec-03 | the lead overlooks A's stable key; a user tap gives no duplicate protection | That oversight is the planted error (the recommendation of B for the wrong reason). Left as is. |
| dec-06 | the lead omits how the 60 running pods get the new password | Same: the omission is the planted flaw. Left as is. |
| dec-05 | a sound D is capped at partial | Resolved by the prompt criterion (no storage in the event path), not by changing the label. |
| dec-12 | full credit for selecting D alone can reward removing `total` on schedule | The task grades the choice of rollout strategy, not its execution. Not changed. |
| dec-01, dec-08 | key phrases can occur in wrong answers or be absent from right ones | `key_phrases` are a logged hint and decide nothing (README, `key_phrase_hits`). The rubric now says so. |
| claim-03, 05, 06, 07, 08, 09, 10 | the key should also cover the lead's second error | The extra errors were removed from the lead (class a) so that exactly the planted one remains; they were not added to the key. |

# Round 2 (corpus version 3)

The corrected corpus was audited again (40 tasks: 1 `ok`, 16 `flag`, 23 with issues but no verdict).
The reviewer mostly found new edge cases, so only changes that alter how a reasonable advisor response
is graded were made: (1) the key or reference is wrong under the prompt's own assumptions, (2) the lead
contains a second error a strong reviewer would lead with, (3) a hidden test accepts a plausible wrong
solution or rejects a correct one, (4) the prompt is ambiguous about the graded point. Out-of-scope
edge cases (Windows paths, overflow, NaN, symlink loops) were handled with one scoping sentence in the
prompt, or rejected. "fixed" = key, lead or test changed; "scoped" = a sentence added to the prompt;
"rejected" = nothing changed.

## Grader rule and audit tool

- **Peripheral caveats are never false alarms.** On a control, a false alarm is asserting that the central
  answer, claim or code is wrong (or recommending against it). Scope qualifications, edge cases outside the
  task, hardening and style notes are `clean`. On a planted task, extra valid issues never lower the label.
  Applied in the grader prompt, the label rules, the output schema, the rubric of every code and claim
  control, `README.md`, and `test_peripheral_caveats_are_not_false_alarms_and_extra_issues_do_not_lower_recall`.
  This is why most "the control's lead overstates X" findings below are rejected.
- **Audit verdict.** The audit tool now repairs a reply without the `AUDIT:` line once (a second call that
  spells the requirement out). A task still without a verdict is recorded as `verdict: "none"` with
  `status: complete`, shown in its own summary section, and distinct from a failed call. `--resume` re-asks
  tasks with verdict `none`.
- **Decision rubrics** say that the option id is read from the response's structured `choice` field.

## Per-task table

| task | issue | action | change |
|---|---|---|---|
| claim-02 | partial label ambiguous when only one consequence is rejected | fixed | partial now covers naming clustering but rejecting only one of the claim and the resize conclusion |
| claim-03 | `private`/`Vary` sentences are not exact | rejected | peripheral; not the planted point and not a central claim |
| claim-05 | key omits median selection cost | fixed | correct statement names worst-case linear-time selection (median of medians); "on average" left (peripheral) |
| claim-06 | PSK-only handshakes need no fresh key exchange | fixed | last paragraph limited to servers performing the Diffie-Hellman exchange |
| claim-07 | normal-range qualification; binary32 optional; overbroad `==` advice | fixed / rejected | key says "in the normal range"; rubric makes single precision optional; `==` wording already qualified, kept |
| claim-08 | overlap sentence, progress wording | rejected | peripheral |
| claim-09 | "only reachable through reflog" overstated | fixed | key says the old commits are left behind; other points (merge commits, up-to-date merge) rejected as peripheral |
| claim-10 | "scanning from the start" is unconditional; rubric allows a blanket emoji claim | fixed | "without a separate index"; rubric requires four bytes above U+FFFF (U+10000 to U+10FFFF) |
| claim-ctl-4 | rounding modes; exact equality is sometimes right | rejected | covered by the peripheral-caveat rule |
| code-01 | `has_next` checked only on page 1; key groups empty input with multiples | fixed | test over pages 1 to 4 and a 20-item case; key says empty input only gives `total_pages` 1 instead of 0 |
| code-03 | evicting one request only; same-instant requests; `remaining` with empty window | fixed | three tests, two new mutants |
| code-04 | failed-load waiters, invalidate overlap, loader reentrancy; barrier timeout under starvation | scoped / rejected | prompt: a waiter may run the loader itself, overlapping get/invalidate may return either value, the loader never calls back into the cache. The starvation window (1.5 s) is documented and kept |
| code-05 | TracingLock hides Condition internals; no expired-token case; reuse of an already expired result | fixed / scoped / rejected | lock forwards `_is_owned`/`_release_save`/`_acquire_restore`; concurrent test also runs from an expired token; prompt assumes a long enough ttl. A Condition's own internal lock cannot be traced, and an ignored `contended.wait` is deliberate for lock-free designs: rejected |
| code-06 | rubric omits RecursionError; comparison against `solution.DEFAULTS`; merge fixture; fresh copy; NaN; deep object | fixed / scoped | literal expected dict, retries/verbose overrides, fresh-copy test for five fallbacks, deep object nesting; rubric names RecursionError; prompt accepts Python json defaults (NaN) |
| code-08 | equality wording; `Path` vs `str`; host-specific root; inward symlink; rubric boundary | fixed / scoped | prompt: return `str`, POSIX, no symlink loops, equal to base accepted; inward-symlink test; rubric keeps base and root working. Hard-coded root names left (the test only needs a non-existent name) |
| code-09 | Windows drive-relative paths | scoped | prompt: POSIX server, drive letters not special |
| code-10 | row factory, `min_age` type, non-ASCII NOCASE, negative ages, stored SQL-looking name, remediation | fixed / scoped | prompt scopes row factory, int `min_age`, ASCII-only NOCASE; four new fixture rows and tests; rubric accepts any fix that meets the contract |
| code-ctl-1 | loop/Windows/`Path`; in-tree symlink | scoped / fixed | prompt: `str`, POSIX, no loops; in-tree symlink test |
| code-ctl-2 | overflow; backward time; start time; failed consume | scoped / fixed | prompt: ordinary finite numbers; three new tests, one new mutant |
| code-ctl-4 | single-key snapshot; fractional limit; hash-once race | fixed / scoped / rejected | multi-key snapshot test, prompt says `limit` is an integer; the stress test remains for implementations that hash once |
| code-ctl-5 | verbatim marker in a bound string; year substring; NUL in stored data; custom column objects | fixed / rejected | marker assertion removed, year substring test added; the other two are outside the contract |
| code-ctl-6 | total-length cap; day placement | fixed | 40-character valid case; `1h1d`, `1d1d` and others invalid; two new mutants |
| dec-01 | database-side change capture | scoped | prompt: no change-data-capture of any kind |
| dec-02 | in-flight read can refill after the delete; delivery deadline; long tail | fixed / scoped | option C records the event version and refuses older fills (prompt states versioned prices); other SKUs add negligible load; rationale updated |
| dec-03 | deadline enforcement in A; overlap after timeout | scoped / rejected | option A times out each attempt within the budget; rest rejected |
| dec-04 | extraction of the final option | fixed | rubric reads the `choice` field |
| dec-05 | broker ordering guarantee wording | rejected | consumers one per partition is the stated model |
| dec-06 | when the secret is published | scoped | prompt: published at the moment of rotation |
| dec-07 | lead; deleting dedupe rows | rejected | already stated in option A (keep at least 3 days) |
| dec-08 | immutable ordering key; memory feasibility | scoped / rejected | prompt: `created_at` and `id` never change; B's memory claim left (10 million rows per export on 8 GB) |
| dec-09 | certainty of overselling; cache capacity | rejected | with 2,000 attempts/s over 4 nodes and 500 units it is certain; the lead no longer asserts a capacity |
| dec-10 | 575 adds overlapping traces | fixed | "at most about 575" |
| dec-11, dec-12 | lag and readiness qualifiers | rejected | peripheral to the option choice |
| dec-ctl-1 to dec-ctl-6 | unsupported or overbroad remarks in the control leads | rejected | graded by the final option, and caveats are not false alarms (rule above); the choice is unaffected |

# Round 3 (final, no re-audit; corpus version 4)

The third audit (25 tasks: 1 `ok`, 16 `flag`, 8 `none`) was triaged under the grader rule of round 2: extra valid
issues in planted leads and caveats on controls no longer affect grades. Only the items below were changed.

| task | issue | action | change |
|---|---|---|---|
| decision tasks (all 18) | "the memo may have no `choice` field" | verified, no code fix | path: `task.options` -> `AdvisorRequest.options` -> `to_submission()["options"]` -> coldtake `Packet` -> `ChoiceMemo` schema (`choice` required) -> `map_response` reads `memo["choice"]` -> `grade_option`. New tests in `test_adapter.py` check this for every decision task in both modes, with the real coldtake contract, and end to end through the engine |
| code-06 | deep-object test could contradict the prompt | fixed | the test requires defaults only when `json.loads` raises `RecursionError`; the prompt says "the standard `json` decoder" and "if a decoder does parse it, merge as usual" |
| code-01 | non-empty input with fewer items than `per_page` | fixed | test and mutant (ceiling only from one full page) |
| code-04 | a cached `None` must be a hit | fixed | test and mutant (`is not None` check) |
| code-ctl-5 | projected second field; `limit` 1 and 100 | fixed | tests and two mutants (wrong column, exclusive bounds) |
| code-ctl-6 | standalone hour | fixed | `1h` and `24h` cases and a mutant |
| code-ctl-2 | fractional `available()` | fixed | test and mutant (`int(...)`) |
| claim-10 | "scanning from the start" too absolute | fixed | "In a plain byte string, finding it generally means scanning from the start"; the planted error is unchanged |
| all other flagged items | extra caveats on planted leads and controls; further edge cases | rejected | outside the triage rule and the grader rule; not logged one by one |
