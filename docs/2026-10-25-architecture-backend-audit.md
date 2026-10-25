# Repository architecture and backend audit

Date: 2026-10-25\
Audited revision: `e2beace`, plus preserved local changes\
Branch: `Manas`\
Status: All ten ranked code findings resolved; final publication verification and operational limits appear below

## Scope and assumptions

Takumi captures expert knowledge, publishes reviewed releases, and trains apprentices through simulation and interviews. This audit covered both backend applications, both frontend trees, deployment configuration, and relevant tests. Review was risk-based, not a claim that every source line was inspected.

Assumed load: several demo users, one API process per backend, and separate workers. Documented demo limitations were distinguished from defects affecting that deployment.

Four parallel review slices covered:

- Knowledge engine: authentication, media, drafts, clarifications, publication, grounded Q&A, and processing workers.
- Studio and learning: extraction, diagnosis, approval, publication, progression, and live interviewing.
- Technical Viva: authentication, voice, evaluation, budgets, storage, retention, and migrations.
- Frontends: Studio, course player, simulation, interviews, API clients, and state lifecycles.

Parent review covered deployment and integration boundaries, test execution, and isolated reproductions. The ranked findings below have execution evidence; broader static concerns were not promoted without equivalent validation.

## Repository update

Pulled `origin/main` into `Manas`, advancing `4d85929` to `e2beace`.

The pull initially stopped because incoming changes overlapped local edits in `src/components/CourseHub.tsx` and `takumi-backend/app/ai/studio_analysis.py`. Tracked edits were preserved in a stash named `pre-main-audit-local-fixes`, then reapplied after the pull.

Upstream already implemented the corresponding playback-identity and Gemini-schema fixes. Conflicts were resolved using those upstream implementations, and the duplicate local schema helper was removed. Local regression tests and Playwright configuration changes were retained. The stash backup was retained; no commit or push was performed.

## Architecture assessment

- **The knowledge engine has useful boundaries.** Releases are immutable, draft updates use optimistic revisions, evidence is attached at field level, and the reviewed AI paths release database transactions before provider calls.
- **Two interview implementations coexist.** Standalone Technical Viva and the knowledge-engine live interview use separate authentication, persistence, and recovery paths. Their guarantees differ.
- **The weakest boundary is the new live interview handler.** `takumi-backend/app/learning/live_interview.py` combines WebSocket transport, provider calls, timing, grading, and persistence. Findings 1 and 3 demonstrate failures crossing those responsibilities.
- **SQLite serialization is deliberate.** The knowledge engine starts transactions with `BEGIN IMMEDIATE`. This protects concurrent writes but also serializes reads. It is a documented demo tradeoff, not proven under sustained load.

## Must fix

### 1. Gemini API key reaches apprentices

**Location:** `takumi-backend/app/learning/live_interview.py:150,178,483–486`\
**Priority:** P1

- **What this is:** Live interviewing sends provider requests with the API key in the URL query string.
- **Problem:** HTTPX logs that URL. Failed opening requests also expose the raw exception, including the key, through WebSocket error messages.
- **Fix:** Send credentials through headers. Return sanitized errors and never forward raw provider exceptions.
- **If skipped:** An apprentice can obtain a billable provider credential.

**Evidence:** An isolated HTTPX transport returning a provider error exposed a fake credential in both the exception and INFO logs. An actual FastAPI TestClient WebSocket connection received that fake credential in its error event. No real credential was used or printed.

### 2. Viva retries and lease recovery crash the worker

**Location:** `backend/app/worker.py:21–29,94–95`\
**Priority:** P1

- **What this is:** Workers claim queued evaluations and recover jobs whose leases expired.
- **Problem:** SQLAlchemy evaluates the update predicate against loaded objects. SQLite returns timezone-naive timestamps, while the comparison timestamp is timezone-aware. The claim runs outside the worker's recovery handler.
- **Fix:** Add `execution_options(synchronize_session=False)` to the claim update. The existing `expire_all()` already refreshes loaded objects.
- **If skipped:** Retried jobs remain stuck. The standalone worker can exit, and the oldest eligible job can repeatedly block later work.

**Evidence:** Three existing worker tests failed with `TypeError: can't compare offset-naive and offset-aware datetimes`:

