"""Replays the Appendix C schedule against POST /calls using data/call_events.jsonl.

Default: 1 simulated hour = 1 real minute (--speed 1 = 1 simulated minute per real second).
Note this also multiplies load: 10 calls/min becomes 10 calls/s, the spike becomes 50 calls/s.
"""
import argparse
import json
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx


def rate_at(sim_minute: float) -> float:
    """Calls per simulated minute at a given minute-of-day (Appendix C)."""
    for start in (8 * 60, 14 * 60):  # 08:00-10:00 and 14:00-16:00
        if start <= sim_minute < start + 120:
            return 10 + 40 * min((sim_minute - start) / 15, 1.0)
    return 10


class EventSource:
    """Cycles through the dataset; repeat passes get fresh UUIDv7 ids to avoid duplicate ids."""

    def __init__(self, path: str):
        with open(path) as f:
            self._events = [json.loads(line) for line in f if line.strip()]
        if not self._events:
            raise SystemExit(f"{path} is empty - run scripts/generate_sample_data.py first")
        self._i = 0

    def next(self) -> dict:
        ev = dict(self._events[self._i % len(self._events)])
        if self._i >= len(self._events):
            ev["event_id"] = str(uuid.uuid7())
        self._i += 1
        ev["audio_ref"] = "synthetic"
        return ev


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--data", default="data/call_events.jsonl")
    ap.add_argument("--duration", type=int, default=120, help="real seconds to run")
    ap.add_argument("--speed", type=float, default=1.0, help="simulated minutes per real second")
    ap.add_argument("--start-hour", type=float, default=7.5, help="simulated start hour of day")
    args = ap.parse_args()

    source = EventSource(args.data)
    client = httpx.Client(base_url=args.url, timeout=30)
    stats: Counter = Counter()
    attempts_hist: Counter = Counter()

    def send(ev: dict):
        try:
            r = client.post("/calls", json=ev)
            if r.status_code in (200, 202):
                body = r.json()
                stats[body["status"]] += 1
                attempts_hist[body["attempts"]] += 1
            else:
                stats[f"http_{r.status_code}"] += 1
        except Exception:
            stats["client_error"] += 1

    with ThreadPoolExecutor(max_workers=200) as pool:
        for tick in range(args.duration):
            sim_minute = (args.start_hour * 60 + tick * args.speed) % 1440
            n = round(rate_at(sim_minute) * args.speed)  # calls this real second
            print(f"t={tick:4d}s sim={int(sim_minute) // 60:02d}:{int(sim_minute) % 60:02d} sending={n}")
            for _ in range(n):
                pool.submit(send, source.next())
            time.sleep(1)

    total = sum(stats.values())
    print(f"\nSent {total} calls")
    print("Status  :", dict(stats))
    print("Attempts:", dict(sorted(attempts_hist.items())), "(attempts -> number of calls)")


if __name__ == "__main__":
    main()