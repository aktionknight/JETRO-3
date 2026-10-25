"""Live AI conversational interview using Gemini AI + Gemini TTS.

Provides a live, interactive 2-minute technical interview for apprentices:
- Grounded in active release knowledge units, video evidence, and studio drafts.
- Uses Gemini for expert reasoning and conversational turn-taking.
- Uses Gemini TTS (gemini-2.5-flash-preview-tts) for spoken examiner voice.
- Client receives streaming PCM 24kHz audio and live transcript.
- 2-minute hard timer with structured wrap-up and automatic completion credit.
"""

import asyncio
import json
import logging
import time
from copy import deepcopy

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.core.security import DEMO_USERS, CurrentUser, _verify
from app.learning import live_evaluation, service
from app.models.entities import Draft, Task

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/learning", tags=["learning"])

INTERVIEW_DURATION_SECONDS = 120  # 2 minutes max
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
TTS_MODEL = "gemini-2.5-flash-preview-tts"


def _build_system_prompt(release_context: dict, draft_units: list[dict], video_info: str) -> str:
    """Build the system instruction for a conversational technical interview."""
    title = release_context.get("title", "Shaft and Bearing Inspection")
    description = release_context.get("description", "")
    equipment = release_context.get("equipment", "Precision Shaft & Journal Bearing Assembly")
    units = release_context.get("knowledge_units", [])
    evidence = release_context.get("evidence_items", [])

    knowledge_text = ""
    for unit in units:
        parts = []
        for field in ("action", "cue", "interpretation", "decision", "exception"):
            if unit.get(field):
                parts.append(f"  - {field.upper()}: {unit[field]}")
        if parts:
            knowledge_text += f"\n[{unit.get('id', 'unit')}]:\n" + "\n".join(parts) + "\n"

    draft_section = ""
    if draft_units:
        draft_text = ""
        for unit in draft_units:
            parts = []
            for field in ("action", "cue", "interpretation", "decision", "exception"):
                if unit.get(field):
                    parts.append(f"  - {field.upper()}: {unit[field]}")
            if parts:
                draft_text += f"\n[DRAFT {unit.get('id', 'unit')}]:\n" + "\n".join(parts) + "\n"
        if draft_text:
            draft_section = (
                f"LATEST STUDIO DRAFT UPDATES (Recent expert revisions):\n{draft_text}\n"
            )

    evidence_text = ""
    for item in evidence:
        content = item.get("content") or "Recording segment"
        evidence_text += f"\n  - [{item.get('id', '')}] ({item.get('source_label', '')}): {content}"

    video_desc = (
        evidence_text
        or video_info
        or "Video showing shaft rotation, bearing surface check, and clearance measurement."
    )

    rules = (
        "EXAMINER GUIDELINES:\n"
        "1. Conduct a spoken oral interview with an apprentice technician.\n"
        "2. In your opening turn, briefly welcome them and ask your FIRST technical question.\n"
        "3. Ask about procedures, cues, interpretations, decisions, tolerances,\n"
        "   or failure signs from the published knowledge and video.\n"
        "4. When evaluating the apprentice's response:\n"
        "   - If correct: briefly validate and follow up with a deeper or next question.\n"
        "   - If partially correct or vague: prompt for specifics.\n"
        "   - If incorrect: politely correct them citing the evidence and move to next topic.\n"
        "5. Keep your spoken responses concise (2 to 3 sentences maximum) for natural flow.\n"
        "6. When time is almost up (at ~1:45 or [TIME UP]), provide a concise constructive\n"
        "   assessment of their performance, state competence, and conclude professionally."
    )

    return (
        f'You are an expert technical examiner conducting an oral viva for: "{title}".\n'
        f"Equipment: {equipment}\n\n"
        f"COURSE CONTEXT:\n{description}\n\n"
        f"PUBLISHED APPROVED KNOWLEDGE (Primary source of truth):\n"
        f"{knowledge_text or 'Standard precision bearing inspection and alignment protocols.'}\n\n"
        f"{draft_section}"
        f"VIDEO EVIDENCE FROM EXPERT RECORDING:\n{video_desc}\n\n"
        f"{rules}"
    )


