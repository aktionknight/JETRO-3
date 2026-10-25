from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Heartbeat(Input):
    position_seconds: float = Field(ge=0, allow_inf_nan=False)


class Playground(Input):
    scenario: Literal["standard", "uncertain"]
    mode: Literal["guided", "assessment"]
    actions: list[str] = Field(min_length=1, max_length=500)


class Answer(Input):
    session_id: str
    question_id: str
    option_id: str
    reflection: str = Field(default="", max_length=4000)
    expected_revision: int = Field(ge=0)


class Completion(Input):
    stage: Literal["course", "playground", "interview"]
