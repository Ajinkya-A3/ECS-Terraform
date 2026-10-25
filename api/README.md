# Collections Voice-Agent Inference Service

A resilient inference service for a collections voice agent. Each call event goes through an STT → LLM → TTS pipeline, which is flaky (the mock fails 20% of requests). The service wraps that dependency with **retries + backoff, a circuit breaker, and a replayable dead-letter queue (DLQ)**, so a call is never silently dropped: every event ends as a completed response or as an inspectable DLQ row plus an ERROR log.

The dependency sits behind a one-method interface, so the mock can be swapped for real open-weight models without touching the resilience code.

## Project layout

```
.
├── docker-compose.yml          # runs mock + service together
├── data/
│   └── call_events.jsonl       # 500 sample call events (created by script/generate.py)
├── script/                     # host-side tools (not in containers)
│   ├── generate.py             # creates the sample data
│   └── traffic.py              # replays the Appendix C traffic schedule
├── mock/                       # fake STT→LLM→TTS dependency (own image)
│   ├── Dockerfile
│   ├── requirements.txt
│   └── server.py
└── service/                    # the inference service (own image)
    ├── Dockerfile
    ├── requirements.txt        # deps for the mock/http path
    ├── requirements-real.txt   # extra deps for real models (local backend)
    └── app/
        ├── main.py             # FastAPI endpoints + startup wiring
        ├── config.py           # environment-variable settings
        ├── schemas.py          # request/response models
        ├── service.py          # call handling + DLQ replay
        ├── resilience.py       # retry + circuit breaker
        ├── dlq.py              # SQLite dead-letter queue
        └── pipeline/           # the swappable dependency boundary
            ├── base.py         # contract: Pipeline, PipelineResult, PipelineError
            ├── http_backend.py # calls a remote server (mock or GPU model server)
            ├── local_backend.py# runs real models in-process
            └── factory.py      # picks the backend from PIPELINE_BACKEND
```

## How it works

```
traffic.py ──POST /calls──▶ service :8000
                              │
                       CallService (service.py)
                              │
              ┌───────────────▼────────────────┐
              │ ResilientPipeline (resilience) │
              │ retry: backoff + jitter        │
              │   └─ circuit breaker           │
              │        └─ Pipeline.process() ◀── swappable
              └───────────────┬────────────────┘
          success │                    │ retries exhausted / breaker open
                  ▼                    ▼
         200 "completed"      DLQ row (SQLite) + 202 "degraded"
                                       │
                 POST /dlq/replay ◀────┘ after the dependency recovers
```

### The files, one by one

| File | What it does |
|---|---|
| `main.py` | Builds everything at startup (settings → backend → resilience wrapper → DLQ → service) and exposes the endpoints. Returns `202` when a call was dead-lettered. |
| `config.py` | Reads settings from environment variables, with defaults. |
| `schemas.py` | Validates input. `event_id` must be a UUIDv7, and `borrower_bucket`, `outcome`, and other fields use fixed value sets. Bad input gets `422`. Also defines the response shape. |
| `service.py` | Success: returns the transcript. Failure: writes the event to the DLQ, logs an ERROR, returns `degraded`. Replay: re-processes pending DLQ rows oldest-first and stops early if the breaker is open. |
| `resilience.py` | Retries up to `RETRY_ATTEMPTS` with exponential backoff and jitter. A circuit breaker inside it opens after `BREAKER_FAIL_MAX` consecutive failures and fails fast. An open breaker is not retried. DLQ reasons keep the real cause, e.g. `circuit_open (last: http_503)`. |
| `dlq.py` | SQLite table holding payload, error, attempts, status (`pending` or `replayed`), replay count, and timestamps. On AWS only this file changes (to SQS). |
| `pipeline/base.py` | The contract. `Pipeline.process(event_id, audio_ref)`, the `PipelineResult` shape, and the single `PipelineError` that every backend must raise. |
| `pipeline/http_backend.py` | POSTs to `{PIPELINE_URL}/pipeline/process`. Network errors, non-200 responses, and malformed bodies all become `PipelineError`. |
| `pipeline/local_backend.py` | Runs faster-whisper → llama.cpp → Piper in-process. Models load once, a lock serialises calls, and any exception becomes `PipelineError`. |
| `pipeline/factory.py` | Chooses the backend from `PIPELINE_BACKEND` (`http` or `local`). Heavy imports happen only for `local`. |
| `mock/server.py` | `POST /pipeline/process`: 20% of requests return `503 {"status":"error","reason":"downstream_timeout"}`, the rest sleep for a lognormal delay (p50 ≈ 300 ms, p90 ≈ 645 ms, p99 ≈ 1200 ms) and return a synthetic transcript. |
| `script/generate.py` | Writes the sample events. |
| `script/traffic.py` | Sends the events to the service on the Appendix C schedule. |

## Prerequisites

- Docker with Compose v2.
- Python **3.14** on the host for the scripts (`uuid.uuid7()` is new in 3.14).

