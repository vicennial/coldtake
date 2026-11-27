# coldtake evaluation

A corpus and harness for measuring whether a blind second opinion from another
model family finds mistakes that the lead model missed, and whether showing the
reviewer the lead's answer makes it worse at finding them.

Status: corpus, harness and the coldtake adapter are in place and tested with
fake providers. No real model has been called by the tests. A real run is
started deliberately, outside the tests (see "Real runs"). Pass `--smoke` to
`python -m evals.run` to run a fast deterministic 6-task stratified slice
(2 `code`, 2 `decision`, 2 `claim`, including controls).

## Questions

**Q1, error recall.** Given an answer with one planted error, how often does a
second pass find it, for each of:

| condition | mode | advisor family vs the lead's nominal family |
|---|---|---|
| `self_review` | review (sees the answer) | same |
| `cross_review` | review (sees the answer) | other |
| `blind_same` | alternate (answer sealed) | same |
| `blind_other` | alternate (answer sealed) | other |

**Q2, anchoring.** When the lead's answer is wrong, how often does a reviewer
who sees it agree with it (`self_review`, `cross_review`), compared with how
often an advisor who never saw it independently makes the same mistake
(`blind_same`, `blind_other`)? The blind rate is the baseline: a reviewer's
agreement rate above it is the part that exposure to the answer adds.

A third quantity, the **false-alarm rate** on control tasks whose lead answer is
correct, keeps Q1 honest: an advisor that says "wrong" every time has perfect
recall.

## Corpus (`tasks/`)

54 YAML files: 36 tasks with a planted error (12 per kind) and 18 controls (6
per kind) whose lead answer is correct. All content is original and fictional.

| kind | what the lead produced | the planted error | arbiter |
|---|---|---|---|
| `code` (18) | a Python module, at most 80 lines | one defect: off-by-one (2), wrong boundary (1), race (2), unhandled error (2), path traversal (2), injection (2), wrong default (1) | hidden pytest file, run on the lead code (must fail) and on `reference_fix` (must pass) |
| `decision` (18) | a recommendation among 3-4 options | a defensible-looking wrong option; the facts in the prompt make one option clearly best | option id match |
| `claim` (18) | a technical explanation of at most 250 words | one factual or logical error with an exact `error_span` and a `correct_statement` | LLM grader with a rubric |

Task fields: `id`, `kind`, `control`, `difficulty`, `tags`, `prompt` (what the
lead was asked), `lead_answer`, `ground_truth`, `grading`; code tasks add
`reference_fix`, decision tasks add `options`. The schema is
`schemas/task.schema.json`; `tasks_io.semantic_errors` checks what a schema
cannot (word and line limits, span uniqueness, option consistency).
`difficulty` is the author's guess and has not been calibrated.

Hidden tests live in `tasks/hidden/` (named so pytest does not collect them) and
import the candidate as `solution`. Every code task was checked to fail on the lead
and pass on the fix, and the lead and fix differ by a few lines.
`tests/test_hidden_mutants.py` adds plausible wrong solutions that earlier versions
of a suite accepted and requires each to fail, and re-runs the three concurrency
suites several times to show that their verdict does not flap. Those suites force
the overlap with events, barriers and instrumented locks; none of them sleeps.

**Corpus version.** `tasks_io.CORPUS_VERSION` is part of `corpus_sha256` and of every
run header, and each result record carries the hash of its task (YAML plus hidden
test). `--resume` refuses a results file written for another version or for a task
that has since changed, so results from different corpus states cannot be mixed.
Version 2 applied the findings of the independent cross-family audit, version 3 those of
a second round (with the grader rule on peripheral caveats) and version 4 a final pass; see
`AUDIT-FIXES.md`.

## Method

For each task, lead family L (two, mirrored) and condition, one advisor call is
made through `Advisor.ask(AdvisorRequest) -> AdvisorResponse`.

* Review requests carry the lead's answer (`primary_answer`). Blind requests do
  not; the lead's answer travels only as `sealed_answer` / `sealed_choice`,
  which are excluded from `visible_payload()`. `assert_blind` runs before every
  call and fails closed if the whole answer, or any 12-word span of it, appears
  in the payload. A canary test covers this.
* Blind code and claim requests ask the advisor to produce its own solution or
  explanation; the adapter returns the router's `comparison` of that memo with
  the sealed answer (`agree | partial | conflict` plus a crux). Blind decision
  requests return an option id and are compared by exact match.
