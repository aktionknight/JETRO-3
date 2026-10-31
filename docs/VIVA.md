# Takumi Technical Viva

A release-grounded technical interview with an animated AI examiner, spoken or typed answers, bounded follow-ups, and an evidence-linked evaluation report. The report includes the original Playground outcome, answer excerpts, assessment limitations, and every document in the interview's approved knowledge release.

The detailed requirements and roadmap live in [the implementation specification](../markdowns/globals/03-technical-viva-implementation-spec.md). [The hackathon guide](../markdowns/hackathon/README.md) explains team ownership and the Studio/Playground integration. The project overview is in the [root README](../README.md).

This repository also contains the Takumi knowledge engine ([takumi-backend/README.md](../takumi-backend/README.md), port 8001) and the apprentice learning playground with the 3D bearing simulation and Studio admin UI ([PLAYGROUND.md](../PLAYGROUND.md)). The Viva API uses port 8000; the playground's `npm run dev` starts the knowledge engine on 8001, so all three run side by side.

Run all commands below from the repository root.

## Run locally

Use Python 3.12 and Node.js 22. The backend seeds illustrative demonstration knowledge and completed Playground attempts so Viva can be exercised independently of a full Studio capture or Playground renderer. Instructors must review these illustrative sources and task-specific rubrics before real learner assessment.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt
cd frontend
npm install
cd ..
```

Start the API and development frontend in separate terminals:

```sh
VIVA_PROVIDER_MODE=fixture VIVA_DEMO_DATABASE=true VIVA_DATABASE_URL=sqlite:///./var/viva.db .venv/bin/python -m uvicorn app.main:app --app-dir backend --reload
```

```sh
cd frontend
NEXT_PUBLIC_API_BASE=http://localhost:8000/api/v1 npm run dev
```

Open `http://localhost:3000`. The default demo learner token is `demo-learner-token`. The API's interactive documentation is at `http://localhost:8000/docs`.

Fixture mode is a visibly labeled offline simulation. It uses fixed provider responses for development and repeatable tests; it does not provide live speech or reliable assessment of arbitrary reasoning. Select a fresh seeded attempt or run the guarded reset before repeating an interview.

## Real provider mode

Set the backend environment to `VIVA_PROVIDER_MODE=openai`, supply `OPENAI_API_KEY`, choose account-accessible `VIVA_TEXT_MODEL` and `VIVA_VOICE_MODEL` identifiers, and configure `VIVA_PRICING_JSON` with verified dated USD rates (`version`, `effective_date`, `currency`, `text_input_per_million`, `text_output_per_million`, `audio_per_minute`). Live calls fail closed when pricing is absent. Audio pricing must be a conservative combined per-minute bound for the selected voice model; recorded costs are estimates. Provider keys remain server-side. The browser negotiates WebRTC through the authenticated backend, and the server owns interview progression and sideband control.

Verify model/account access with a short actual interview before presenting this as a live demonstration. Network failures preserve the interview and expose retry or text mode. A provider outage must not silently switch to fixture responses. Model aliases in `.env.example` are configuration defaults, not evidence of account availability or compatibility.

## Docker Compose

```sh
cp .env.example .env
docker compose up --build
```

Open `http://localhost:8080` for the Technical Viva. Compose runs a Next.js frontend, FastAPI API, separate evaluation worker, and Caddy reverse proxy. SQLite and media share the persistent `viva-data` volume. `docker compose down` preserves the database; `down -v` removes it and must only be used for disposable demos.

The same `docker compose up --build` also serves the apprentice playground and the Takumi knowledge engine on `http://localhost:8081` (`/`, `/studio`, `/studio/admin`; `/api/*` goes to `takumi-api`). They use a second origin because both apps own `/` and `/api/v1/studio/...` paths. `takumi-api` reads `takumi-backend/.env` when it exists (optional), stores SQLite and media in the `takumi-data` volume, and reaches host services such as Ollama or whisper at `http://host.docker.internal:<port>` (bind them to `0.0.0.0`). The video-processing worker needs live-AI settings, so it is opt-in: `docker compose --profile takumi-worker up -d`.

The supplied proxy is for local development. For remote microphone access, serve a real hostname over HTTPS: replace the `:80` site in `deploy/Caddyfile` with that hostname, publish ports 80/443, and configure DNS. Configure the backend's allowed origin to match the browser origin. Do not expose a public deployment with the seeded demo tokens. Non-demo configuration must disable seeding (`VIVA_SEED_DEMO=false`), set `VIVA_DEMO_TOKENS={}`, and configure the OIDC issuer/audience/HTTPS JWKS URL. Production authentication accepts RS256-signed OIDC JWTs with required identity/expiry claims, tenant identity, and application roles; static tokens are disabled.

## Tests and verification

Tests are written before the corresponding implementation. The initial workspace has no installed backend/frontend dependencies and restricted package-registry DNS; dependency-free frontend/provider checks can run, but full API/browser/build checks require package installation. Generate and review the frontend lockfile when registry access is available. Run the full suite once dependencies are installed:

```sh
.venv/bin/python -m pytest backend/tests -q
cd frontend
npm run typecheck
npm test
npm run build
cd ..
git diff --check
```

Or run `scripts/verify.sh`. Against a running disposable fixture deployment, `python3 scripts/demo_smoke.py` creates an interview, finishes early, waits for evaluation, and checks report provenance and its source index.

The backend acceptance suite covers ownership, release binding, idempotency, stale revisions, question order, duplicate completion, event cursors, and source scope. Provider validation tests cover malformed assessments and fabricated citations. Frontend unit checks cover event replay, revision handling, caption parsing, authenticated requests, and safe source paths. A browser integration scenario is included for the interview/report flow. Live voice requires a real browser, microphone permission, network access, and provider credentials; unit tests do not establish that integration's operational availability.