## Setup and run

Run everything from the folder that contains `docker-compose.yml`.

```bash
python3.14 -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install httpx

python script/generate.py --seed 42     # creates data/call_events.jsonl
docker compose up --build -d            # starts mock (:9000) and service (:8000)
curl localhost:8000/health              # expect backend=http, breaker_state=closed, dlq_pending=0
```

Stop with `docker compose down`, or `docker compose down -v` to also wipe the DLQ volume.

## Generating traffic

Traffic generation is two steps: **create the events, then replay them on a schedule.**

### Step 1: create the sample events (`script/generate.py`)

```bash
python script/generate.py --seed 42
```

It writes 500 events (one JSON object per line) to `data/call_events.jsonl`:
- exactly **100 (20%) are `failed_downstream`**, at random positions;
- the other 400 are split 60/20/15/5 across `connected`, `no_answer`, `ptp_taken`, and `dispute`;
- each event has a fresh UUIDv7 `event_id`, a random borrower bucket and product type, and a timestamp inside one of the two campaign windows (08:00–10:00, 14:00–16:00).

| Option | Default | Meaning |
|---|---|---|
| `--count` | `500` | number of events |
| `--fail-rate` | `0.20` | share labelled `failed_downstream` |
| `--date` | `2026-09-01` | campaign day |
| `--seed` | none | makes output repeatable |
| `--out` | `data/call_events.jsonl` | output path |

The `failed_downstream` labels are dataset values. Real failures come from the mock's independent 20% random rolls.

### Step 2: replay on the schedule (`script/traffic.py`)

```bash
python script/traffic.py --duration 120
```

Every real second it:
1. works out the simulated time of day;
2. looks up the target rate from the Appendix C schedule (10 calls/min baseline; 10 → 50 over 15 minutes then hold at 50 during 08:00–10:00 and 14:00–16:00);
3. sends that many events from the file, one `POST /calls` each, concurrently;
4. prints a line like `t=  45s sim=08:15 sending=50`.

At the end it prints a summary of `completed` vs `degraded` calls and how many attempts each needed. When the file runs out it wraps around, and repeated events get a new UUIDv7.

| Option | Default | Meaning |
|---|---|---|
| `--url` | `http://localhost:8000` | service address |
| `--data` | `data/call_events.jsonl` | events file |
| `--duration` | `120` | real seconds to run |
| `--speed` | `1.0` | simulated minutes per real second |
| `--start-hour` | `7.5` | simulated start time (07:30, so the first ramp shows quickly) |

### Time compression and speed

`--speed` compresses the 24-hour day. At `--speed 1`, 1 simulated hour = 1 real minute, and the rate scales with it: 10 calls/min becomes **10 calls per second** and the spike becomes **50 calls per second** (60× the real request rate). Rounding happens per second, so use `--speed 0.1` or higher; below that the baseline rounds to zero calls per second.

| Goal | Command |
|---|---|
| Quick check | `python script/traffic.py --duration 60 --speed 0.2` |
| See the ramp and hold (gentler load) | `python script/traffic.py --duration 300 --speed 0.2` |
| Jump straight to the spike | `python script/traffic.py --start-hour 8 --duration 60` |
| Full simulated day | `python script/traffic.py --start-hour 0 --duration 1440` (24 real minutes at speed 1) |
| Service on another host | `python script/traffic.py --url http://<host>:8000` |

At `--speed 0.2` the peak is about 10 calls/s. At `--speed 1` the 50 calls/s spike can saturate the service's thread pool, so drop the speed if you see `client_error`.

### Reading the summary

```
Sent 240 calls
Status  : {'completed': 238, 'degraded': 2}
Attempts: {1: 200, 2: 30, 3: 10} (attempts -> number of calls)
```

With independent 20% failures expect about 80% on attempt 1, 16% on attempt 2, 4% on attempt 3, and about 0.8% (0.2³) degraded. Every call is accounted for: it either completed or sits in the DLQ.

## Inspecting the DLQ and replaying

```bash
curl localhost:8000/health                  # breaker_state and dlq_pending
curl localhost:8000/dlq                     # pending dead letters
curl "localhost:8000/dlq?status=replayed"   # already replayed
curl -X POST "localhost:8000/dlq/replay?limit=500"
```

Each row holds the original payload, the error (`http_503`, or `circuit_open (last: ...)`), attempts, status, and timestamps. Replay runs rows through the same retry and breaker logic. Some may fail again because of the random failures, so run it again until `dlq_pending` reaches 0.

## Proving the circuit breaker

```bash
docker compose stop mock
python script/traffic.py --duration 20 --speed 0.2
curl localhost:8000/health                  # breaker_state: open, calls return 202 and fill the DLQ
docker compose start mock
sleep 20                                    # breaker reset is 15 s
curl -X POST "localhost:8000/dlq/replay?limit=500"
curl localhost:8000/health                  # breaker_state: closed, dlq_pending near 0
```