- `test_expired_lease_can_be_reclaimed`
- `test_transient_failure_retries_same_frozen_snapshot`
- `test_invalid_score_never_publishes_and_retry_budget_is_bounded`

The global queue-blocking consequence follows from the unchanged oldest-job selection after rollback; a sustained multi-job outage was not separately exercised.

### 3. Disconnects discard accepted interview answers

**Location:** `takumi-backend/app/learning/live_interview.py:389–421,426–468`\
**Priority:** P1

- **What this is:** Live interviewing grades and persists its report after the conversation loop.
- **Problem:** A disconnect during response delivery causes a failed send. The error handler attempts another send, which fails again and bypasses grading and persistence.
- **Fix:** Handle outbound disconnects separately and continue finalization without requiring additional socket sends.
- **If skipped:** Learners lose accepted interview work after a connection drop.

**Evidence:** The real handler ran against an isolated seeded database with stubbed providers and a WebSocket transport that disconnected during examiner-response delivery. Results: `answer accepted=True`, `evaluation called=False`, `interview persisted=False`.

### 4. Microphone lifecycle ignores mute and examiner playback

**Location:** `src/components/VirtualInterview.tsx:133–161,211–239,278–288`\
**Priority:** P1

- **What this is:** Browser speech recognition automatically restarts while the component believes it is listening.
- **Problem:** Restart handlers capture stale listening and mute state. Separately, server `listening` messages override the speaking state while examiner audio is still playing.
- **Fix:** Disable restart and detach handlers before stopping each recognizer. Guard restart and listening transitions against current mute, component lifecycle, and playback state.
- **If skipped:** Capture can restart after mute or teardown. Examiner speech can also become learner input.

**Evidence:** The real React component was rendered in Chromium with mocked WebSocket, speech-recognition, and audio devices. Recognition started while audio remained unfinished. Clicking Mute produced additional recognition starts while the interface displayed the Unmute control. No real microphone was accessed.

### 5. Delayed Viva speech answers the wrong question

**Location:** `backend/app/service.py:194–209,380–402`\
**Priority:** P1

- **What this is:** Finalized voice transcripts become answers to the current interview question.
- **Problem:** A second utterance is not accepted while another utterance is pending, but its later finalization can still be committed against a newly issued question.
- **Fix:** Bind accepted utterance IDs to question IDs. Reject finalizations without matching accepted input.
- **If skipped:** Assessments attribute speech to questions the learner had not yet heard.

**Evidence:** An isolated service flow started utterances A and B before either finalized. After A generated a follow-up, B was committed against that follow-up. Observed question IDs were `question-explanation` for A and `question-explanation-follow-up` for B. No live provider was called.

## Should fix

### 6. Default Viva test collection is broken

**Location:** `backend/tests/test_reviews.py:10–11`\
**Priority:** P2

- **What this is:** Human-review tests import `app.reviews` and `ReportReview`.
- **Problem:** Neither implementation exists in the current repository. Full backend verification stops during collection.
- **Fix:** Remove deferred review tests from default discovery, or deliver their implementation and migration together. Do not present an excluded suite as full verification.
- **If skipped:** The normal verification command cannot provide a backend release gate.

**Evidence:** Full backend pytest execution failed during collection at the missing import. Repository inspection confirmed this was not merely an outdated container module.

### 7. Same-text expert confirmation loses its evidence

**Location:** `takumi-backend/app/services/knowledge_service.py:36–46`; `takumi-backend/app/services/clarification_service.py:49–56`\
**Priority:** P2

- **What this is:** Experts can confirm extracted text through clarification responses.
- **Problem:** Equal field text triggers an early `continue`, dropping supplied field-level confirmation evidence. The clarification consumer adds only the unit-level evidence reference afterward.
- **Fix:** Attach supplied confirmation evidence even when text stays unchanged. Preserve ordinary PATCH no-op behavior.
- **If skipped:** Text without field-level support remains unsupported despite explicit expert confirmation.

**Evidence:** Calling the actual shared update function with unchanged action text and a new confirmation evidence ID left field evidence empty. The resulting publication limitation follows from the field-evidence requirement; the full approval flow was not separately rerun for this case.

### 8. Fixture reset defeats stale-write protection

**Location:** `takumi-backend/app/services/seed_service.py:58–65,145–148`\
**Priority:** P2

