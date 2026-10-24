from app.config import Settings
from app.pipeline.base import Pipeline


def build_backend(s: Settings) -> Pipeline:
    if s.backend == "http":
        from app.pipeline.http_backend import HttpPipeline
        return HttpPipeline(s.pipeline_url, s.request_timeout_s)
    if s.backend == "local":
        from app.pipeline.local_backend import LocalPipeline
        return LocalPipeline(s)
    raise ValueError(f"Unknown PIPELINE_BACKEND: {s.backend!r} (use 'http' or 'local')")