* Review framing is neutral in both review conditions ("an answer"); `own` can
  be selected in the config to say "your earlier answer" in `self_review`.

### Grading, deterministic first

1. **Decision.** Exact match of the option id (`grading/deterministic.py`). No
   LLM. `identified` = the correct option; `missed` = the lead's wrong option;
   `partial` = some other option or `none`.
2. **Code.** The advisor's fix (review) or independent solution (blind) is run
   against the hidden tests (`run_hidden_tests`). The LLM grader is used only
   to ask whether the advisor *named* the planted defect, and a label of
   `identified` is downgraded to `partial` if the advisor's own code fails the
   tests. A passing fix plus a "defect found" verdict is `identified` without
   the LLM.
3. **Claim.** LLM grader only (`grading/llm.py`, prompt in
   `grading/grader_prompt.md`, output schema in
   `grading/grader_output.schema.json`). Key phrases are logged as a hint and
   never decide.

The grader receives the ground truth and the advisor's output, plus whether the
advisor saw the lead answer. It does not receive the condition, the advisor's
family or any other advisor's output. The runner picks a grader of a
**different family** from the advisor (`choose_grader`). Labels are
`identified | partial | missed` for defect tasks and `clean | false_alarm` for
controls (`clean` is added to the four labels so that a control has a correct
outcome). Anything that needs an LLM grade and has none is `ungraded` and is
counted, not guessed.

**Extra valid points do not count against the advisor.** On a control, a
false alarm is asserting that the lead's *central* answer, claim or code is wrong
(or recommending against it). Peripheral caveats, scope qualifications, edge cases
outside the task, hardening ideas, style notes and corrections of minor wording are
`clean`, even when valid and even when there are several. On a task with a planted
error, recall is whether the planted error was found; other valid issues raised
never lower the label (`spurious_findings` is only logged). Decision tasks are
unaffected: they are graded by the final option alone. The rule is stated in the
grader prompt (`grading/llm.py`, `grader_prompt.md`), in the grader output schema and
in the rubric of every code and claim control.

Outcome fields per response (`grading/combine.py` documents each): `label`,
`false_alarm` (controls), `agrees_with_wrong` (review) or `reproduces_wrong`
(blind) on defect tasks, `flagged`, `independent_correct`, `fix_passes`,
`valid`, `graded`. An invalid or errored response counts as a miss for recall
and is excluded from anchoring; the report prints the count.

### Statistics (`stats.py`, `report.py`)

* Proportions with Wilson 95% intervals: strict recall (`identified`), lenient
  recall (`identified` or `partial`), false-alarm rate, and the "wrong answer
  survives" rate (agreement for reviewers, reproduction for blind advisors).
* Paired comparisons by exact McNemar test on the discordant pairs, over tasks
  present in both conditions: self vs cross review, blind same vs other family,
  self review vs blind same, cross review vs blind other, for recall, false
  alarms and anchoring.
* Several samples of one (task, condition) are collapsed by majority vote (a
  tie counts as no) before any interval or test, so repeats do not inflate n.
* Results are given per lead family and pooled. Pooled units are (task, lead
  family) pairs that share tasks, so pooled p-values are optimistic and are
  marked.
* No multiple-comparison correction is applied. Treat p-values as screening
  signals.

How small is small: with 36 defect tasks per cell, an exact McNemar test needs
about 6 discordant pairs all in one direction (a 17-point gap) to reach p < 0.05,
and 0/18 false alarms still leaves an upper bound of about 18% on a single
family's false-alarm rate (0/36 pooled: about 10%). The corpus can show large effects. It cannot
rank two advisors that differ by ten points, and it cannot establish a low
false-alarm rate. More tasks, in particular more controls, are the cheapest way
to improve it.

## Threats to validity

Read these before using a number from this eval in a claim.

1. **Planted errors are not natural bugs.** Each task has exactly one defect,
   placed by the author, in code short enough to read in one pass. Real defects
   are rarer, entangled and unlabelled. Recall here is an upper bound on, and
   not an estimate of, recall on real code review.
2. **Classic defects are probably in training data.** The prefix-check path
   traversal, double-checked locking without the re-check and the
   `no-cache`/`no-store` mix-up are well known. High recall on them may be
   recall of familiar patterns.
