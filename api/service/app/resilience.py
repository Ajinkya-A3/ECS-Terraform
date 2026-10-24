import logging

import pybreaker
from tenacity import (
    Retrying,
    before_sleep_log,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.config import Settings
from app.pipeline.base import Pipeline, PipelineError, PipelineResult

log = logging.getLogger("resilience")


class CallFailed(Exception):
    """Raised when a call could not be processed; carries what the DLQ needs."""

    def __init__(self, reason: str, attempts: int):
        super().__init__(reason)
        self.reason = reason
        self.attempts = attempts


class _BreakerLogger(pybreaker.CircuitBreakerListener):
    def state_change(self, cb, old_state, new_state):
        log.warning("circuit_breaker_state_change old=%s new=%s", old_state.name, new_state.name)


class ResilientPipeline:
    """Retry (outer) -> circuit breaker (inner) -> backend. Knows nothing about the backend."""

    def __init__(self, backend: Pipeline, s: Settings):
        self._backend = backend
        self._s = s
        self.breaker = pybreaker.CircuitBreaker(
            fail_max=s.breaker_fail_max,
            reset_timeout=s.breaker_reset_s,
            listeners=[_BreakerLogger()],
            name="pipeline",
        )

    def process(self, event_id: str, audio_ref: str) -> tuple[PipelineResult, int]:
        attempts = 0
        retrying = Retrying(
            stop=stop_after_attempt(self._s.retry_attempts),
            wait=wait_exponential_jitter(initial=self._s.retry_initial_s, max=self._s.retry_max_s),
            retry=retry_if_exception_type(PipelineError),  # CircuitBreakerError is NOT retried
            before_sleep=before_sleep_log(log, logging.WARNING),
            reraise=True,
        )
        try:
            for attempt in retrying:
                with attempt:
                    attempts = attempt.retry_state.attempt_number
                    result = self.breaker.call(self._backend.process, event_id, audio_ref)
            return result, attempts
        except pybreaker.CircuitBreakerError as e:
            raise CallFailed("circuit_open", attempts) from e
        except PipelineError as e:
            raise CallFailed(str(e), attempts) from e