- **What this is:** Demo reset deletes and recreates fixture content.
- **Problem:** Recreation reuses the draft ID, unit IDs, and revision `1`. A pre-reset browser tab can submit an update that still passes the revision check.
- **Fix:** Preserve a monotonic revision or change the draft generation identity across resets.
- **If skipped:** Stale pre-reset browser state can overwrite recreated content.

**Evidence:** In an isolated database, a draft was read, the demo was reset, and a PATCH used the cached pre-reset revision. The server returned HTTP 200 instead of a revision conflict.

### 9. Gap generation discards candidates before finding unanswered gaps

**Location:** `takumi-backend/app/services/gap_detection.py:60–70`; `takumi-backend/app/services/gap_generation_service.py:210–226`\
**Priority:** P2

- **What this is:** Deterministic generation selects a bounded set of clarification questions.
- **Problem:** Selection happens before previously asked unit/gap pairs are excluded. Remaining candidates never refill the result.
- **Fix:** Exclude existing pairs before ranking and applying the selection cap.
- **If skipped:** Missing knowledge can receive no clarification despite available unasked candidates.

**Evidence:** The fresh fixture had three unasked pairs: `ku_002/beginner_mistake`, `ku_003/missing_reasoning`, and `ku_003/beginner_mistake`. The generation endpoint returned HTTP 200, status `completed`, and an empty `questions` array.

### 10. Spoken answers lose earlier phrases

**Location:** `src/components/VirtualInterview.tsx:179–203`\
**Priority:** P2

- **What this is:** Speech-recognition events become a debounced learner answer.
- **Problem:** Each event resets accumulated text and reads only results starting at the new `resultIndex`. Later events replace earlier finalized phrases.
- **Fix:** Accumulate finalized text for the current answer and append current interim text. Clear the accumulated answer only after submission.
- **If skipped:** Grading receives incomplete reasoning.

**Evidence:** The real component received two successive finalized recognition results before its debounce elapsed. Chromium observed a `user_turn` containing only the second phrase: `Then I record the measured clearance.`

## Verification results

| Check | Observed result |
| --- | --- |
| Knowledge backend | 399 passed; live Gemini test excluded |
| Playground unit tests | 33 passed |
| Playground TypeScript | `tsc --noEmit` passed |
| Viva frontend unit tests | 14 passed |
| Viva backend | Full collection blocked by missing review implementation |

After explicitly excluding `tests/test_reviews.py`, the Viva container suite produced **97 passes and 10 failures**. Three failures expose finding 2. Seven failures came from contract documents absent from the container's filesystem; these are not established repository defects.

Verification commands:

```sh
# From takumi-backend/
.venv/bin/python -m pytest -q --ignore=tests/test_gemini_live.py

# From repository root
npm test
./node_modules/.bin/tsc --noEmit

docker compose exec -T api python -m pytest tests -q -p no:cacheprovider

# Explicitly partial diagnostic run; not a passing full-suite substitute
docker compose exec -T api python -m pytest tests -q -p no:cacheprovider --ignore=tests/test_reviews.py

# From frontend/
npm test
```

Additional evidence came from isolated HTTP/WebSocket and service probes, plus Chromium execution of the real interview component with mocked external devices. Temporary databases were removed and the temporary frontend server was stopped. Probe scripts were not added to the repository.

## Verdict and limits

Fix findings **1 and 2 first**, then interview capture and persistence failures. Passing knowledge-engine tests do not cover the reproduced edge cases above.

No audit fixes, dependency changes, migrations, commits, or pushes were performed. Only pull-conflict reconciliation preceded the read-only audit; this document was added afterward at the user's request.

Not checked: paid providers, real microphone hardware, the full end-to-end suite, production identity integration, sustained load, or a line-by-line inspection of every file. Public deployment remains unsafe while credential disclosure is present.

## Repair verification — 2026-10-25

The sections above retain the original audit evidence and pre-repair verdict. All ten ranked findings are now repaired in the working tree; no commit, push, production deployment, credential change, or existing database migration/reset was performed.

### Changes

