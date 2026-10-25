"""Versioned prompt registry. Prompt changes ship as a new version string, never in place."""

from dataclasses import dataclass

from app.ai.prompts import (
    gap_elicitation_v1,
    grounded_qa_v1,
    transcript_extraction_v1,
    video_extraction_v1,
)


@dataclass(frozen=True)
class PromptSpec:
    name: str
    version: str
    template: str
    input_schema_version: str
    output_schema_version: str
    provider_model_pattern: str
    introduced_on: str


PROMPTS: dict[str, PromptSpec] = {
    spec.version: spec
    for spec in (
        PromptSpec(
            "video_extraction",
            video_extraction_v1.PROMPT_VERSION,
            video_extraction_v1.INSTRUCTIONS,
            "TaskContext",
            "GeminiVideoResult",
            r"gemini-3[\w.-]*",
            "2026-10-25",
        ),
        PromptSpec(
            "transcript_extraction",
            transcript_extraction_v1.PROMPT_VERSION,
            transcript_extraction_v1.INSTRUCTIONS,
            "TaskContext+transcript",
            "VideoExtractionResult",
            r".+",
            "2026-10-25",
        ),
        PromptSpec(
            "gap_elicitation",
            gap_elicitation_v1.PROMPT_VERSION,
            gap_elicitation_v1.INSTRUCTIONS,
            "gap_context_v1",
            "GapAnalysis",
            r"gemini-3[\w.-]*",
            "2026-10-25",
        ),
        PromptSpec(
            "grounded_qa",
            grounded_qa_v1.PROMPT_VERSION,
            grounded_qa_v1.INSTRUCTIONS,
            "approved_context_v1",
            "GroundedAnswer",
            r"gemini-3[\w.-]*",
            "2026-10-25",
        ),
    )
}
