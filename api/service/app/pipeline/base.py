from dataclasses import dataclass
from typing import Protocol


class PipelineError(Exception):
    """Any failure from the STT -> LLM -> TTS dependency. Backends MUST raise only this."""


@dataclass
class PipelineResult:
    transcript: str
    response_audio_ref: str
    latency_ms: float


class Pipeline(Protocol):
    def process(self, event_id: str, audio_ref: str) -> PipelineResult: ...