| Finding | Repair |
| --- | --- |
| 1 | Both live Gemini helpers authenticate through `x-goog-api-key`, not URL queries. Logs retain exception types only; clients receive `Interview unavailable. Please try again.` |
| 2 | Worker claims use `synchronize_session=False`, preserving SQL eligibility, lease guards, retries, and explicit refresh. |
| 3 | A local transport-aware send helper stops sending after disconnect. Accepted turns enter history before acknowledgements, grading/persistence proceeds offline, and timer cancellation is awaited. |
| 4 | Capture uses current ownership/status/mute/playback refs. Teardown detaches callbacks before abort; playback owns capture before awaiting resume and cannot reopen it after terminal events. |
| 5 | `pending_question_id` binds accepted voice items to their original question. First-observation receipts prevent overlap reuse; commit rechecks ownership after provider planning. Matching-item cleanup cannot release another item's drain waiter. |
| 6 | Removed only unsupported `backend/tests/test_reviews.py`; human-review overrides remain deferred. Default backend collection now works without exclusions. |
| 7 | Explicit same-text nonnull confirmations add deduplicated field evidence. Equal nulls and ordinary unchanged PATCH requests remain provenance-neutral. |
| 8 | Fixture reset retains stable IDs but advances revisions. Fresh reset uses previous revision plus one; approved baseline uses plus two. Demo acceptance compares captured revisions instead of literal `1`. |
| 9 | Deterministic gap selection excludes all previously asked unit/type pairs before ranking and batching, including provider-error fallback. |
| 10 | Recognition rebuilds the complete result list and preserves finalized prefixes across natural restarts. Debounce submits the full current answer; submission locks prevent duplicate turns and failed sends retain drafts. |

### Observed gates

| Gate | Result |
| --- | --- |
| Focused knowledge provenance/reset/gap/integration tests | 97 passed |
| Focused live interview/final evaluation tests | 52 passed |
| Full knowledge-engine suite, excluding paid `test_gemini_live.py` | 467 passed |
| Default Viva collection, no exclusions | 129 tests collected |
| Full Viva suite, no exclusions | 129 passed |
| Playground unit tests | 33 passed |
| Playground `tsc --noEmit` and production build | Passed |
| Focused rendered interview Playwright suite | 10 passed, no skips |
| Viva frontend unit tests | 14 passed |
| Viva frontend production build | Not completed: `next: command not found` in the host frontend environment |

Viva gates used the backend Compose image with the entire current repository mounted read-only, including contract documents. The migration test exercised `0003_pending_voice` → head → `0003_pending_voice` → head on disposable SQLite data, preserving historical rows. One new sideband fixture initially lacked pricing; adding fixture rates resolved it before the full passing run. Existing Starlette/httpx deprecation warnings remain. Vite reports its existing large-chunk warning.

The browser command exceeded the context tool's 30-second transport deadline. Its generated Playwright report independently recorded `total=10`, `expected=10`, `unexpected=0`, `flaky=0`, `skipped=0`, `ok=true`; `.last-run.json` also reported `passed`. The command was not blindly rerun.

### Actual-path smoke evidence

1. Disposable knowledge API on `8003`: all 16 `scripts/demo_flow.py` steps passed. A subsequent HTTP probe cached revision `5`, reset to `6`, rejected PATCH revision `5` with `409 REVISION_CONFLICT`, then accepted revision `6` and saved `7`.
2. Standalone actual ASGI WebSocket/API probe: examiner delivery failed after accepting `I check the approved evidence first.`. GET progress returned that exact saved answer and `assessment_status: assessed`. Regression tests also cover grading-unavailable persistence and failures at acknowledgement, examiner text/audio, fallback, timer, evaluating, and ended delivery.
3. Disposable Viva API on `8004` plus a separate worker: `scripts/demo_smoke.py` exited `0`, retrieved a persisted report with the matching release and three source entries. Early finish correctly produced an `incomplete` report. Fresh container temporary storage was used, never the existing `/data` volume.
4. Managed Chromium tab rendered the real component with mocked devices: unfinished examiner audio displayed speaking with zero recognizer starts. Audio completion enabled listening; a saved callback after mute caused no restart. Two phrases transmitted exactly `I inspect the approved evidence. Then I record the measured clearance.`. A screenshot was captured; the tab and disposable servers were closed.

### Rollout and remaining limits

Migration `backend/alembic/versions/0004_voice_question_ownership.py` must precede new Viva writers. Stop old API/workers, back up the database, configure `VIVA_DATABASE_URL`, run `alembic -c alembic.ini upgrade head` from `backend/` in the backend environment, then start only the new version. Demo `create_all` cannot upgrade an existing table. For unversioned existing databases, reconcile the complete historical baseline first; do not blindly stamp or reset. No live database was changed during these repairs.

