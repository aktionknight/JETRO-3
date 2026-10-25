import hashlib
import json
from datetime import UTC

from sqlalchemy import select

from app.core.errors import AppError
from app.database import new_id, utcnow
from app.learning.interview_script import build_script
from app.learning.models import Enrollment, ManualCompletion
from app.learning.replay import replay
from app.models.entities import MediaAsset, Release, Task


def course_video(session):
    return session.scalar(
        select(MediaAsset)
        .where(MediaAsset.task_id == "task_001")
        .order_by(MediaAsset.created_at.desc(), MediaAsset.id.desc())
    )


def video_settings(session, settings):
    video = course_video(session) if settings.enable_media_streaming else None
    if video is None:
        return settings
    return settings.model_copy(
        update={
            "course_video_url": f"/api/v1/media/{video.id}/stream",
            "course_video_duration_seconds": video.duration_ms / 1000,
        }
    )


def active_release(session):
    task = session.get(Task, "task_001")
    release = (
        session.get(Release, task.active_release_id) if task and task.active_release_id else None
    )
    return task, release


def course(session, settings):
    task, release = active_release(session)
    published = release.task_snapshot if release else {}
    return {
        "id": "ball-bearing",
        "title": published.get("title", task.title if task else "Shaft and Bearing Inspection"),
        "description": published.get(
            "description",
            "Review the approved observations and decisions for this course."
            if release
            else "Course content awaits a reviewed release.",
        ),
        "equipment": published.get("equipment", ""),
        "source_label": published.get("source_label", ""),
        "release_id": release.id if release else None,
        "release_version": release.version if release else None,
        "release_content_hash": release.content_hash if release else None,
        "approved_at": release.approved_at.replace(tzinfo=UTC).isoformat() if release else None,
        "approved_by": release.approved_by if release else None,
        "approval_note": release.approval_note if release else None,
        "video_url": settings.course_video_url,
        "video_duration_seconds": settings.course_video_duration_seconds,
        "video_configured": bool(settings.course_video_url.strip()),
        "knowledge_units": release.knowledge_snapshot if release else [],
        "evidence_items": release.evidence_snapshot if release else [],
    }


def enrollment(session, user, settings):
    _, release = active_release(session)
    release_id = release.id if release else ""
    context = hashlib.sha256(
        f"{release_id}|{release.content_hash if release else ''}|"
        f"{release.approved_at.isoformat() if release else ''}|"
        f"{settings.course_video_url}|{settings.course_video_duration_seconds}".encode()
    ).hexdigest()
    row = session.get(Enrollment, user.id)
    if row is None:
        row = Enrollment(
            user_id=user.id,
            context=context,
            release_id=release_id,
            watched_seconds=0,
            position_seconds=0,
        )
        session.add(row)
    elif row.context != context:
        row.context, row.release_id = context, release_id
        row.watched_seconds = row.position_seconds = 0
        row.playback_id = row.heartbeat_at = row.playground = row.interview = None
    session.flush()
    row.manual_completion = {
        item.stage: item.completed_at.replace(tzinfo=UTC).isoformat()
        for item in session.scalars(
            select(ManualCompletion).where(
                ManualCompletion.user_id == row.user_id, ManualCompletion.context == row.context
            )
        )
    }
    return row


def progress(row, settings):
    completed = bool(row.release_id and settings.course_video_url.strip()) and (
        row.watched_seconds
        >= settings.course_video_duration_seconds
        - min(1, settings.course_video_duration_seconds * 0.02)
    )
    completed = completed or bool(row.release_id and row.manual_completion.get("course"))
    interview = None
    if row.interview:
        source = row.interview
        index = len(source["answers"])
        question = source["bank"][index] if index < len(source["bank"]) else None
        interview = {
            "id": source["id"],
            "status": source["status"],
            "revision": source["revision"],
            "source_release_id": source.get("source_release_id", source["release_id"]),
            "source_content_hash": source.get("source_content_hash"),
            "script_hash": source["bundle_hash"],
            "generation_method": source.get("generation_method", "authored_fixture"),
            "current_question": {
                "id": question["id"],
                "prompt": question["prompt"],
                "source_context": question.get("source_context", ""),
                "options": [{"id": o["id"], "label": o["label"]} for o in question["options"]],
            }
            if question
            else None,
            "answers": source["answers"],
            "result": source.get("result"),
            "transcript": source.get("transcript", []),
            "duration_seconds": source.get("duration_seconds"),
        }
    return {
        "course_id": "ball-bearing",
        "release_id": row.release_id or None,
        "watched_seconds": row.watched_seconds,
        "course_completed": completed,
        "playground_unlocked": completed,
        "interview_unlocked": completed
        and (row.playground is not None or bool(row.manual_completion.get("playground"))),
        "manual_completion": row.manual_completion,
        "playground_result": row.playground,
        "interview": interview,
        "evaluation": final_evaluation(row),
    }


