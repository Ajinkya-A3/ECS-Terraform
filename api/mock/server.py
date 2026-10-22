import asyncio
import os
import random

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

FAIL_RATE = float(os.getenv("MOCK_FAIL_RATE", "0.20"))
# mu=5.7 -> p50 ~ 300 ms. The spec's sigma=0.35 gives p90 ~ 470 ms / p99 ~ 675 ms,
# which misses the p90 ~ 700 / p99 ~ 1200 targets. sigma=0.6 gives ~645 / ~1207.
MU = 5.7
SIGMA = float(os.getenv("MOCK_SIGMA", "0.6"))

app = FastAPI(title="Mock STT/LLM/TTS")


class PipelineRequest(BaseModel):
    event_id: str
    audio_ref: str = "synthetic"


@app.get("/health")
def health():
    return {"status": "ok", "fail_rate": FAIL_RATE}


@app.post("/pipeline/process")
async def process(req: PipelineRequest):
    if random.random() < FAIL_RATE:
        return JSONResponse(
            status_code=503, content={"status": "error", "reason": "downstream_timeout"}
        )
    latency_ms = random.lognormvariate(MU, SIGMA)
    await asyncio.sleep(latency_ms / 1000)
    return {
        "status": "ok",
        "transcript": "synthetic transcript",
        "response_audio_ref": "synthetic",
        "latency_ms": latency_ms,
    }