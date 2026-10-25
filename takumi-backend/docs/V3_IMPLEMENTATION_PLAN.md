# V3 implementation plan (C6A → C6B → C6C)

V3 adds live Gemini contextual clarification generation (C6A), live grounded Q&A with
deterministic support validation (C6B), and reproducible evaluation datasets plus an
HTTP acceptance CLI (C6C). V1/V2 lifecycle, publication gates, video extraction
(`video_extraction_v3`) and deterministic fallbacks are unchanged.

## Shared foundation

- `app/config.py`: `ENABLE_LIVE_GAP_GENERATION` and `ENABLE_LIVE_QA` are no longer
  reserved; both require `AI_PROVIDER=gemini` with a nonblank key and model. New
  booleans: `ENABLE_QA_SUPPORT_VALIDATION` (true), `ENABLE_QA_ABSTENTION` (true),
  `ENABLE_AI_EVAL_LOGGING` (true), `ENABLE_DETERMINISTIC_GAP_FALLBACK` (true),
  `ENABLE_AI_VIVA_SCORING` (false, reserved). Live Q&A refuses to start with either
  safety check disabled. Multilingual, embeddings, advanced retrieval, model-input
  logging and AI viva scoring stay reserved.
- `app/ai/knowledge.py` `KnowledgeAI`: structured Gemini text calls (JSON schema, temperature
  0, LOW thinking, 4096 output tokens, `STUDIO_AI_TIMEOUT_SECONDS`, two SDK attempts).
  Malformed output raises `AI_OUTPUT_INVALID`; transport errors reuse Gemini error
  normalisation. Services instantiate it at call time and always close it.
- `app/ai/schemas.py`: strict `GapCandidate`, `GapAnalysis`, `GroundedClaim`,
  `GroundedAnswer`; `ApprovedContext.evidence_items`.
- `GapType` adds `missing_cue`, `missing_exception`, `unobservable_information`.
- `knowledge_ai_runs` (`app/models/knowledge_ai.py`): gap-generation receipts and
  provenance plus grounded-Q&A call logs. Unique `(task_id, operation, request_id)`.
  Schema version 8 (table-additive). Demo reset and Studio reset delete task rows.
- `app/ai/prompts/registry.py`: frozen `PromptSpec` registry of `video_extraction_v3`,
  `transcript_extraction_v1`, `gap_elicitation_v1`, `grounded_qa_v1`. Prompt changes
  ship as new versions.

## C6A — contextual clarifications

`POST /api/v1/tasks/{task_id}/clarifications/generate` (`app/services/gap_generation_service.py`).
Short read transaction → bounded context (30 units, 60 evidence, latest 30
clarifications, 1000-character fields, 64,000-character DATA, whole-unit truncation
with `CONTEXT_TRUNCATED`) → model call with no open transaction → validation and
ranking (`select_candidates`) → final write transaction that resolves an existing
receipt first, then rejects revision or snapshot drift with `GENERATION_STALE`.
Candidates must reference supplied units and that unit's supplied evidence; prior
`(unit, gap_type)` pairs are excluded; at most three questions per generation.
Provider failure falls back to labelled deterministic questions or returns 503 when
fallback is disabled. Generation never bumps the draft revision.

## C6B — grounded Q&A

`qa_service.ask(factory, settings, task_id, req, user, provider)`. The active release
snapshot is copied to plain data, plus an independent validation copy, before any
provider call. `qa_matching.rank_units` (weighted, IDF-scaled overlap with four
conservative synonyms) selects the top three units. Deterministic guards (language,
empty question, unsupported specifications, unrecorded tactile cues) run first.
Every claim must quote a whole approved field exactly and cite a nonempty subset of
release evidence ∩ that field's evidence for this task, with valid intervals. Any
invalid live claim converts the answer to `insufficient_evidence` with
`review_required=true`. `missing_information` is server-owned. Provider failure is
503 (no fixture fallback); a release change during the call is 409 `RELEASE_CHANGED`.

## C6C — evaluation and acceptance

- `evals/gaps_v1.json`, `evals/qa_v1.json`: synthetic, illustrative-labelled cases.
- `scripts/run_evals.py --mode deterministic|live --output PATH`: runs production
  selection/Q&A code against temporary SQLite and writes JSON plus Markdown. Exit 1 on
  behavioural failure, 2 on runner/provider error.
- `scripts/v3_flow.py`: HTTP acceptance (generate → caller-supplied expert answer →
  attribution check → defer others → evaluate → reviewer publish → release/training
  pack → apprentice grounded answer citing the new evidence → torque abstention).

## Rollback

Disable `ENABLE_LIVE_GAP_GENERATION` / `ENABLE_LIVE_QA` with the schema-8 binary. A
schema-7 binary refuses a schema-8 database; restore the pre-upgrade backup if code
must roll back.

Results: [V3_CHECKPOINTS.md](V3_CHECKPOINTS.md), [AI_EVALUATION.md](AI_EVALUATION.md).
