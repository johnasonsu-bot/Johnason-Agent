"""HTTP command payloads; none expose writable state fields."""

from typing import Literal

from pydantic import BaseModel


class CreateRunRequest(BaseModel):
    run_id: str
    mission_id: str
    epoch_id: str


class InterventionRequest(BaseModel):
    kind: Literal[
        "supplement",
        "correct",
        "constraint",
        "replan",
        "pause",
        "skip",
        "retry",
        "cancel",
    ]
    content: str
    context_version: int = 0
