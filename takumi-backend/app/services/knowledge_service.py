from copy import deepcopy

from sqlalchemy import select, update

from app.core.errors import AppError
from app.database import new_id, utcnow
from app.models.entities import Clarification, Draft, DraftRevision, Evidence, MediaAsset
from app.models.schemas import ClarificationOut, DraftOut, DraftPatchOut
from app.services import evidence_service


def get_draft(session, task_id):
    draft = session.scalar(select(Draft).where(Draft.task_id == task_id))
    if draft is None:
        raise AppError("DRAFT_NOT_FOUND", "Draft not found.", 404)
    return draft


def read_draft(session, task_id):
    draft = get_draft(session, task_id)
    clarifications = session.scalars(
        select(Clarification).where(Clarification.draft_id == draft.id).order_by(Clarification.id)
    ).all()
    return DraftOut(
        draft_id=draft.id,
        task_id=draft.task_id,
        revision=draft.revision,
        status=draft.status,
        knowledge_units=draft.knowledge_units,
        clarifications=[
            ClarificationOut.model_validate(row, from_attributes=True) for row in clarifications
        ],
    )


def apply_field_updates(unit, fields, evidence_id):
    result = deepcopy(unit)
    for field, value in fields.items():
        if result.get(field) == value and (evidence_id is None or value is None):
            continue
        result[field] = value
        support = result.setdefault("field_evidence", {})
        if value is None:
            support.pop(field, None)
        elif evidence_id is not None:
            support[field] = list(dict.fromkeys([*support.get(field, []), evidence_id]))
        if evidence_id is not None:
            result["evidence_ids"] = list(
                dict.fromkeys([*result.get("evidence_ids", []), evidence_id])
            )
        result["review_status"] = "draft"
    return result


def check_revision(session, draft, expected_revision):
    actual = session.scalar(select(Draft.revision).where(Draft.id == draft.id))
    if actual != expected_revision:
        raise AppError(
            "REVISION_CONFLICT",
            "Draft revision does not match.",
            409,
            {"expected": expected_revision, "actual": actual},
        )


def bump_draft(session, draft, expected_revision, new_units, actor_id, reason):
    result = session.execute(
        update(Draft)
        .where(Draft.id == draft.id, Draft.revision == expected_revision)
        .values(revision=expected_revision + 1, knowledge_units=new_units, updated_at=utcnow())
    )
    if result.rowcount == 0:
        check_revision(session, draft, expected_revision)
    session.add(
        DraftRevision(
            draft_id=draft.id,
            revision=expected_revision + 1,
            knowledge_units=new_units,
            actor_id=actor_id,
            reason=reason,
        )
    )
    session.flush()


def patch_draft(session, task_id, req, user):
    draft = get_draft(session, task_id)
    check_revision(session, draft, req.expected_revision)
    ids = [change.knowledge_unit_id for change in req.changes]
    if len(ids) != len(set(ids)):
        raise AppError("VALIDATION_ERROR", "Duplicate knowledge unit id in changes.", 422)
    units = {unit["id"]: deepcopy(unit) for unit in draft.knowledge_units}
    for unit_id in ids:
        if unit_id not in units:
            raise AppError(
                "KNOWLEDGE_UNIT_NOT_FOUND",
                "Knowledge unit not found.",
                422,
                {"knowledge_unit_id": unit_id},
            )
    evidence_ids = []
    for change in req.changes:
        unit = units[change.knowledge_unit_id]
        if change.operation == "reject":
            del units[change.knowledge_unit_id]
            session.execute(
                update(Clarification)
                .where(
                    Clarification.task_id == task_id,
                    Clarification.knowledge_unit_id == change.knowledge_unit_id,
                    Clarification.status == "pending",
                )
                .values(
                    status="premise_rejected",
                    answer="Knowledge unit rejected during review.",
                    answered_by=user.id,
                    answered_at=utcnow(),
                )
            )
            continue
        if change.operation == "approve":
            unsupported = [
                field
                for field in ("action", "cue", "interpretation", "decision", "exception")
                if (field == "action" and not unit.get(field))
                or (unit.get(field) is not None and not unit.get("field_evidence", {}).get(field))
            ]
            if unsupported:
                raise AppError(
                    "UNIT_NOT_APPROVABLE",
                    "Knowledge unit has unsupported fields.",
                    409,
                    {"knowledge_unit_id": change.knowledge_unit_id, "fields": unsupported},
                )
            unit["review_status"] = "approved"
            continue
        if change.interval is not None:
            interval = change.interval
            old = session.get(Evidence, interval.evidence_id)
            if (
                old is None
                or old.task_id != task_id
                or old.type != "video_segment"
                or old.id not in unit.get("evidence_ids", [])
            ):
                raise AppError(
                    "INVALID_EVIDENCE",
                    "Evidence is not a video segment cited by this knowledge unit.",
                    422,
                    {"evidence_id": interval.evidence_id},
                )
            duration = session.get(MediaAsset, old.media_id).duration_ms if old.media_id else None
            problem = evidence_service.interval_problem(
                interval.start_ms, interval.end_ms, duration
            )
            if problem:
                raise AppError(
                    "INVALID_EVIDENCE", problem, 422, {"evidence_id": interval.evidence_id}
                )
            evidence = Evidence(
                id=new_id("ev"),
                task_id=task_id,
                type="video_segment",
                author_id=user.id,
                author_label=user.display_name,
                source_label=old.source_label,
                media_id=old.media_id,
                start_ms=interval.start_ms,
                end_ms=interval.end_ms,
                content=(
                    f"[Interval corrected by {user.display_name} from "
                    f"{old.start_ms}-{old.end_ms} ms] {old.content or ''}"
                ).strip(),
            )
            session.add(evidence)
            evidence_ids.append(evidence.id)
            unit["evidence_ids"] = [
                evidence.id if item == old.id else item for item in unit["evidence_ids"]
            ]
            unit["field_evidence"] = {
                field: [evidence.id if item == old.id else item for item in support]
                for field, support in unit.get("field_evidence", {}).items()
            }
            unit["review_status"] = "draft"
        if change.fields is None:
            continue
        fields = {
            field: value
            for field, value in change.fields.model_dump(exclude_unset=True).items()
            if unit.get(field) != value
        }
        if not fields:
            continue
        evidence = Evidence(
            id=new_id("ev"),
            task_id=task_id,
            type="reviewer_correction",
            author_id=user.id,
            author_label=user.display_name,
            source_label="real",
            content="\n".join(
                f"{field}: {value}" if value is not None else f"{field}: (cleared; marked unknown)"
                for field, value in fields.items()
            ),
        )
        session.add(evidence)
        evidence_ids.append(evidence.id)
        units[change.knowledge_unit_id] = apply_field_updates(unit, fields, evidence.id)
    bump_draft(session, draft, req.expected_revision, list(units.values()), user.id, "correction")
    return DraftPatchOut(
        draft_id=draft.id,
        revision=req.expected_revision + 1,
        status=draft.status,
        updated_knowledge_unit_ids=ids,
        correction_evidence_ids=evidence_ids,
    )