Legacy pending items with unknown question ownership cannot become finalized answers. Downgrade removes only the nullable ownership column and retains transcripts/reports, but loses captured pending question metadata.

Previously exposed credentials may remain compromised. Header authentication and error sanitization do not revoke leaked keys; an operator must rotate potentially exposed credentials and review retained logs without redistributing secrets.

Not checked: paid-provider compatibility, real microphone hardware, production identity/infrastructure, sustained load, or the full unrelated end-to-end suite. No dependency or lockfile changes were made. The host Viva frontend build remains unverified because Next.js is not installed there; its production code was unchanged.

### Follow-up: capture recovery after synchronous failures

Rendered regressions exposed one remaining lifecycle edge: React could batch `speaking` → `listening` or `thinking` → `listening` into unchanged displayed state after recognition had stopped. Capture therefore remained inactive despite the listening label.

`VirtualInterview.tsx` now explicitly advances capture reconciliation after playback releases ownership or a send fails. A failed send also preserves interim speech as the next capture's draft prefix, so restarting capture cannot discard the unsent answer.

Before the fix, three targeted cases failed: synchronous AudioContext construction, synchronous buffer creation, and capture restart after failed send. Afterward, the complete interview browser suite passed **12 tests**; TypeScript and the playground production build also passed. The existing Vite chunk-size warning remains.

A separate managed-browser smoke confirmed recognizer starts advanced from `1` to `2` after audio construction failure and to `3` after send failure. The unsent phrase remained visible, and subsequent speech transmitted exactly `I inspect the approved evidence. Then I record the measured clearance.`. Screenshot captured; the tab and temporary frontend server were closed. No real microphone or provider was used.

## Final publication verification

Integrated remote `main` through `371592c` before preparing this publication. Preserved its JETRO styling, gesture-time audio unlock, bounded recognition recovery, and sanitized voice-error notices while retaining all audited persistence, credential, and ownership safeguards.

All ten ranked findings are closed as code defects. Findings 2, 5, and 6 also received an independent release review with no evidence-backed blockers under the documented one-API-plus-worker deployment model. Credential rotation remains an operator action, not something a code change can establish.

| Final gate | Observed result |
| --- | --- |
| Full knowledge-engine suite, excluding paid `test_gemini_live.py` | 475 passed |
| Full Viva backend, without review exclusions | 129 passed |
| Playground unit tests | 33 passed |
| Viva frontend unit tests | 14 passed |
| Changed browser suites: interview and course playback | 21 passed |
| Playground TypeScript and production build | Passed |
| Viva frontend container production build | Passed during the verified fresh Compose rebuild; its source is unchanged by this publication |

The final passing browser gate includes 20 interview cases and the course playback regression. An initial run exposed two test-fixture problems: the recording test used a generic URL rather than the same-origin media endpoint whose tokens intentionally rotate, and the new-session test tried to click Retake after CourseHub had already returned to format selection. Correcting those fixtures preserved the production contracts; the full changed-browser gate then passed without skips.

The final outside-pytest ASGI WebSocket/API probe again accepted `I check the approved evidence first.`, lost examiner delivery, and retrieved that exact persisted answer with assessment status `unavailable`. A separate managed Chromium smoke confirmed capture stayed closed during examiner audio and keyboard submission still worked after a `voice_error` notice. Temporary smoke resources were closed.

The user-approved fresh Compose reset included the previously retained `takumi-worker` profile, removed and recreated all four project volumes, rebuilt the stack, and verified both pages on `8080` and `8081`. Both APIs were healthy; fresh learner progress was zero and the new Viva database contained `pending_question_id`. This reset was a separate explicitly approved operation after the repair-only verification above. Existing versioned databases elsewhere still require the coordinated migration described in Rollout and remaining limits.

Publication scope includes code, tests, migration, README updates, and this audit. The user explicitly excluded the eight untracked machine-local `.omp/plans` notes. The preservation stash remains local. No dependency or lockfile changes are included.

Not established by these gates: paid-provider compatibility, real microphone behavior, production identity integration, sustained load, the full unrelated end-to-end suite, or revocation of previously exposed credentials. Existing deprecation and Vite chunk-size warnings remain.
