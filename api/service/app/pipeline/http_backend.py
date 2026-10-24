import httpx

from app.pipeline.base import PipelineError, PipelineResult


class HttpPipeline:
    """Talks to the mock OR any real model server that honours POST /pipeline/process."""

    def __init__(self, base_url: str, timeout_s: float):
        self._client = httpx.Client(base_url=base_url, timeout=timeout_s)

    def process(self, event_id: str, audio_ref: str) -> PipelineResult:
        try:
            r = self._client.post(
                "/pipeline/process", json={"event_id": event_id, "audio_ref": audio_ref}
            )
        except httpx.HTTPError as e:
            raise PipelineError(f"transport_error:{type(e).__name__}") from e

        if r.status_code != 200:
            raise PipelineError(f"http_{r.status_code}")

        try:
            d = r.json()
            return PipelineResult(
                transcript=d["transcript"],
                response_audio_ref=d["response_audio_ref"],
                latency_ms=float(d["latency_ms"]),
            )
        except (ValueError, KeyError, TypeError) as e:
            raise PipelineError(f"bad_response:{type(e).__name__}") from e