import logging

from app.dlq import DeadLetterQueue
from app.resilience import CallFailed, ResilientPipeline
from app.schemas import CallEvent, CallResponse

log = logging.getLogger("service")


class CallService:
    def __init__(self, pipeline: ResilientPipeline, dlq: DeadLetterQueue):
        self._pipeline = pipeline
        self._dlq = dlq

    def handle(self, event: CallEvent) -> CallResponse:
        event_id = str(event.event_id)
        log.info("call_received event_id=%s campaign=%s", event_id, event.campaign_id)
        try:
            result, attempts = self._pipeline.process(event_id, event.audio_ref)
        except CallFailed as e:
            dlq_id = self._dlq.add(event_id, event.model_dump(mode="json"), e.reason, e.attempts)
            log.error(
                "call_failed event_id=%s reason=%s attempts=%d dlq_id=%d",
                event_id, e.reason, e.attempts, dlq_id,
            )
            return CallResponse(
                event_id=event_id, status="degraded", outcome="failed_downstream",
                attempts=e.attempts, error=e.reason, dlq_id=dlq_id,
            )

        log.info("call_completed event_id=%s attempts=%d", event_id, attempts)
        return CallResponse(
            event_id=event_id, status="completed", outcome=event.outcome, attempts=attempts,
            transcript=result.transcript, response_audio_ref=result.response_audio_ref,
            latency_ms=result.latency_ms,
        )

    def replay_dlq(self, limit: int = 100) -> dict:
        replayed = still_failing = 0
        for item in self._dlq.list("pending", limit):
            try:
                self._pipeline.process(item["event_id"], item["payload"].get("audio_ref", "synthetic"))
            except CallFailed as e:
                self._dlq.record_failed_replay(item["id"], e.reason)
                still_failing += 1
                if e.reason == "circuit_open":
                    break  # downstream still down, stop hammering it
            else:
                self._dlq.mark_replayed(item["id"])
                replayed += 1
        log.info("dlq_replay replayed=%d still_failing=%d", replayed, still_failing)
        return {"replayed": replayed, "still_failing": still_failing}