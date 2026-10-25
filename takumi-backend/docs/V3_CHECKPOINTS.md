# V3 checkpoints

Plan: [V3_IMPLEMENTATION_PLAN.md](V3_IMPLEMENTATION_PLAN.md). Evaluation:
[AI_EVALUATION.md](AI_EVALUATION.md). All results observed on 2026-10-25.

## Baseline

`uv run pytest -q` before V3 changes: 324 passed, 1 skipped.

## Foundation

Flags, `KnowledgeAI`, strict AI schemas, extended `GapType`, `knowledge_ai_runs`
(schema 8), reset cleanup, prompt registry. Upgrade test: a schema-7 database with an
approved release advances to 8 and keeps release `content_hash` values and
clarification rows unchanged.

## C6A — contextual clarifications

`tests/test_gap_generation.py` (fake `KnowledgeAI`) covers: labelled fallback on
malformed output and timeout; 503 with no writes when fallback is disabled; at most
three ranked questions; rejection of unknown units, other-task evidence and evidence
outside the unit; identical replay without a second model call or duplicate rows
(including empty and fallback receipts); `REQUEST_ID_CONFLICT`; stale revision and
disposition/evidence drift during the call → `GENERATION_STALE` with no writes;
populated-but-uninformative fields still questioned; prior answered/rejected pairs
excluded; concurrent identical requests return the first receipt; model call runs
without an open transaction; live disabled → 501.

## C6B — grounded Q&A

`tests/test_live_qa.py` (fake `KnowledgeAI`) covers fail-closed handling of wrong-field
evidence, mixed valid/invalid citations, other task/release evidence, fabricated
units, empty citations, unrecorded numbers, lost negation, answered-without-claims,
provider-mutated context and advice hidden in `missing_information`; exact approved
field with correct citation answers; draft-only content is not answerable; the five
unsupported specification questions abstain; timeout → 503 without fixture prose;
release change during the call → `RELEASE_CHANGED`; call logs contain no question text.

## C6C — evaluation and acceptance tooling

- Deterministic evaluation: exit 0, 32/32 cases pass; supported 10/10, unsupported
  5/5 (synthetic labels, not expert review).
- Full suite after integration: 393 passed, 1 skipped. `ruff check` and
  `ruff format --check` clean.
- Real Uvicorn HTTP smoke (temporary DB, fixture seed, fake structured `KnowledgeAI`
  injected by a disposable launcher): `scripts/v3_flow.py --generate-only` then resume
  with the same request ID and a caller-supplied illustrative answer printed `OK 1`
  through `OK 8` (generation, attributed expert evidence, deferral, evaluation,
  reviewer publish, immutable release and training pack, grounded answer citing the
  new evidence, torque abstention). This proves API/state integration, not Gemini
  quality.
- Same server with live flags off: `scripts/demo_flow.py` printed `OK 1` through `OK 16`.
- Read-only security review of grounding, idempotency, transaction and upgrade paths:
  no findings.

## Local live acceptance (real Gemini)

Source: copy of `/tmp/takumi_accept.db` (full-clip V2 extraction, task
`task_f9a6ece37e8a`, draft `draft_043ceb9cf247` revision 2) made with the SQLite backup
API to `/tmp/takumi_v3_accept.db`. Startup upgraded it from schema 7 to 8.

`scripts/v3_flow.py --mode live --generate-only --request-id v3-live-acceptance-001`
with `gemini-3.5-flash-lite`: exit 0, `generation_mode=live`, one call, latency
2,891 ms, 3,460 total tokens, no warnings, one question:

- `clar_fc225ddc2fba`, unit `ku_0c6928163e80`, `missing_reasoning`, citing
  `ev_6c6aa5163d50` (6.0–21.0 s): "What is the primary criteria or reasoning for
  demonstrating the precision insert split sleeve type bearing versus the radial load
  bearing design?"

The references were valid. The operator chose not to supply an expert answer, so the
resume half (answer, publish, live grounded Q&A, abstention) was **not run**. Live
grounded Q&A against real Gemini is therefore **unverified**; live gap quality is a
single observation without expert judgement.

## Active configuration

`takumi-backend/.env` (shared by Docker `takumi-api` and `takumi-worker`):
`AI_PROVIDER=gemini`, `GEMINI_MODEL=gemini-3.5-flash-lite`, media upload/streaming/
processing, worker and live extraction on, `ENABLE_LIVE_GAP_GENERATION=true`,
`ENABLE_LIVE_QA=true`, support validation, abstention, eval logging and deterministic
gap fallback on, Studio AI analysis on; multilingual, embeddings, advanced retrieval
and AI viva scoring off. Docker behaviour with this configuration was not tested.
