import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402


def run_flow(client, record=None) -> None:
    last = None

    def request(method, path, body=None, headers=None, status=200):
        nonlocal last
        response = client.request(method, f"/api/v1{path}", json=body, headers=headers)
        assert response.status_code == status, (path, response.status_code, response.text)
        last = (method, f"/api/v1{path}", body, response.status_code, response.json())
        return response.json()

    def step(name):
        if record:
            record(name, *last)

    def login(role):
        data = request("POST", "/auth/demo-login", {"demo_user": role})
        return {"Authorization": f"Bearer {data['access_token']}"}

    reset = {"scope": "full_demo", "confirm": "RESET_DEMO_DATA", "state": "fresh"}
    reviewer = login("reviewer")
    request("POST", "/demo/reset", reset, reviewer)
    step("reset-fresh")
    expert = login("expert")
    draft = request("GET", "/tasks/task_001/draft", headers=expert)
    assert draft["knowledge_units"][0]["interpretation"] is None
    step("read-draft")
    clarification = next(item for item in draft["clarifications"] if item["id"] == "clar_001")
    assert clarification["status"] == "pending"
    step("read-clarification")
    # The fixture remains the single source of illustrative mechanical content.
    import json

    from app.ai.fixture_provider import FIXTURE_PATH

    example = json.loads(FIXTURE_PATH.read_text())["example_expert_responses"][0]
    answer = request(
        "POST",
        "/clarifications/clar_001/respond",
        {
            "expected_draft_revision": draft["revision"],
            "disposition": "answered",
            "answer": example["answer"],
            "knowledge_update": example["knowledge_update"],
        },
        expert,
    )
    evidence_id = answer["evidence_id"]
    assert evidence_id
    step("answer-clarification")
    evidence = request("GET", f"/evidence/{evidence_id}", headers=expert)
    assert evidence["content"] == example["answer"]
    step("read-evidence")
    draft = request("GET", "/tasks/task_001/draft", headers=expert)
    assert evidence_id in draft["knowledge_units"][0]["field_evidence"]["interpretation"]
    step("read-updated-draft")
    # Explicitly defer unanswered prompts and evaluate the exact reviewed revision.
    # This keeps the legacy reviewer route governed by the Studio publication policy.
    for gap in draft["clarifications"]:
        if gap["status"] == "pending":
            request(
                "POST",
                f"/clarifications/{gap['id']}/respond",
                {
                    "expected_draft_revision": draft["revision"],
                    "disposition": "deferred",
                    "answer": "Demo reviewer deferred this unanswered prompt; no claim added.",
                },
                expert,
            )
    evaluation = request(
        "POST",
        "/studio/tasks/task_001/evaluate",
        {"expected_revision": draft["revision"]},
        expert,
    )
    assert evaluation["passed"], evaluation
    published = request(
        "POST",
        "/tasks/task_001/publish",
        {"expected_revision": draft["revision"], "approval_note": "Demo flow reviewer approval."},
        reviewer,
        status=201,
    )
    release_id = published["release_id"]
    step("publish")
    release = request("GET", "/tasks/task_001/release", headers=reviewer)
    assert release["id"] == release_id
    step("read-release")
    pack = request("GET", f"/releases/{release_id}/training-pack", headers=reviewer)
    assert pack["release_id"] == release_id
    assert evidence_id in {item["id"] for item in pack["evidence_items"]}
    step("training-pack")
    apprentice = login("apprentice")
    answer = request(
        "POST",
        "/tasks/task_001/ask",
        {"question": "Why do you inspect the bearing housing?"},
        apprentice,
    )
    assert answer["status"] == "answered"
    step("ask-grounded")
    assert any(
        claim["knowledge_unit_id"] == "ku_001" and evidence_id in claim["evidence_ids"]
        for claim in answer["claims"]
    )
    step("check-new-citation")
    answer = request(
        "POST",
        "/tasks/task_001/ask",
        {"question": "What is the torque specification for the housing bolts?"},
        apprentice,
    )
    step("ask-unsupported-spec")
    assert answer["status"] == "insufficient_evidence" and not answer["claims"]
    step("check-abstention")
    request("PATCH", f"/releases/{release_id}", {}, reviewer, status=405)
    step("reject-release-edit")
    assert last[4]["error"]["code"] == "METHOD_NOT_ALLOWED"
    step("check-immutable")
    previous_revision = draft["revision"]
    request("POST", "/demo/reset", reset, reviewer)
    draft = request("GET", "/tasks/task_001/draft", headers=expert)
    assert draft["revision"] == previous_revision + 1
    assert draft["knowledge_units"][0]["interpretation"] is None
    step("reset-repeatable")


def main():
    parser = argparse.ArgumentParser(description="Exercise the 16-step knowledge lifecycle.")
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()
    count = 0

    def record(name, *_):
        nonlocal count
        count += 1
        print(f"OK {count} {name}")

    with httpx.Client(base_url=args.base_url, timeout=30) as client:
        run_flow(client, record)
    assert count == 16


if __name__ == "__main__":
    main()