def final_evaluation(row):
    playground = row.playground
    interview = row.interview or {}
    result = interview.get("result") or {}
    scores = list(playground["scores"].values()) if playground else []
    playground_score = sum(scores) / len(scores) if scores else None
    available = result.get("available_points", 0)
    interview_score = (
        100 * result["explanation_points"] / available
        if interview.get("status") == "completed" and available > 0
        else None
    )
    overall = (
        (playground_score + interview_score) / 2
        if (playground_score is not None and interview_score is not None)
        else None
    )
    return {
        "playground_score": round(playground_score, 1) if playground_score is not None else None,
        "interview_score": round(interview_score, 1) if interview_score is not None else None,
        "overall_score": round(overall, 1) if overall is not None else None,
        "weighting": "50% playground average + 50% interview",
        "critical_playground_error": bool(playground and playground["critical_error"]),
    }


def start_playback(row, settings):
    if not settings.course_video_url.strip():
        raise AppError(
            "COURSE_VIDEO_UNAVAILABLE", "The course recording has not been configured.", 409
        )
    if not row.release_id:
        raise AppError(
            "COURSE_RELEASE_UNAVAILABLE", "Publish a reviewed course release before training.", 409
        )
    if not row.playback_id:
        row.playback_id = new_id("watch")
    row.heartbeat_at = utcnow()
    return {
        "session_id": row.playback_id,
        "position_seconds": row.position_seconds,
        "progress": progress(row, settings),
    }


def heartbeat(row, playback_id, position, settings):
    if not row.playback_id or playback_id != row.playback_id:
        raise AppError("NOT_FOUND", "Playback session not found.", 404)
    now = utcnow()
    elapsed = max(0, (now - row.heartbeat_at.replace(tzinfo=UTC)).total_seconds())
    delta = position - row.position_seconds
    if (
        position > settings.course_video_duration_seconds + 0.25
        or delta > min(elapsed, 15) * 1.15 + 1
    ):
        raise AppError(
            "INVALID_PLAYBACK_PROGRESS", "Watch the course in sequence before continuing.", 409
        )
    row.watched_seconds = min(
        min(settings.course_video_duration_seconds, max(position, row.position_seconds)),
        row.watched_seconds + min(elapsed, 15),
    )
    row.position_seconds = max(position, row.position_seconds)
    row.heartbeat_at = now
    return progress(row, settings)


def submit_playground(row, req, settings):
    if not progress(row, settings)["playground_unlocked"]:
        raise AppError(
            "COURSE_INCOMPLETE", "Watch the current course before entering the playground.", 409
        )
    fingerprint = hashlib.sha256(req.model_dump_json().encode()).hexdigest()
    if row.playground and row.playground.get("fingerprint") == fingerprint:
        return progress(row, settings)
    result = replay(req.actions, req.scenario)
    row.playground = {
        "id": new_id("attempt"),
        "scenario": req.scenario,
        "mode": req.mode,
        "release_id": row.release_id,
        "fingerprint": fingerprint,
        "actions": req.actions,
        **result,
    }
    row.interview = None
    return progress(row, settings)