3. **The corpus was drafted by a model from one of the compared families** (the
   Claude family). An independent audit by a model of the other family (versions 2
   to 4 of the corpus apply its findings, see `AUDIT-FIXES.md`) is not a human audit.
   Task difficulty and
   what counts as a "defensible" wrong option reflect that model's view, which
   may favour or disfavour its own family. A human must audit each ground truth,
   hidden test and rubric before a result is quoted.
4. **The leads are authored, not generated.** "Self-review" therefore means the
   advisor is of the lead's nominal family, not that the advisor wrote the
   answer, so it does not test recognition of one's own mistakes. The wrong
   decisions were chosen to look plausible; models may pick them less (or more)
   often than the errors they actually make. A follow-up should seed the lead
   answers from real model failures found by sampling.
5. **Small n and a coarse control set.** See the power note above. Eighteen controls
   still cannot bound false alarms tightly, and `difficulty` strata have 1-8 tasks.
6. **Decision tasks reward reading and arithmetic.** The facts are stated so
   that an expert can decide; real decisions are fuzzier. Option letters are
   balanced across tasks (tested) but not randomised per call.
7. **Grader bias.** The LLM grader decides all claim labels and the
   localisation part of code labels. Mitigations: deterministic grading wherever
   possible (all decisions, all code fixes); a grader from a different family
   than the advisor; a grader that cannot see the condition; a structured schema
   with verbatim evidence; deterministic overrides when tests disagree
   (`n_llm_downgraded_by_tests` is reported). Not mitigated: a cross-family
   grader can still favour answers in its own style or be lenient on vague
   advice. Before launch, hand-label at least 20% of LLM-graded records
   including all false alarms, and compare with `report.py --agreement` using a
   second grader (Cohen's kappa). Hidden tests can also be wrong in either
   direction: audit every record where the fix test and the LLM label disagree.
8. **The blind comparison is part of the measured system.** Blind recall on code
   and claim tasks depends on the router's comparison step as well as the
   advisor, while blind decision recall does not. Report `independent_correct`
   (the advisor's own answer) next to the flagged rate to separate them.
9. **"Wrong answer survives" is not identical across arms.** For reviewers it is
   graded agreement with the planted flaw; for blind advisors it is reproducing
   the same flaw. For decisions both are the same exact-match test. For code and
   claims the second needs an LLM judgement, and a blind advisor that fails the
   hidden tests for a different reason is not counted as reproducing the flaw.
10. **Nondeterminism and prompt sensitivity.** One prompt template and one
    output contract per mode. Use `--samples` of 3 or more for stable numbers
    and report model ids, efforts and the run's `provenance` block.
11. **Untrusted code.** Advisor-proposed code is executed by the hidden tests. The
    runner uses a temporary directory, a minimal environment and CPU, file-size
    and wall-clock limits, which is not a sandbox. Run real evaluations in a
    disposable container.

## Layout

```
evals/
  tasks/*.yaml            54 tasks; tasks/hidden/*.py hidden pytest files
  schemas/                task and advisor-response JSON schemas
  grading/
    deterministic.py      hidden-test runner, option matching
    llm.py                grader prompt builder, output validation, FakeGrader
    grader_prompt.md      prompt template
    grader_output.schema.json
    combine.py            outcome rules
  advisor.py              Advisor protocol, request builder, blindness check, FakeAdvisor
  run.py                  matrix, runner (threads, resume), JSONL writer, call estimates
  adapters/
    coldtake_adapter.py  real advisor and grader, driving coldtake in process
  audit.py                corpus audit through coldtake review requests
  report.py, stats.py     recall, false alarms, anchoring, Wilson, McNemar
  config.example.yaml, config.coldtake.example.yaml
  tests/                  includes fake_coldtake.py, in-process fake providers
```

## How to run

Install `pyyaml`, `jsonschema` and `pytest` (`pip install -r evals/requirements.txt`,
or `uv run --no-project --with pyyaml --with jsonschema --with pytest ...`).
From the repository root:

```
# validate the corpus and the harness (about 40 seconds; runs the hidden tests)
python -m pytest evals

# show the plan without calling anything
python -m evals.run --dry-run

# end-to-end on fake advisors, then the report
python -m evals.run --advisor fake --grader fake --out /tmp/fake.jsonl
python -m evals.report /tmp/fake.jsonl --json /tmp/fake.json
```

Useful options: `--ids`, `--kinds`, `--conditions`, `--lead-families`,
`--samples N`, `--seed S`, `--resume`, `--retry-errors`, `--concurrency N`,
`--max-cost-usd X`, `--config FILE`.

### Real runs

The runner refuses anything but `fake` unless `--allow-real` is given. The
adapter in `adapters/coldtake_adapter.py` drives coldtake in process through
its `Engine`, with the subscription CLIs only (no OpenRouter key is passed, the
paid model list is emptied and the budget is zero). Copy
`config.coldtake.example.yaml`, set `coldtake.state_dir` and run, preferably
in a disposable container:

```
python -m evals.run --dry-run --config my.yaml      # call counts per provider, no calls
python -m evals.run --allow-real --config my.yaml \
    --advisor evals.adapters.coldtake_adapter:advisor \
    --grader  evals.adapters.coldtake_adapter:graders \
    --samples 1 --out results.jsonl
python -m evals.run ... --out results.jsonl --resume   # continue after an interruption
```

**Conditions.** The adapter forces the advisor's family with a routing resolver
that exists only for the evaluation (`eval_resolver`; it raises when used
outside the adapter). The ladder has one route, so there is no fallback to the
other family and a family that cannot answer ends the cell as an error record.
`self_review` and `blind_same` use the lead's own family (coldtake allows it as
the last route and marks the memo `same_family: true`, copied to
`response.meta`); `cross_review` and `blind_other` use the other family. Review
cells submit the lead's answer as `primary_answer`. Blind cells submit it only
as `sealed_answer` (and `sealed_choice` for decisions), which coldtake stores
and hashes and never renders into an advisor prompt; the canary tests check
this on the prompts and on the bytes sent to fake CLI executables. The
comparison judge does see it, on the same family as the advisor (so in
`blind_same` the judge is of the lead's family; it is recorded in
`response.meta.comparison.judge`). Report this when reading blind recall on code
and claim tasks.

**Output mapping.** coldtake returns a memo, not the harness's fields, so the
adapter asks (in `constraints`) for a first line `VERDICT: sound|defect_found|uncertain`
in review mode, a fenced module in `recommendation` for code, and uses the
memo's `choice` for decisions. A missing verdict is recorded as such (the
outcome is invalid and counts as a miss), never guessed. `meta` carries the
provider, model, every attempt (provider, model, seconds, outcome, fallback),
the full comparison, the memo and the coldtake request id.