def _authenticate_ws(token: str, signing_key: str) -> CurrentUser | None:
    """Validate a bearer token from the WebSocket query string."""
    try:
        data = _verify(token, signing_key)
        if "scope" in data:
            return None
        for identity in DEMO_USERS.values():
            if (data["sub"], data["role"]) == identity[:2]:
                user = CurrentUser(*identity)
                if user.role == "apprentice":
                    return user
        return None
    except Exception:
        return None


def _get_course_data(sessionmaker, settings) -> tuple[dict, list[dict], str]:
    """Load active release context, studio drafts, and video info."""
    with sessionmaker() as session:
        effective_settings = service.video_settings(session, settings)
        rel_context = service.course(session, effective_settings)
        draft_units = []
        video_info = ""

        task = session.get(Task, "task_001")
        if task:
            draft = session.scalar(select(Draft).where(Draft.task_id == task.id))
            if draft and draft.knowledge_units:
                draft_units = draft.knowledge_units
            video = service.course_video(session)
            if video:
                video_info = (
                    f"Video asset {video.id} ({video.duration_ms // 1000}s) "
                    f"uploaded by expert for task '{task.title}'."
                )
        return rel_context, draft_units, video_info


async def _generate_text(
    client: httpx.AsyncClient,
    api_key: str,
    model_name: str,
    system_prompt: str,
    contents: list[dict],
) -> str:
    """Generate examiner response text via Gemini generateContent."""
    url = f"{GEMINI_BASE_URL}/models/{model_name}:generateContent"
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 300,
        },
    }
    resp = await client.post(url, headers={"x-goog-api-key": api_key}, json=payload, timeout=20.0)
    resp.raise_for_status()
    data = resp.json()
    candidates = data.get("candidates", [])
    if candidates and candidates[0].get("content", {}).get("parts"):
        return candidates[0]["content"]["parts"][0].get("text", "")
    return ""


async def _synthesize_speech(
    client: httpx.AsyncClient,
    api_key: str,
    text: str,
) -> tuple[str, str]:
    """Synthesize speech using Gemini TTS (returns base64 PCM data and mimeType)."""
    clean_text = text.replace("**", "").replace("*", "").replace("#", "").strip()
    if not clean_text:
        return "", ""

    url = f"{GEMINI_BASE_URL}/models/{TTS_MODEL}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": clean_text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Aoede"}}},
        },
    }
    resp = await client.post(url, headers={"x-goog-api-key": api_key}, json=payload, timeout=25.0)
    resp.raise_for_status()
    data = resp.json()
    candidates = data.get("candidates", [])
    if candidates and candidates[0].get("content", {}).get("parts"):
        for part in candidates[0]["content"]["parts"]:
            if "inlineData" in part:
                inline = part["inlineData"]
                return inline.get("data", ""), inline.get(
                    "mimeType", "audio/L16;codec=pcm;rate=24000"
                )
    return "", ""


async def _report_voice_error(ws, exc):
    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
    logger.warning("Gemini TTS unavailable: status=%s exception=%s", status, type(exc).__name__)
    message = (
        "Gemini voice quota or rate limit reached (429). Check the TTS model quota "
        "in Google AI Studio for this API key's project. Text questions still work."
        if status == 429
        else "Gemini could not generate interviewer audio. Check the voice service configuration."
    )
    await ws.send_json({"type": "voice_error", "message": message})