def start_interview(session, row, settings):
    if not progress(row, settings)["interview_unlocked"]:
        raise AppError(
            "PLAYGROUND_INCOMPLETE", "Complete the playground before starting the interview.", 409
        )
    if row.interview:
        return progress(row, settings)
    release = session.get(Release, row.release_id)
    _, active = active_release(session)
    if not release or not active or active.id != release.id:
        raise AppError("COURSE_RELEASE_UNAVAILABLE", "Reload the active reviewed course.", 409)
    script = build_script(release)
    row.interview = {
        "id": new_id("interview"),
        "attempt_id": row.playground["id"] if row.playground else None,
        "release_id": row.release_id,
        **script,
        "status": "in_progress",
        "revision": 0,
        "answers": [],
        "receipts": {},
    }
    return progress(row, settings)


def answer(row, req, settings):
    source = row.interview
    if not source or source["id"] != req.session_id:
        raise AppError("NOT_FOUND", "Interview session not found.", 404)
    key = hashlib.sha256(req.model_dump_json().encode()).hexdigest()
    if key in source["receipts"]:
        return source["receipts"][key]
    if req.expected_revision != source["revision"]:
        raise AppError("REVISION_CONFLICT", "Reload the current interview question.", 409)
    if source["status"] == "completed":
        raise AppError("INTERVIEW_COMPLETED", "This interview is complete.", 409)
    question = source["bank"][len(source["answers"])]
    option = next((o for o in question["options"] if o["id"] == req.option_id), None)
    if question["id"] != req.question_id or option is None:
        raise AppError(
            "INVALID_ANSWER", "Answer the current question using one of its options.", 422
        )
    source = json.loads(json.dumps(source))
    source["answers"].append(
        {
            "question_id": req.question_id,
            "question_prompt": question["prompt"],
            "source_context": question.get("source_context", ""),
            "selected_answer": option["label"],
            "supported_answer": next(
                (item["label"] for item in question["options"] if item["points"] > 0), ""
            ),
            "option_id": req.option_id,
            "reflection": req.reflection,
            "feedback": option["feedback"],
            "points": option["points"],
            "evidence_ids": option["evidence_ids"],
            "recognizes_limits": option["recognizes_limits"],
        }
    )
    source["revision"] += 1
    if len(source["answers"]) == len(source["bank"]):
        source["status"] = "completed"
        source["result"] = {
            "explanation_points": sum(a["points"] for a in source["answers"]),
            "available_points": 2 * len(source["bank"]),
            "recognition_of_limits": sum(a["recognizes_limits"] for a in source["answers"]),
            "critical_playground_error": row.playground["critical_error"]
            if row.playground
            else False,
            "playground_assessed": row.playground is not None,
            "notice": (
                "Scripted illustrative assessment. Reflections are ungraded. "
                "Supervised practical assessment is still needed."
            )
            + (
                " Playground manually completed; no simulation score recorded."
                if not row.playground
                else ""
            ),
        }
    row.interview = source
    response = progress(row, settings)
    source["receipts"][key] = json.loads(json.dumps(response))
    row.interview = source
    return response


def complete(session, row, stage, settings):
    if not row.release_id:
        raise AppError(
            "COURSE_RELEASE_UNAVAILABLE", "Publish a reviewed course release before training.", 409
        )
    if stage == "playground" and not progress(row, settings)["course_completed"]:
        raise AppError(
            "COURSE_INCOMPLETE", "Complete the course section before the playground.", 409
        )
    if stage == "interview" and not progress(row, settings)["interview_unlocked"]:
        raise AppError(
            "PLAYGROUND_INCOMPLETE", "Complete the playground before the interview.", 409
        )
    if stage not in row.manual_completion:
        now = utcnow()
        session.add(
            ManualCompletion(
                user_id=row.user_id, context=row.context, stage=stage, completed_at=now
            )
        )
        row.manual_completion = {**row.manual_completion, stage: now.isoformat()}
        session.flush()
    return progress(row, settings)


def reset_progress(session, row, settings):
    row.watched_seconds = 0
    row.position_seconds = 0
    row.playback_id = None
    row.heartbeat_at = None
    row.playground = None
    row.interview = None
    manuals = session.scalars(
        select(ManualCompletion).where(
            ManualCompletion.user_id == row.user_id,
            ManualCompletion.context == row.context,
        )
    ).all()
    for item in manuals:
        session.delete(item)
    row.manual_completion = {}
    session.flush()
    return progress(row, settings)
