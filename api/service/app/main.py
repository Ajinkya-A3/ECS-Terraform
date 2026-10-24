import logging

from fastapi import FastAPI, Response

from app.config import load_settings
from app.dlq import DeadLetterQueue
from app.pipeline.factory import build_backend
from app.resilience import ResilientPipeline
from app.schemas import CallEvent, CallResponse
from app.service import CallService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

settings = load_settings()
resilient = ResilientPipeline(build_backend(settings), settings)
dlq = DeadLetterQueue(settings.dlq_path)
service = CallService(resilient, dlq)

app = FastAPI(title="Collections Voice-Agent Inference Service")


@app.post("/calls", response_model=CallResponse)
def create_call(event: CallEvent, response: Response):
    result = service.handle(event)
    if result.status == "degraded":
        response.status_code = 202  # accepted, captured in DLQ for replay
    return result


@app.get("/health")
def health():
    return {
        "status": "ok",
        "backend": settings.backend,
        "pipeline_url": settings.pipeline_url if settings.backend == "http" else None,
        "breaker_state": resilient.breaker.current_state,
        "dlq_pending": dlq.count("pending"),
    }


@app.get("/dlq")
def list_dlq(status: str = "pending", limit: int = 100):
    return dlq.list(status, limit)


@app.post("/dlq/replay")
def replay_dlq(limit: int = 100):
    return service.replay_dlq(limit)