# Takumi Knowledge Engine

## Setup
From `takumi-backend/`, Python 3.11+ and uv:
```sh
uv sync
```

## Configuration
```sh
cp .env.example .env
```

Studio diagnosis uses `STUDIO_AI_MODEL` when set, then `GEMINI_MODEL`, then
`gemini-3.5-flash-lite`. Set `STUDIO_AI_MODEL` to a Gemini 3-series model available to your key to
configure the Expert agents independently of video extraction. Restart the backend
after changing `.env`. The local diagnosis configuration was verified with
`STUDIO_AI_MODEL=gemini-3.5-flash-lite` and `STUDIO_AI_TIMEOUT_SECONDS=60`.
Keep `.env` private. Defaults need no API key. Set `DEMO_SIGNING_SECRET` for tokens that survive restarts; empty generates a process-local key. Production requires all demo switches false and a future production identity integration.

## Start
```sh
uv run uvicorn app.main:app --reload --port 8000 --no-access-log
```

## Database
Startup creates SQLite tables, upgrades schema v1–v6 additively to v7 and installs the immutable-release UPDATE trigger. A new database automatically seeds `approved_baseline`, with `release_001` active. Existing `task_001` is never overwritten. Unsupported schema versions fail startup; back up before changing the database. Services flush; routers, lifespan, scripts and worker own transactions.

## Demo
```sh
uv run python scripts/seed_demo.py --state approved_baseline
uv run python scripts/demo_flow.py --base-url http://localhost:8000
```
The flow requires a running server (see Start). It logs 16 successful steps, resets only `task_001`, submits an expert answer, publishes and proves the answer is cited. All mechanical example content is illustrative, not a validated procedure.

## Reset
```sh
uv run python scripts/reset_demo.py --state fresh --yes
```
This replaces only fixture task `task_001` and its related records; user-created tasks survive. It refuses production/non-demo deployments. API reset requires reviewer auth and `confirm: "RESET_DEMO_DATA"`. Choose `fresh` before a live expert demonstration; choose `approved_baseline` for an immediately usable apprentice pack. Attempts belong to the scenario engine and cannot be reset here.

## API
Interactive docs: `/docs`. Generated schema: `contracts/openapi.json`. Teammate guide: [INTEGRATION_HANDOFF.md](docs/INTEGRATION_HANDOFF.md). Regenerate checked-in examples and schemas:
```sh
uv run python scripts/export_contracts.py
```
The canonical source is `../markdowns/hackathon/openapi/API.doc.md`, not the conflicting generated specs there.

## Tests
```sh
uv run pytest tests/test_demo.py tests/test_integration.py
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```
Tests isolate databases. The integration test runs the full flow twice.

## Media (C4)
Install `ffprobe` on PATH (`ffmpeg` is also needed for generated-video tests). Set `ENABLE_MEDIA_UPLOAD=true` and `ENABLE_MEDIA_STREAMING=true`.
Experts/reviewers upload multipart field `file` to `POST /api/v1/tasks/{task_id}/media`.
Only MP4 containers with h264/av1 video are accepted; defaults limit uploads to 100 MiB and 240 seconds.
Files stay under `MEDIA_STORAGE_DIR=./data/media`, not a public static directory.
`GET /api/v1/media/{media_id}` returns metadata and a reusable 10-minute signed playback URL.
Streaming supports byte ranges. Apprentices see media only when a release snapshot cites it.
Run the server with `--no-access-log`: default access logs would record playback URL tokens.

