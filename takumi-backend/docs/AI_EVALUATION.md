# AI evaluation (V3)

## Datasets

| File | Cases | Content |
|---|---|---|
| `evals/gaps_v1.json` | 8 | complete knowledge, missing causal reasoning, missing cue, missing alternative, ambiguous action, unobservable factor, already-answered question, embedded prompt injection |
| `evals/qa_v1.json` | 24 | 10 supported (illustrative bearing fixture + a second synthetic task), 5 unsupported (torque, tolerance, speed safety, unrecorded tactile feel, unrelated machine), 2 ambiguous, 2 wrong-task/wrong-release, 2 source injection, 3 adversarial counterexamples (invalid citation, changed number, lost negation) |

All cases are synthetic and labelled illustrative. No torque, tolerance or tactile facts
are authored. No genuine expert-reviewed corpus exists.

## Runner

```sh
uv run python scripts/run_evals.py --mode deterministic --output /tmp/takumi-v3-evals.json
```

Deterministic mode loads no credentials and runs the production code paths
(`select_candidates`, deterministic gap generation, `qa_service.ask` on a temporary
SQLite database). Live-mode cases replay scripted structured model responses through
the same validation. `--mode live` calls Gemini; it was **not executed**.

Reports (JSON + sibling Markdown) contain run ID, timestamp, dataset versions, mode,
model (null in deterministic mode), prompt versions from the registry, per-case
checks, pass/fail/error counts, measured runtime, model latency (null when no model
call) and human-review flags. Expert acceptance and expert-reviewed semantic
correctness are reported as `null` (unmeasured).

## Observed deterministic result (2026-10-25)

Exit 0. 32 cases: 32 pass, 0 fail, 0 error.

| Metric | Result |
|---|---|
| Supported answered (target 9/10) | 10/10 |
| Unsupported abstained (target 5/5) | 5/5 |
| Retrieval relevance | 12/12 |
| Citation validity | 32/32 |
| Exact-field support | 24/24 |
| Status correctness | 24/24 |
| Abstention | 12/12 |
| Isolation (wrong task/release) | 2/2 |
| Gap relevance vs authored labels | 4/4 measurable |
| Consequential-gap coverage | 4/4 measurable |

These are automatic checks against authored synthetic labels, not expert-reviewed
quality. The legacy three-template detector cannot produce `missing_cue`,
`changed_condition`, `beginner_mistake` or `unobservable_information` gaps; those
cases report a coverage limitation instead of a failure. Live gap relevance, question
specificity and Gemini answer relevance are unmeasured until a live run with genuine
expert labels.