Independent 20% noise never opens the breaker, which is correct. For a correlated failure, run the mock with `MOCK_FAIL_RATE=0.9 docker compose up -d --force-recreate mock` and restore it afterwards.

## Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/calls` | POST | Process a call event. `200 completed`, `202 degraded` (with `dlq_id`), or `422` for invalid input. |
| `/health` | GET | Backend, `pipeline_url`, `breaker_state`, `dlq_pending`. |
| `/dlq` | GET | List dead letters (`status`, `limit`). |
| `/dlq/replay` | POST | Replay pending dead letters (`limit`). |
| `/docs` | GET | Swagger UI. |
| mock `:9000/pipeline/process` | POST | The dependency contract. |
| mock `:9000/health` | GET | Shows `fail_rate` and `sigma`. |

## Configuration

Set in `docker-compose.yml`, overridable from the shell or a `.env` file. No secrets are used anywhere.

| Variable | Default | Purpose |
|---|---|---|
| `PIPELINE_BACKEND` | `http` | `http` or `local` |
| `PIPELINE_URL` | `http://mock:9000` | dependency location |
| `REQUEST_TIMEOUT_S` | `3` | per-attempt timeout; keep above the dependency's p99 |
| `RETRY_ATTEMPTS` | `3` | total attempts per call |
| `RETRY_INITIAL_S` / `RETRY_MAX_S` | `0.2` / `2.0` | backoff start and cap |
| `BREAKER_FAIL_MAX` | `5` | consecutive failures before opening |
| `BREAKER_RESET_S` | `15` | seconds open before a trial call |
| `DLQ_PATH` | `/data/dlq.db` | SQLite file on the `dlq-data` volume |
| `MOCK_FAIL_RATE` | `0.20` | mock failure probability (`1.0` = outage) |
| `MOCK_SIGMA` | `0.6` | mock latency spread |

The `local` backend also reads `WHISPER_MODEL`, `DEVICE`, `COMPUTE_TYPE`, `LLM_PATH`, `PIPER_VOICE_PATH`, `AUDIO_OUT_DIR`. After changing a value, run `docker compose up -d --force-recreate`.

## Swapping the mock for a real open-weight model

The service depends only on `Pipeline.process(event_id, audio_ref) -> PipelineResult` and the single `PipelineError`, so the swap happens behind that line and `resilience.py` is untouched. For a remote GPU server, expose the same `POST /pipeline/process` contract as the mock (a small FastAPI app that loads faster-whisper `large-v3-turbo`, a Mistral-7B or Llama-3.1-8B GGUF via llama.cpp, and Piper once at startup), run it on a g5.xlarge, and set `PIPELINE_URL` to its address; build that image on a CUDA base, likely with Python 3.12 until `ctranslate2` and `llama-cpp-python` publish 3.14 wheels. For in-process use, `pip install -r service/requirements-real.txt`, mount the model files, set `PIPELINE_BACKEND=local` and `DEVICE=cuda`, and pass a real audio path as `audio_ref`; `LocalPipeline` already loads models once, serialises calls with a lock, and converts any CUDA, decoding, or tokenizer error into `PipelineError`. Two settings need re-tuning: raise `REQUEST_TIMEOUT_S` above the real p99 so slow healthy calls are not retried as failures, and add a fault-injection toggle in front of the pipeline to keep the 20% failure scenario. Because the lock limits one process to roughly 40 calls/min at ~1.5 s/call, scale GPU replicas for the 50 calls/min spike.

## Design notes

- **Mock latency:** the spec's `sigma=0.35` gives p90 ≈ 470 ms and p99 ≈ 675 ms, short of its own targets. `sigma=0.6` gives ≈ 645 / 1207 ms.
- **Sync service:** Whisper and llama.cpp block, so FastAPI's threadpool with sync endpoints fits.
- **DLQ:** SQLite locally. The AWS equivalent is SQS with a redrive policy (not SNS, which does not store messages for replay).

## Cost

See [COST.md](COST.md) for the two-AZ, two-NAT Fargate estimate, scaling with load and the cost ceiling.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `uuid` has no attribute `uuid7` | The host venv is not Python 3.14. |
| `No such file: data/call_events.jsonl` | Run from the folder containing `docker-compose.yml`, or run `generate.py` first. |
| Everything degraded, DLQ says `circuit_open (last: transport_error:ConnectTimeout)` | The service cannot reach the mock. Test with `docker compose exec service python -c "import httpx;print(httpx.post('http://mock:9000/pipeline/process',json={'event_id':'x'},timeout=5).status_code)"`, check proxy variables, then `docker compose down -v --remove-orphans` and start again. |
| `client_error` or timeouts during the spike | Lower `--speed`. |
| `422` on every call | Regenerate the data with `generate.py`. |

## Known limitations

- Breaker state and the SQLite DLQ are per process and per container. Multi-task deployments use SQS.
- Replay has no idempotency check on `event_id`, so a replayed event could be processed twice. Add one before production.

## License

Apache 2.0 (see `LICENSE`), as required by the challenge.