## Live extraction (C5)
Set these values in the private `.env` used by both server and worker:
```dotenv
AI_PROVIDER=gemini
GEMINI_API_KEY=<your-key>
GEMINI_MODEL=<gemini-3-series-model-available-to-your-key>
ENABLE_MEDIA_UPLOAD=true
ENABLE_MEDIA_STREAMING=true
ENABLE_MEDIA_PROCESSING=true
ENABLE_LIVE_EXTRACTION=true
ENABLE_PROCESSING_WORKER=true
```
No extraction model is selected by default. Live extraction with `gemini-3.5-flash-lite` succeeded on an earlier synthetic clip and the Navy clip in [Final acceptance (§26)](docs/V2_CHECKPOINTS.md#final-acceptance-26). The Navy run required human rejection of an unsupported unit before publication. Quality is promising, not validated.
Start server and worker in separate terminals from `takumi-backend/`, sharing `DATABASE_URL`, `MEDIA_STORAGE_DIR` and configuration:
```sh
uv run uvicorn app.main:app --port 8000 --no-access-log
uv run python scripts/run_worker.py
uv run python scripts/video_flow.py --video demo.mp4
```
The script creates a new task, uploads, checks a range, submits a job, polls and prints extracted units with evidence intervals.
`POST /process` returns 202 immediately; the worker builds an unreviewed draft, never a release.
Continue through the existing clarify → publish → training-pack → ask flow.
Existing drafts reject new processing with 409 `DRAFT_ALREADY_EXISTS`; use a new task.
`--mode demo` cannot analyze uploads and returns 422 `FIXTURE_NOT_AVAILABLE`; use `scripts/demo_flow.py` for the seeded fixture.
Without live configuration, processing returns 503 `AI_PROVIDER_UNAVAILABLE`; uploaded media remains available.

Code path: `app/api/media.py` → `app/services/media_service.py` → `app/services/processing_service.py` → `app/services/processing_worker.py` → `app/ai/gemini_provider.py` / `app/ai/prompts/video_extraction_v1.py` → `app/services/extraction_service.py`.

### Local models (Ollama + whisper.cpp)

Run AI on a separate MacBook Air M2 on the LAN; the backend host needs only `uv`, `ffmpeg` and `ffprobe`, not local AI software. In both backend server and worker environments, replace the Gemini settings with:
```dotenv
AI_PROVIDER=ollama
OLLAMA_BASE_URL=http://192.168.1.42:11434
OLLAMA_MODEL=qwen2.5:7b
WHISPER_BASE_URL=http://192.168.1.42:8080
ENABLE_MEDIA_UPLOAD=true
ENABLE_MEDIA_STREAMING=true
ENABLE_MEDIA_PROCESSING=true
ENABLE_LIVE_EXTRACTION=true
ENABLE_PROCESSING_WORKER=true
PROCESSING_PROVIDER_TIMEOUT_SECONDS=300
PROCESSING_JOB_LEASE_SECONDS=360
```
Replace the example IP with the Mac's LAN address. All three Ollama settings are required; missing values fail startup with `AI_PROVIDER=ollama requires OLLAMA_BASE_URL, OLLAMA_MODEL and WHISPER_BASE_URL`.

On the Mac (quit any Ollama menu-bar app first so port 11434 is free):
```sh
brew install ollama whisper.cpp
curl -fL https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en-q5_1.bin -o ggml-small.en-q5_1.bin
```
Keep these running in separate Mac terminals; `ollama pull` needs the server already up:
```sh
OLLAMA_HOST=0.0.0.0:11434 ollama serve                  # terminal 1
ollama pull qwen2.5:7b                                   # terminal 2, once serve is up
whisper-server --model ./ggml-small.en-q5_1.bin --host 0.0.0.0 --port 8080 --language en --threads 4   # terminal 3
caffeinate -dimsu                                        # terminal 4
```
Then use the server, worker and `video_flow.py` commands above on the backend host.

The backend extracts 16 kHz mono WAV with ffmpeg, sends it to whisper-server `/inference`, then sends the transcript to Ollama `/api/chat` with JSON-schema output. The text-only model reads narration only: visual-only knowledge is not captured. Steps use `source_modality="spoken"`; detected language is whisper's language name (for example, `english`). Q&A/gaps remain deterministic.

Recordings without an audio track return 422 `MEDIA_HAS_NO_AUDIO` at processing; a non-speech-only transcript fails the job with non-retryable `NO_SPEECH_DETECTED`. For an 8 GB Mac under memory pressure, try `ggml-base.en-q5_1.bin`, then `OLLAMA_MODEL=qwen2.5:3b`. For Japanese, download `ggml-small-q5_1.bin` instead and use `--language auto`.

**Use trusted demo Wi-Fi only.** Audio and transcripts travel over plain HTTP; upstream whisper-server warns against untrusted networks. **The live Mac run is unverified:** real whisper.cpp transcription, Qwen quality/speed, Mac RAM, LAN reachability and Windows/other demo-host OS behavior remain unverified. Linux tests and a stub-service smoke passed; stubs are not real models. See [V2_CHECKPOINTS.md](docs/V2_CHECKPOINTS.md#ollama-transcript-provider).

### OpenRouter extraction + local whisper.cpp

This option keeps the existing local whisper.cpp transcription stage and sends only the timestamped transcript to OpenRouter for extraction. Configure the backend server and worker with:
```dotenv
AI_PROVIDER=openrouter
OPENROUTER_API_KEY=your-key
OPENROUTER_MODEL=xingchen-agi/xing4.0-29b-a4b:free,nvidia/nemotron-3.5-super-vl:free,openrouter/free
WHISPER_BASE_URL=http://192.168.1.42:8080
ENABLE_MEDIA_UPLOAD=true
ENABLE_MEDIA_STREAMING=true
ENABLE_MEDIA_PROCESSING=true
ENABLE_LIVE_EXTRACTION=true
ENABLE_PROCESSING_WORKER=true
PROCESSING_PROVIDER_TIMEOUT_SECONDS=300
PROCESSING_JOB_LEASE_SECONDS=360
```
When whisper-server runs on the same Mac as this backend, use `WHISPER_BASE_URL=http://127.0.0.1:<whisper port>` (for example `http://127.0.0.1:12017`). If the Technical Viva API in `backend/` already uses port 8000, start this server with `--port 8001` and pass `--base-url http://localhost:8001` to `scripts/video_flow.py`.

The existing WhisperServer.app on the Mac is also supported. It listens on IPv6 loopback, so use `WHISPER_BASE_URL=http://[::1]:12017` for a backend on that Mac. The backend tries whisper.cpp's `/inference` first and, on HTTP 404, uses `/v1/audio/transcriptions` with `response_format=verbose_json` and segment timestamps. WhisperServer.app uses its selected default model; no second model server is needed. Check this variant with:
```sh
curl --noproxy '*' -g 'http://[::1]:12017/v1/models'
curl --noproxy '*' -g 'http://[::1]:12017/v1/audio/transcriptions' \
  -F file=@/tmp/p.wav -F response_format=verbose_json \
  -F 'timestamp_granularities[]=segment'
```

`OPENROUTER_MODEL` is a comma-separated fallback list sent as OpenRouter's `models` array: when a model errors (downtime, rate limit), OpenRouter tries the next one. Requests set `provider.require_parameters=true`, so they route only to providers that honor the strict JSON schema. The default list uses free models that advertise structured outputs; keep it `:free`-only (plus `openrouter/free`) to avoid charges. Free models are rate-limited (about 50 requests/day without purchased credits). Output still validates against the application schema; invalid output fails as retryable `AI_RESPONSE_INVALID`. The job's `model_id` records the model that actually answered. OpenRouter receives the transcript, not the original video/audio; transcription stays on the trusted LAN. A synthetic live smoke passed as described below; extraction quality on real expert recordings remains unverified.

## Verification status
Mac smoke (2026-10-25): a generated, narrated 13.1-second MP4 passed real upload, HTTP 206 playback, local WhisperServer.app transcription and live OpenRouter extraction into an unreviewed draft with four timestamp-linked knowledge units. The first Nemotron request did not complete after more than seven minutes; it was manually interrupted, the worker reclaimed the expired lease, and prioritizing the Xing4 free fallback completed the same job using its cached transcript.

That run exposed the difference between HTTPX per-chunk read timeouts and a total deadline. OpenRouter chat calls now also have a wall-clock deadline set by `PROCESSING_PROVIDER_TIMEOUT_SECONDS`, covering connection through the complete response body. Expiry cancels the request, closes the client and returns retryable `PROCESSING_TIMEOUT`; the worker applies its existing retry budget. OpenRouter model fallback still depends on upstream errors; a local timeout does not rotate the model list. Total job time can include transcription, extraction and retries.

Transcription now shares one `PROCESSING_PROVIDER_TIMEOUT_SECONDS` budget across FFmpeg conversion and the complete Whisper HTTP response, including the 404-only fallback from `/inference` to `/v1/audio/transcriptions`. Conversion consumes the budget available to Whisper; fallback never resets it. FFmpeg timeout or HTTP deadline expiry returns retryable `PROCESSING_TIMEOUT`, closes the local HTTP client and cleans temporary audio. Expired results are rejected before transcript caching. Closing the connection does not guarantee that the remote Whisper server stops inference. This applies to both OpenRouter and Ollama transcription; Ollama chat extraction still uses HTTPX I/O timeouts.

The worker renews its lease after transcription and immediately before extraction. With a 300-second provider timeout and a 360-second lease, transcription and OpenRouter extraction each have their own renewed lease window; they do not share one 360-second budget. Normal scheduling and cleanup have 60 seconds of margin. This is not a guarantee against machine suspension, process stalls or arbitrarily slow local filesystem operations.

This is a synthetic integration check, not validation of real demonstration quality. A follow-up live smoke with the OpenRouter deadline enabled and Xing4 first completed in 12 seconds and produced three timestamp-linked action units. After the shared transcription deadline fix, another live smoke completed in 18 seconds with three timestamp-linked action units. Current Takumi checks: 228 passed, one opt-in Gemini test skipped; ruff check and format passed.

C4 gates: 113 tests passed; real-server upload and range smoke passed.
C5 gates: 192 tests passed, 1 opt-in live test skipped; ruff check/format clean.
Processing, retries, recovery and V1 integration use mocked provider tests.
**Live Gemini extraction is unverified: no API key is available.** An invalid-key preflight failed safely; it is not proof of provider availability.
Opt in with a real key, model and recording:
```sh
TAKUMI_LIVE_GEMINI=1 GEMINI_API_KEY=… GEMINI_MODEL=… TAKUMI_LIVE_VIDEO=/path/to.mp4 uv run pytest tests/test_gemini_live.py
```
See [V2_CHECKPOINTS.md](docs/V2_CHECKPOINTS.md) and [V3_CHECKPOINTS.md](docs/V3_CHECKPOINTS.md) for observed checkpoint evidence and final verification status. V3 evaluation: [AI_EVALUATION.md](docs/AI_EVALUATION.md); `uv run python scripts/run_evals.py --mode deterministic --output /tmp/takumi-v3-evals.json`; HTTP acceptance: `uv run python scripts/v3_flow.py --task-id <task> --generate-only`.

## Feature flags
| Setting | Default | Behavior |
|---|---|---|
| DEMO_MODE, ENABLE_DEMO_AUTH, ENABLE_DEMO_SEED, ENABLE_DEMO_RESET | true | Development login, startup seed, reviewer reset |
| DEMO_SEED_STATE | approved_baseline | Initial release_001 |
| ENABLE_GROUNDED_QA | true | Answers only from release snapshots |
| ENABLE_TRAINING_PACK_EXPORT | true | Immutable release export |
| AI_PROVIDER | fixture | fixture, gemini, ollama or openrouter; Q&A/gaps stay deterministic unless the V3 live flags below are on |
| ENABLE_MEDIA_UPLOAD, ENABLE_MEDIA_STREAMING | false | Validated uploads and private ranged playback (C4) |
| ENABLE_LIVE_EXTRACTION, ENABLE_MEDIA_PROCESSING, ENABLE_PROCESSING_WORKER | false | Gemini, Ollama or OpenRouter extraction, durable jobs and separate worker (C5) |
| ENABLE_LIVE_GAP_GENERATION | false | Gemini contextual clarifications via `POST /tasks/{id}/clarifications/generate` (V3 C6A); requires AI_PROVIDER=gemini |
| ENABLE_LIVE_QA | false | Gemini grounded Q&A with exact approved-field claims (V3 C6B); requires AI_PROVIDER=gemini |
| ENABLE_QA_SUPPORT_VALIDATION, ENABLE_QA_ABSTENTION | true | Must stay true when ENABLE_LIVE_QA=true |
| ENABLE_AI_EVAL_LOGGING, ENABLE_DETERMINISTIC_GAP_FALLBACK | true | Usage/latency and Q&A call logs; labelled deterministic fallback on gap-generation failure |
| ENABLE_MULTILINGUAL, ENABLE_EMBEDDINGS, ENABLE_ADVANCED_RETRIEVAL, ENABLE_AI_VIVA_SCORING, LOG_MODEL_INPUTS | false | Reserved |

Reserved capabilities fail at startup when enabled. Disabled optional endpoints return HTTP 501 `FEATURE_DISABLED`. Core lifecycle and authentication have no off switch. `.env.example` lists every setting.

## Limitations
See [KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md). Media and Gemini extraction code ship here, but successful live Gemini extraction is unverified. No production identity, frontend or scenario/viva engine ships here. Q&A abstains on absent specifications.

## Expert Studio demo workflow

The current integrated demo is documented in the [root README](../README.md#one-button-diagnosis-demo) and [remediation runbook](../audits/2026-10-25-demo-remediation.md). `/studio` diagnoses source notes with three bounded AI roles, proposes source-extractive patches, evaluates them, and waits for human approval before applying/publishing. Missing keys show an unconfigured live state. Illustrative rehearsal requires explicit selection and `mode: rehearsal` in the request. A live failure never falls back to fixtures.

In `DEMO_MODE=true`, both Studio and legacy publication endpoints require a fresh passing evidence evaluation and explicit human publication. No separate admin grant is required. Outside demo mode, the revision/content-specific authorization remains required. Demo identities are not production account management.

AI analysis records and stages live in `studio_analysis_runs`; schema 7 adds saved course scripts and expert notes in `studio_course_content`. Provider calls run outside database transactions. Approving selected proposals checks the draft revision and source snapshot, creates attributed evidence, applies patches, evaluates and publishes atomically. Unanswered prompts require explicit deferral or a recorded response. Existing clarification responses can carry `knowledge_update` to apply a selected field.

In **AI diagnosis → Course video & script**, upload the course MP4 and upload or paste its text script. The panel automatically uses these saved inputs. **Expert review → Expert notes for tacit extraction** saves optional expert observations; choose **Include saved expert review notes in course extraction** before the next run. Gemini receives the actual uploaded video and produces timestamped observations and structured tacit proposals. Review previews the extracted units before you save the replacement draft or explicitly publish it. Source edits invalidate earlier proposals. MP4 upload requires `ENABLE_MEDIA_UPLOAD=true` and server ffprobe; live extraction requires `GEMINI_API_KEY`. A configured course playback URL alone is not an uploaded source file.

Full fixture reset now also clears learner enrollments, manual completions and diagnosis runs. It is destructive and must be explicitly requested; it is not part of preflight. Learner-only reset uses `/api/v1/learning/reset` and leaves published course content in place. A new publication starts a fresh learning context; historical learner reports across releases remain future work.
