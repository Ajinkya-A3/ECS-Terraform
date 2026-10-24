import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    backend: str
    pipeline_url: str
    request_timeout_s: float
    retry_attempts: int
    retry_initial_s: float
    retry_max_s: float
    breaker_fail_max: int
    breaker_reset_s: float
    dlq_path: str
    whisper_model: str
    device: str
    compute_type: str
    llm_path: str
    piper_voice_path: str
    audio_out_dir: str


def load_settings() -> Settings:
    e = os.getenv
    return Settings(
        backend=e("PIPELINE_BACKEND", "http"),
        pipeline_url=e("PIPELINE_URL", "http://localhost:9000"),
        request_timeout_s=float(e("REQUEST_TIMEOUT_S", "3")),
        retry_attempts=int(e("RETRY_ATTEMPTS", "3")),
        retry_initial_s=float(e("RETRY_INITIAL_S", "0.2")),
        retry_max_s=float(e("RETRY_MAX_S", "2.0")),
        breaker_fail_max=int(e("BREAKER_FAIL_MAX", "5")),
        breaker_reset_s=float(e("BREAKER_RESET_S", "15")),
        dlq_path=e("DLQ_PATH", "dlq.db"),
        whisper_model=e("WHISPER_MODEL", "large-v3-turbo"),
        device=e("DEVICE", "cpu"),
        compute_type=e("COMPUTE_TYPE", "int8"),
        llm_path=e("LLM_PATH", "models/model.gguf"),
        piper_voice_path=e("PIPER_VOICE_PATH", "models/voice.onnx"),
        audio_out_dir=e("AUDIO_OUT_DIR", "out_audio"),
    )