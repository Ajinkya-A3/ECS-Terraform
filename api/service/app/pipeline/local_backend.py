import os
import threading
import time
import wave

from app.config import Settings
from app.pipeline.base import PipelineError, PipelineResult

SYSTEM_PROMPT = (
    "You are a polite, compliant debt-collections voice agent. "
    "Reply in one or two short sentences."
)


class LocalPipeline:
    """faster-whisper (STT) -> llama.cpp GGUF (LLM) -> Piper (TTS).

    audio_ref must be a path to a real audio file (wav/mp3).
    """

    def __init__(self, s: Settings):
        # lazy imports so the mock path never needs these heavy deps
        from faster_whisper import WhisperModel
        from llama_cpp import Llama
        from piper import PiperVoice

        self._stt = WhisperModel(s.whisper_model, device=s.device, compute_type=s.compute_type)
        self._llm = Llama(
            model_path=s.llm_path,
            n_ctx=2048,
            n_gpu_layers=-1 if s.device == "cuda" else 0,
            verbose=False,
        )
        self._tts = PiperVoice.load(s.piper_voice_path)
        self._out_dir = s.audio_out_dir
        os.makedirs(self._out_dir, exist_ok=True)
        self._lock = threading.Lock()  # these models are not safe for concurrent calls

    def process(self, event_id: str, audio_ref: str) -> PipelineResult:
        start = time.perf_counter()
        try:
            with self._lock:
                segments, _ = self._stt.transcribe(audio_ref)
                transcript = " ".join(seg.text.strip() for seg in segments)

                out = self._llm.create_chat_completion(
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": transcript},
                    ],
                    max_tokens=128,
                )
                reply = out["choices"][0]["message"]["content"].strip()

                out_path = os.path.join(self._out_dir, f"{event_id}.wav")
                with wave.open(out_path, "wb") as wf:
                    self._tts.synthesize_wav(reply, wf)
        except Exception as e:  # normalise everything so resilience logic stays model-agnostic
            raise PipelineError(f"local_pipeline_error:{type(e).__name__}:{e}") from e

        return PipelineResult(
            transcript=transcript,
            response_audio_ref=out_path,
            latency_ms=(time.perf_counter() - start) * 1000,
        )