## Operations

Use a dedicated demo database with `VIVA_DEMO_DATABASE=true`. The guarded reset command is:

```sh
cd backend
../.venv/bin/python -m app.cli reset-demo --confirm-demo
```

Reset refuses a non-demo database and active connections/jobs. It preserves approved Studio knowledge and source artifacts. Pause or finish interviews and let jobs settle before resetting. Evaluation workers can be run separately with `python -m app.worker` from `backend`; inline processing is available for local/test operation.

Transcripts and reports are sensitive learner data. Raw audio/video retention is disabled. Scope access to the original attempt owner and release, keep provider keys out of browser configuration/logs, and back up the database and media together. Reports use immutable source identifiers; expired file links should be resolved again through the authorized API.

### Voice ownership schema rollout

Migration `0004_voice_question_ownership` stores the question that accepted each pending voice item. Stop all old API/worker writers, back up the database, set `VIVA_DATABASE_URL` to that database, then run `alembic -c alembic.ini upgrade head` from `backend/` using the backend environment. Start only the new API/worker version afterward; mixed versions are unsupported. Demo `create_all` does not upgrade existing tables.

For an unversioned existing database, compare its complete schema against the historical migrations before choosing a baseline. Do not blindly stamp a revision or reset data. Reconcile the baseline separately, preserving learner records, before upgrading.

Legacy pending items have unknown question ownership and cannot become finalized answers. Downgrading to `0003_pending_voice` drops only the new nullable ownership column; transcripts/reports remain, but captured pending question ownership is lost.


## Production readiness

This repository implements the Viva application and its local demo foundation. Production rollout still requires configuring and testing the organizational OIDC issuer and tenant assignment, reviewed task-specific rubrics, actual provider compatibility checks, an expert-calibrated evaluation benchmark, infrastructure deployment, restore exercises, and measured load/accessibility checks. The specification's production roadmap is a release checklist, not a claim those operational activities have occurred.

The SQLAlchemy backend supports PostgreSQL; configure a psycopg-enabled runtime and apply/reconcile migrations before cutover. OIDC verification and source/retrieval/storage primitives are implemented, but require deployment-specific integration checks. Redis/Celery scheduling, authorized object-storage resolution, retrieval adapters, and PDF export are included. Production pgvector indexing and a reviewer override interface remain integration work; all optional adapters require checks against actual infrastructure before enabling them. Merely setting the reserved environment values in `.env.example` does not enable integrations that are not implemented. The printable HTML report is the default export.

Never describe seeded sources as a live Studio publication, fixture replies as a live AI interview, or an AI-assisted score as practical certification.


## Source adapters and stored PDF export

`backend/app/knowledge.py` provides checksummed UTF-8 text, DOCX paragraph, and optional PDF page extraction; overlapping chunks; and tenant/task/release-filtered lexical/vector reciprocal-rank retrieval. The default chunk budget counts lexical tokens. Supply the selected model's encode/decode functions for exact token budgets, and configure indexing/embedding generation before production use. Scope filtering precedes ranking and unavailable sources are excluded.

`backend/app/storage.py` provides authorized immutable local writes/reads with traversal checks and an optional private S3 presigner. The caller must obtain source scope from trusted release metadata rather than browser input. URLs expire within five minutes. The demo's inline source records remain the default; these adapters must be wired to the organization's actual content ingestion pipeline.

Optional document/storage/PDF dependencies:

```sh
.venv/bin/python -m pip install -r backend/requirements-extras.txt
.venv/bin/python -m playwright install chromium
```

A local operator can export a stored validated report without model rewriting:

```sh
cd backend
../.venv/bin/python -m app.pdf_export --input /path/to/report.json --output /path/to/report.pdf
```

The renderer escapes report content, disables JavaScript, blocks external network requests, and preserves report provenance and the full source index. Actual Chromium rendering and visual pagination must be verified in the deployment environment; HTML serialization tests do not establish PDF visual quality.


## Retention and optional distributed tasks

The local maintenance commands remove learner-derived Viva sessions, transcripts, reports, job snapshots, events, usage rows, and associated idempotency responses while preserving approved releases and original Playground attempts:

```sh
cd backend
../.venv/bin/python -m app.cli purge-retention
../.venv/bin/python -m app.cli delete-learner INTERNAL_LEARNER_ID --confirm-delete
```

Retention defaults to `VIVA_RETENTION_DAYS=30` and considers recent answer/report activity. Both commands skip active interviews, live connections, queued/running jobs, and unsettled provider reservations; their result lists deleted and skipped session IDs. Audit records retain IDs/action names without transcript text. These are local operator commands, not public endpoints. Exported PDF copies or external content caches require their own storage lifecycle policy.

An optional Celery/Redis adapter uses the same database-leased evaluator. After installing `backend/requirements-extras.txt`, set `VIVA_REDIS_URL` and run separate processes from `backend`:

```sh
celery -A app.tasks:celery_app worker --loglevel=INFO
celery -A app.tasks:celery_app beat --loglevel=INFO
```

Beat checks evaluation jobs every two seconds and schedules a daily retention purge. Configure only one beat scheduler per deployment. Set `VIVA_RUN_INLINE_WORKER=false` when using distributed workers; database leases protect duplicate evaluation scheduling. Broker encryption, authentication, availability, actual Celery execution, and production load remain deployment verification requirements.


Before starting a production database, install dependencies and apply migrations from `backend` with `../.venv/bin/alembic upgrade head`. Production startup verifies the schema and does not create tables automatically. The demo can initialize a fresh SQLite schema directly.