@router.websocket("/interview/live")
async def live_interview_ws(ws: WebSocket):
    """WebSocket endpoint for live AI conversational interview with Gemini TTS."""
    token = ws.query_params.get("token")
    if not token:
        await ws.close(code=4001, reason="Missing authentication token")
        return

    app = ws.app
    signing_key = app.state.signing_key
    settings = app.state.settings

    user = _authenticate_ws(token, signing_key)
    if not user:
        await ws.close(code=4001, reason="Invalid or expired token")
        return

    gemini_key = settings.gemini_api_key
    if not gemini_key:
        await ws.close(code=4003, reason="AI provider not configured")
        return
    api_key = gemini_key.get_secret_value()

    # Load release context, video evidence, and studio draft updates
    rel_context, draft_units, video_info = _get_course_data(app.state.sessionmaker, settings)
    if not rel_context or not rel_context.get("release_id"):
        await ws.close(code=4004, reason="No published course release available")
        return

    with app.state.sessionmaker() as session, session.begin():
        effective_settings = service.video_settings(session, settings)
        enrollment = service.enrollment(session, user, effective_settings)
        unlocked = service.progress(enrollment, effective_settings)["interview_unlocked"]
        starting_context = enrollment.context
        starting_interview = deepcopy(enrollment.interview)
        starting_completions = deepcopy(enrollment.manual_completion)
        playground = deepcopy(enrollment.playground)
    if not unlocked:
        await ws.close(code=4005, reason="Complete the playground before the interview")
        return
    # Apprentice questions and scoring use the approved snapshot, not unreviewed drafts.
    system_prompt = _build_system_prompt(rel_context, [], video_info)
    model_name = settings.gemini_model or "gemini-3.5-flash-lite"

    await ws.accept()
    await ws.send_json({"type": "status", "status": "connecting"})

    start_time = time.monotonic()
    client_closed = asyncio.Event()
    conversation_history: list[dict] = []

    async with httpx.AsyncClient() as http_client:
        try:
            # Generate initial greeting and first question
            init_msg = "[SYSTEM: The apprentice entered the room. Start the interview now.]"
            init_contents = [
                {
                    "role": "user",
                    "parts": [{"text": init_msg}],
                }
            ]
            greeting_text = await _generate_text(
                http_client, api_key, model_name, system_prompt, init_contents
            )
            if not greeting_text:
                greeting_text = (
                    "Welcome to your technical viva on shaft and bearing inspection. "
                    "To begin, how do you inspect a bearing for signs of premature wear?"
                )

            conversation_history.append({"role": "model", "parts": [{"text": greeting_text}]})

            # Synthesize audio with Gemini TTS
            audio_base64 = ""
            mime_type = "audio/L16;codec=pcm;rate=24000"
            try:
                audio_base64, mime_type = await _synthesize_speech(
                    http_client, api_key, greeting_text
                )
            except Exception as tts_err:
                await _report_voice_error(ws, tts_err)

            # Send greeting to client
            await ws.send_json({"type": "status", "status": "connected"})
            await ws.send_json({"type": "transcript", "role": "model", "text": greeting_text})
            if audio_base64:
                await ws.send_json(
                    {
                        "type": "audio",
                        "data": audio_base64,
                        "mimeType": mime_type,
                    }
                )
            await ws.send_json({"type": "status", "status": "listening"})

            # Background timer task
            async def timer_task():
                while not client_closed.is_set():
                    elapsed = time.monotonic() - start_time
                    remaining = max(0, INTERVIEW_DURATION_SECONDS - elapsed)
                    try:
                        await ws.send_json(
                            {
                                "type": "timer",
                                "remaining": round(remaining, 1),
                                "elapsed": round(elapsed, 1),
                            }
                        )
                    except Exception:
                        break

                    if remaining <= 0:
                        client_closed.set()
                        break
                    await asyncio.sleep(1)

            timer_handle = asyncio.create_task(timer_task())

            # Message loop: receive answers from client
            try:
                while not client_closed.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.receive_text(), timeout=0.5)
                    except TimeoutError:
                        continue
                    except WebSocketDisconnect:
                        client_closed.set()
                        break

                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    msg_type = msg.get("type")

                    if msg_type == "end":
                        client_closed.set()
                        break

                    elif msg_type == "user_turn":
                        user_text = (msg.get("text") or "").strip()
                        if not user_text:
                            continue

                        await ws.send_json(
                            {
                                "type": "transcript",
                                "role": "user",
                                "text": user_text,
                            }
                        )
                        await ws.send_json({"type": "status", "status": "thinking"})

                        conversation_history.append(
                            {
                                "role": "user",
                                "parts": [{"text": user_text}],
                            }
                        )

                        elapsed = time.monotonic() - start_time
                        remaining = max(0, INTERVIEW_DURATION_SECONDS - elapsed)

                        turn_prompt = system_prompt
                        if remaining < 20:
                            turn_prompt += (
                                "\n\n[TIME UP: Wrap up interview with final assessment & bye.]"
                            )

                        try:
                            examiner_text = await _generate_text(
                                http_client, api_key, model_name, turn_prompt, conversation_history
                            )
                            conversation_history.append(
                                {
                                    "role": "model",
                                    "parts": [{"text": examiner_text}],
                                }
                            )

                            turn_audio, turn_mime = "", "audio/L16;codec=pcm;rate=24000"
                            try:
                                turn_audio, turn_mime = await _synthesize_speech(
                                    http_client, api_key, examiner_text
                                )
                            except Exception as tts_err:
                                await _report_voice_error(ws, tts_err)

                            await ws.send_json(
                                {
                                    "type": "transcript",
                                    "role": "model",
                                    "text": examiner_text,
                                }
                            )
                            if turn_audio:
                                await ws.send_json(
                                    {
                                        "type": "audio",
                                        "data": turn_audio,
                                        "mimeType": turn_mime,
                                    }
                                )

                            if remaining <= 10:
                                await asyncio.sleep(4)
                                client_closed.set()
                                break
                            else:
                                await ws.send_json({"type": "status", "status": "listening"})

                        except Exception as gen_err:
                            logger.error("Error generating turn: %s", gen_err)
                            await ws.send_json(
                                {
                                    "type": "transcript",
                                    "role": "model",
                                    "text": ("Thank you. Let us continue with the next check."),
                                }
                            )
                            await ws.send_json({"type": "status", "status": "listening"})

            finally:
                timer_handle.cancel()

            # Grade outside a transaction, then save the actual report before announcing completion.
            try:
                await ws.send_json({"type": "status", "status": "evaluating"})
            except Exception:
                pass  # A disconnected apprentice can retrieve the saved report later.
            elapsed = time.monotonic() - start_time
            try:
                report = await live_evaluation.evaluate(
                    http_client,
                    api_key,
                    model_name,
                    rel_context,
                    playground,
                    conversation_history,
                    elapsed,
                )
            except Exception as assessment_error:
                logger.warning("Live assessment unavailable: %s", type(assessment_error).__name__)
                report = live_evaluation.build_record(
                    rel_context,
                    playground,
                    live_evaluation.answer_turns(conversation_history),
                    None,
                    elapsed,
                )
                report["result"]["summary"] = (
                    "The conversation was saved, but scoring was unavailable. Retake the "
                    "interview or ask your instructor to review it. No score was assigned."
                )
            with app.state.sessionmaker() as session, session.begin():
                effective_settings = service.video_settings(session, settings)
                row = service.enrollment(session, user, effective_settings)
                if (
                    row.context != starting_context
                    or row.playground != playground
                    or row.interview != starting_interview
                    or row.manual_completion != starting_completions
                ):
                    raise ValueError(
                        "Learning record changed during the interview; report not saved."
                    )
                row.interview = report
                session.flush()

            # Send final ended status
            elapsed = time.monotonic() - start_time
            try:
                await ws.send_json(
                    {
                        "type": "status",
                        "status": "ended",
                        "duration": report["duration_seconds"],
                    }
                )
            except Exception:
                pass

        except Exception as exc:
            logger.exception("Live interview error: %s", exc)
            try:
                await ws.send_json({"type": "error", "message": f"Interview error: {exc}"})
            except Exception:
                pass
        finally:
            try:
                await ws.close()
            except Exception:
                pass
