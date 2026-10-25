"""Exercise V3 HTTP lifecycle with a caller-supplied expert answer."""

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import httpx


def receipt_path(base_url, task_id, request_id):
    # Keep only replay metadata locally; never persist expert answers or bearer tokens.
    root = Path(tempfile.gettempdir()) / f"takumi-v3-flow-{os.getuid()}"
    root.mkdir(mode=0o700, exist_ok=True)
    key = hashlib.sha256(
        json.dumps([base_url.rstrip("/"), task_id, request_id]).encode()
    ).hexdigest()
    return root / f"{key}.json"


def run_flow(
    client,
    *,
    task_id,
    request_id,
    mode="live",
    generate_only=False,
    clarification_id=None,
    answer=None,
    question=None,
    receipt_file=None,
    emit=print,
):
    if not generate_only and (not answer or not question):
        raise ValueError("--answer and --question are required unless --generate-only is set.")
    count = 0

    def step(name):
        nonlocal count
        count += 1
        emit(f"OK {count} {name}")

    def request(method, path, body=None, headers=None, status=200):
        response = client.request(method, f"/api/v1{path}", json=body, headers=headers)
        if response.status_code != status:
            raise RuntimeError(f"{path}: HTTP {response.status_code}: {response.text}")
        return response.json()

    def login(role):
        result = request("POST", "/auth/demo-login", {"demo_user": role})
        return {"Authorization": f"Bearer {result['access_token']}"}

    expert = login("expert")
    draft = request("GET", f"/tasks/{task_id}/draft", headers=expert)
    path = (
        Path(receipt_file)
        if receipt_file is not None
        else receipt_path(str(client.base_url), task_id, request_id)
    )
    identity = dict(task_id=task_id, request_id=request_id, mode=mode)
    if path.exists():
        saved = json.loads(path.read_text())
        if saved["identity"] != identity:
            raise ValueError("Saved generation receipt does not match task, request ID and mode.")
        payload = saved["request"]
    else:
        payload = dict(
            draft_id=draft["draft_id"],
            expected_revision=draft["revision"],
            mode=mode,
            request_id=request_id,
        )
        # Save before calling so a lost HTTP response can replay the exact original request.
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "x", opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
            json.dump(dict(identity=identity, request=payload), stream)
    generation = request("POST", f"/tasks/{task_id}/clarifications/generate", payload, expert)
    if (
        generation["draft_id"] != payload["draft_id"]
        or generation["revision"] != payload["expected_revision"]
    ):
        raise RuntimeError("Generation receipt changed its original draft or revision.")
    questions = generation["questions"]
    if len(questions) > 3:
        raise RuntimeError("Generation returned more than three questions.")
    units = {u["id"]: u for u in draft["knowledge_units"]}
    metadata = {
        **{key: value for key, value in generation.items() if key != "questions"},
        "request_id": request_id,
    }
    emit(json.dumps(metadata, ensure_ascii=False))
    for item in questions:
        unit = units.get(item["knowledge_unit_id"])
        if (
            unit is None
            or (unit["evidence_ids"] and not item["evidence_ids"])
            or not set(item["evidence_ids"]) <= set(unit["evidence_ids"])
        ):
            raise RuntimeError("Generated question references unknown unit or evidence.")
        sources = []
        for eid in item["evidence_ids"]:
            source = request("GET", f"/evidence/{eid}", headers=expert)
            if source.get("task_id", task_id) != task_id:
                raise RuntimeError("Generated question evidence belongs to another task.")
            sources.append(
                {k: source.get(k) for k in ("id", "type", "content", "start_ms", "end_ms")}
            )
        emit(
            json.dumps(
                {**item, "action": unit["action"], "evidence_items": sources}, ensure_ascii=False
            )
        )
    step("generate-clarifications")
    if not questions:
        raise RuntimeError("No new clarification requires an expert answer.")
    if generate_only:
        return generation
    if clarification_id is None:
        if len(questions) != 1:
            raise ValueError(
                "--clarification-id is required when generation contains more than one question."
            )
        clarification_id = questions[0]["id"]
    selected = next((q for q in questions if q["id"] == clarification_id), None)
    if selected is None:
        raise ValueError("--clarification-id must name a question from this generation.")
    current = next((q for q in draft["clarifications"] if q["id"] == clarification_id), None)
    if current is None:
        draft = request("GET", f"/tasks/{task_id}/draft", headers=expert)
        current = next(q for q in draft["clarifications"] if q["id"] == clarification_id)
    field = {
        "missing_cue": "cue",
        "missing_exception": "exception",
        "beginner_mistake": "exception",
        "changed_condition": "decision",
    }.get(selected["gap_type"], "interpretation")
    if current["status"] == "answered":
        # A retry after answering must not create another expert response or overwrite knowledge.
        unit = next(u for u in draft["knowledge_units"] if u["id"] == selected["knowledge_unit_id"])
        matches = []
        for eid in unit["field_evidence"].get(field, []):
            ev = request("GET", f"/evidence/{eid}", headers=expert)
            if (
                ev["type"] == "expert_response"
                and ev["content"] == answer
                and ev["author_id"] == "expert_001"
            ):
                matches.append(eid)
        if not matches:
            raise RuntimeError(
                "Existing answer does not match caller-supplied attributed expert evidence."
            )
        evidence_id = matches[-1]
    else:
        if current["status"] != "pending":
            raise RuntimeError("Selected clarification is not pending or answered.")
        result = request(
            "POST",
            f"/clarifications/{clarification_id}/respond",
            {
                "expected_draft_revision": draft["revision"],
                "disposition": "answered",
                "answer": answer,
                "knowledge_update": {
                    "knowledge_unit_id": selected["knowledge_unit_id"],
                    "fields": {field: answer},
                },
            },
            expert,
        )
        evidence_id = result["evidence_id"]
    ev = request("GET", f"/evidence/{evidence_id}", headers=expert)
    if (
        ev["type"] != "expert_response"
        or ev["author_id"] != "expert_001"
        or ev["content"] != answer
    ):
        raise RuntimeError("Expert response attribution or content mismatch.")
    step("verify-expert-evidence")
    draft = request("GET", f"/tasks/{task_id}/draft", headers=expert)
    unit = next(u for u in draft["knowledge_units"] if u["id"] == selected["knowledge_unit_id"])
    if evidence_id not in unit["field_evidence"].get(field, []) or unit[field] != answer:
        raise RuntimeError("Answer was not attached to the intended knowledge field.")
    for item in draft["clarifications"]:
        if item["status"] == "pending":
            request(
                "POST",
                f"/clarifications/{item['id']}/respond",
                {
                    "expected_draft_revision": draft["revision"],
                    "disposition": "deferred",
                    "answer": "Deferred during V3 acceptance run.",
                },
                expert,
            )
    step("defer-other-pending")
    evaluation = request(
        "POST",
        f"/studio/tasks/{task_id}/evaluate",
        {"expected_revision": draft["revision"]},
        expert,
    )
    if not evaluation["passed"]:
        raise RuntimeError(f"Review gate failed: {json.dumps(evaluation)}")
    step("evaluate")
    reviewer = login("reviewer")
    published = request(
        "POST",
        f"/tasks/{task_id}/publish",
        {
            "expected_revision": draft["revision"],
            "approval_note": "V3 acceptance reviewer approval.",
        },
        reviewer,
        status=201,
    )
    release_id = published["release_id"]
    step("reviewer-publish")
    release = request("GET", f"/tasks/{task_id}/release", headers=reviewer)
    pack = request("GET", f"/releases/{release_id}/training-pack", headers=reviewer)
    if release["id"] != release_id or pack["release_id"] != release_id:
        raise RuntimeError("Release and training pack IDs differ.")
    if evidence_id not in {item["id"] for item in pack["evidence_items"]}:
        raise RuntimeError("Training pack omitted new expert evidence.")
    request("PATCH", f"/releases/{release_id}", {}, reviewer, status=405)
    step("immutable-release-training-pack")
    apprentice = login("apprentice")
    result = request("POST", f"/tasks/{task_id}/ask", {"question": question}, apprentice)
    if (
        result["status"] != "answered"
        or result["release_id"] != release_id
        or not any(
            evidence_id in claim["evidence_ids"]
            and claim["knowledge_unit_id"] == selected["knowledge_unit_id"]
            for claim in result["claims"]
        )
    ):
        raise RuntimeError(f"Supported answer lacks new approved evidence: {json.dumps(result)}")
    step("ask-new-expert-explanation")
    unsupported = request(
        "POST",
        f"/tasks/{task_id}/ask",
        {
            "question": "What is the torque specification for the housing bolts?",
        },
        apprentice,
    )
    if (
        unsupported["status"] != "insufficient_evidence"
        or unsupported["claims"]
        or unsupported["release_id"] != release_id
    ):
        raise RuntimeError("Unsupported torque question did not abstain against the same release.")
    step("unsupported-torque-abstention")
    return dict(generation=generation, release_id=release_id, evidence_id=evidence_id)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8012")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--request-id", default=None)
    parser.add_argument("--mode", choices=["live", "deterministic"], default="live")
    parser.add_argument("--generate-only", action="store_true")
    parser.add_argument("--clarification-id")
    parser.add_argument("--answer")
    parser.add_argument("--question")
    args = parser.parse_args(argv)
    if not args.generate_only and (not args.answer or not args.question):
        parser.error("--answer and --question are required unless --generate-only is set.")
    try:
        with httpx.Client(base_url=args.base_url, timeout=120) as client:
            run_flow(
                client,
                task_id=args.task_id,
                request_id=args.request_id or str(uuid4()),
                mode=args.mode,
                generate_only=args.generate_only,
                clarification_id=args.clarification_id,
                answer=args.answer,
                question=args.question,
            )
    except (RuntimeError, ValueError, OSError, httpx.HTTPError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