**Resume and ids.** Request ids are deterministic per (task, lead family,
condition, sample) plus a hash of what is sent, and do not depend on the run id
or seed. `--resume` skips every key already in `--out` and reuses the run id in
its header. A cell that finished in coldtake but is missing from the file (for
example a lost file) is replayed from the state directory without a call, as long
as the state directory is kept. `--retry-errors` asks again for cells whose
recorded response is an error; the adapter then submits under a new id (`-r1`).

**Concurrency.** `--concurrency N` (config `coldtake.concurrency_per_provider`,
default 2) bounds simultaneous calls per provider, counting advisors, judges and
graders together; the runner uses N threads per provider.

**Grader.** `graders` returns one grader per family. The runner picks the family
different from the advisor's. The prompt is exactly the harness prompt (nothing
about the condition or the advisor's family is added; a test compares the bytes),
and its request is a tool-free coldtake call with the grader schema, with one
repair for an invalid reply. A failed call is recorded as an ungraded response
and is not retried.

**Audit.** `python -m evals.audit` sends each task with its answer key to an
coldtake review request for a reviewer of another family (default `openai`, the
corpus having been drafted by an `anthropic`-family model), and writes a JSONL
file and a Markdown summary of the flagged tasks. It changes nothing. Read it
before the real run.

The adapter contract for another implementation, called as
`factory(lead_family, config) -> Advisor` and `factory("graders", config) -> list[Grader]`:

* send only `request.visible_payload()` to the model, never `sealed_*`;
* return `AdvisorResponse` for every call, putting failures in `error`;
* fill `comparison` for blind code and claim requests, `choice` for decisions,
  `proposed_fix` for code, `verdict` for review;
* report `cost_usd` when known, so `--max-cost-usd` works.

Results are JSONL: a header line (run id, harness version, git revision of the
evals tree, corpus hash, seed) then one record per call with the task hash, the
cell, the request hash and visible payload, the full response, every grade and
the outcome. The sealed answer is stored only as a hash.
