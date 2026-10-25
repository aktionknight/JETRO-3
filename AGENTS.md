# AGENTS.md

Takumi: expert knowledge capture → reviewed release → apprentice training. This repo holds planning docs and the V2 backend knowledge engine.

## Layout
- `markdowns/globals/` — product concept (`Takumi_Final_Concept_and_Three_Person_Technical_Plan.docx`) and the V1 backend master prompt (`takumi_v1_backend_prompt.md`).
- `markdowns/hackathon/` — feature specs (expert studio, apprentice playground, technical viva) and `openapi/`.
- `takumi-backend/` — FastAPI + SQLite knowledge engine (tasks, drafts, clarifications, evidence, immutable releases, training pack, grounded Q&A).
- `backend/`, `frontend/`, `deploy/`, `docker-compose.yml`, `pytest.ini`, `scripts/verify.sh` — Technical Viva app (branch `Manas`); see root `README.md`. Separate from `takumi-backend/`; Viva uses port 8000, the knowledge engine 8001.
- `src/`, `e2e/`, `index.html`, `package.json`, `vite.config.ts`, `playwright.config.ts`, `scripts/{dev,demo-preflight,create-test-video}.mjs`, `audits/` — apprentice learning playground, 3D bearing simulation and Studio admin UI (Vite + React + three.js, branch `aakrit`); see `PLAYGROUND.md` (root `README.md` on `himanish`). It calls `takumi-backend` `/api/v1/learning` and `/api/v1/studio` through the Vite `/api` proxy (`VITE_API_TARGET`, default `http://127.0.0.1:8001`); `npm run dev` starts the knowledge engine on 8001.
- Docker: `docker compose up --build` runs Viva on `:8080` and the playground + knowledge engine on `:8081` (separate origins: both own `/` and `/api/v1/studio/...`). Images: `deploy/{backend,frontend,takumi,playground}.Dockerfile`; routing only in `deploy/Caddyfile`; root `.dockerignore` keeps `.env`, `.venv`, `data/` out. `takumi-api` reads optional `takumi-backend/.env`, data in volume `takumi-data`; worker is opt-in via `--profile takumi-worker`.

## Contracts
- Canonical API contract: `markdowns/hackathon/openapi/API.doc.md`.
- `markdowns/hackathon/openapi/*.openapi.json` and `build_openapi.py` conflict with it (`/studio/*` prefixes, flat errors, `learner` role, `Idempotency-Key`). Do not implement from them and do not edit them.
- Backend contract output: `takumi-backend/contracts/` (regenerate with `scripts/export_contracts.py`). Deviations from API.doc.md are listed in `takumi-backend/docs/INTEGRATION_HANDOFF.md`.

## Ownership boundary
`takumi-backend/` is the knowledge engine only. No frontend, 3D, scenario, attempt, viva or report code here; teammates mount their own routers in `app/main.py` and reuse `get_current_user` / `require_roles` from `app/core/security.py`.

## Commands (run from `takumi-backend/`)
```sh
uv sync
uv run uvicorn app.main:app --reload --port 8001 --no-access-log   # DB auto-created and seeded (approved_baseline)
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run python scripts/demo_flow.py --base-url http://localhost:8001   # 16-step acceptance flow, needs running server
uv run python scripts/run_worker.py   # separate process; requires live Gemini configuration
uv run python scripts/video_flow.py --video demo.mp4   # upload, range, process, poll, draft
uv run python scripts/seed_demo.py --state fresh|approved_baseline
uv run python scripts/reset_demo.py --state fresh --yes
uv run python scripts/export_contracts.py
uv run python scripts/run_evals.py --mode deterministic --output /tmp/takumi-v3-evals.json   # V3 offline evals
uv run python scripts/v3_flow.py --base-url http://localhost:8001 --task-id <task> --generate-only   # V3 HTTP acceptance; resume adds --answer/--question
```
Python 3.11 (`.python-version`), managed by uv. Never hand-edit `uv.lock`; use `uv add`.

## Architecture rules
- Routers (`app/api/`) stay thin; logic lives in `app/services/`.
- Transactions: services mutate then `session.flush()`; routers, lifespan, scripts and worker own commits.
- Never run Gemini calls, ffprobe or file copies inside a database transaction.
- Running-job writes use lease-guarded `_update`; completion writes use an equivalent lease guard in the draft transaction.
- AI output is always an unreviewed draft. Code never publishes it automatically.
- Processing rejects tasks with an existing draft (`DRAFT_ALREADY_EXISTS`); create a new task for a new recording.
- Use `--no-access-log` for media servers: access logs expose playback URL tokens.
- All feature flags live in `app/config.py` (`Settings`); reserved flags fail fast at startup. Core lifecycle and auth have no flags.
- Errors: raise `AppError(code, message, status, details)` → `{"error":{code,message,details}}`. Disabled features use `feature_disabled(...)` (HTTP 501). Codes are stable; see the handoff doc table.
- Illustrative demo content lives only in `app/fixtures/shaft_bearing_v1.json` and stays labelled `illustrative`. Never invent torque, tolerance or tactile claims.
- Releases are immutable (SQLite UPDATE trigger); status (`active`/`superseded`) is derived from `Task.active_release_id`.
- Q&A answers only from the active release snapshot; claims must cite release evidence of that field, and live claims must equal a whole approved field (`ENABLE_LIVE_QA` requires support validation and abstention).
- Draft edits go through `knowledge_service.apply_field_updates` + `bump_draft` (optimistic revision check, `REVISION_CONFLICT` 409).
- AI interfaces live in `app/ai/base.py`: `KnowledgeAIProvider` stays fixture-backed for deterministic Q&A/gaps; `VideoExtractor` uses Gemini for worker video extraction. Live gap generation and grounded Q&A call `app/ai/knowledge.py:KnowledgeAI` from services (tests monkeypatch `app.ai.knowledge.KnowledgeAI`). Prompts are versioned in `app/ai/prompts/registry.py`; never edit a released prompt in place.
- Gap generation (`POST /tasks/{id}/clarifications/generate`) persists receipts in `knowledge_ai_runs`; never bump the draft revision there.

## Testing
- `tests/conftest.py`: `make_client(**settings_overrides)` context manager (temp SQLite DB per test, `demo_seed_state="fresh"`), `client` fixture, `auth(client, role)` helper.
- `tests/test_integration.py` runs the full 16-step flow twice; keep it green.
- Contract examples in `contracts/api-examples/` contain generated ids/timestamps; regenerating them produces id churn only.

## Docs
`takumi-backend/README.md`, `takumi-backend/docs/{INTEGRATION_HANDOFF,CHECKPOINTS,V2_CHECKPOINTS,V3_CHECKPOINTS,V3_IMPLEMENTATION_PLAN,AI_EVALUATION,KNOWN_LIMITATIONS}.md`, `takumi-backend/{IMPLEMENTATION_PLAN,V2_IMPLEMENTATION_PLAN}.md`.
