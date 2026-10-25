import json

import pytest

from scripts.run_evals import load_dataset, main, run_evaluations, write_report


@pytest.fixture(scope="module")
def report():
    return run_evaluations()


def test_datasets_cover_authored_scope():
    gaps = load_dataset("gaps")
    qa = load_dataset("qa")
    assert len(gaps["cases"]) >= 8
    groups = [c["group"] for c in qa["cases"]]
    assert groups.count("supported") == 10
    assert groups.count("unsupported") == 5
    assert groups.count("ambiguous") == 2
    assert groups.count("isolation") == 2
    assert groups.count("source_injection") == 2
    for dataset in (gaps, qa):
        assert "illustrative" in dataset["source_label"]
        assert all(not case["expert_reviewed"] for case in dataset["cases"])


def test_deterministic_evaluations_and_honest_provenance(report, tmp_path):
    assert report["counts"] == {"pass": 32, "fail": 0, "error": 0}, report
    assert report["supported"] == {"passed": 10, "total": 10, "target": 9}
    assert report["unsupported"] == {"passed": 5, "total": 5, "target": 5}
    assert report["model_id"] is None
    assert report["latency"]["model_latency_ms"] is None
    assert report["latency"]["measured_total_ms"] > 0
    assert all(c["model_latency_ms"] is None for c in report["cases"])
    assert report["expert_acceptance"] is None
    assert report["expert_rejection"] is None
    assert report["expert_reviewed_semantic_correctness"] is None
    assert report["limitations"]
    assert report["gap_duplicate_count"] == 0
    assert report["citation_failures"] == []
    output = tmp_path / "observed.json"
    write_report(report, output)
    assert json.loads(output.read_text()) == report
    assert "Expert metrics: unmeasured" in output.with_suffix(".md").read_text()


@pytest.mark.parametrize(
    "case_id",
    ["invalid-citation", "changed-number", "lost-negation", "wrong-task", "wrong-release"],
)
def test_counterexamples_fail_closed(report, case_id):
    case = next(c for c in report["cases"] if c["id"] == case_id)
    assert case["passed"], case
    assert case["status"] == "insufficient_evidence"
    assert case["claims"] == []
    assert case["execution"] == "offline_adversarial_replay"


def test_runner_never_constructs_live_client_in_default_mode(monkeypatch):
    from app.ai import knowledge

    def forbidden(*args, **kwargs):
        pytest.fail("Deterministic evaluations attempted a live model call")

    monkeypatch.setattr(knowledge, "KnowledgeAI", forbidden)
    result = run_evaluations()
    assert result["counts"]["error"] == 0


def test_cli_errors_distinguish_behavior_and_runner_errors(monkeypatch, tmp_path):
    import scripts.run_evals as runner

    def failed(mode):
        raise RuntimeError("not disclosed")

    monkeypatch.setattr(runner, "run_evaluations", failed)
    assert main(["--output", str(tmp_path / "error.json")]) == 2
