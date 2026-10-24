"""Generate synthetic Appendix A call events with UUIDv7 ids (needs Python 3.14+)."""
import argparse
import json
import random
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

BUCKETS = ["0-30 DPD", "31-60 DPD", "61-90 DPD", "90+ DPD"]
PRODUCTS = ["credit_card", "bnpl", "personal_loan"]
CAMPAIGN_WINDOWS = [(8, 10), (14, 16)]  # Appendix C spike windows (hours)
OUTCOME_SPLIT = [("connected", 0.60), ("no_answer", 0.20), ("ptp_taken", 0.15), ("dispute", 0.05)]
DURATION_RANGE = {"connected": (20, 180), "ptp_taken": (60, 240), "dispute": (30, 200)}
RETRY_COUNT = {"failed_downstream": 2, "no_answer": 1}


def build_outcomes(n: int, fail_rate: float) -> list[str]:
    """Exactly fail_rate*n failures, the rest split 60/20/15/5, then shuffled."""
    n_fail = round(n * fail_rate)
    rest = n - n_fail
    counts = [round(rest * w) for _, w in OUTCOME_SPLIT]
    counts[0] += rest - sum(counts)  # absorb rounding drift
    outcomes = ["failed_downstream"] * n_fail
    for (name, _), c in zip(OUTCOME_SPLIT, counts):
        outcomes += [name] * c
    random.shuffle(outcomes)  # randomises WHICH records are the failed ones
    return outcomes


def random_timestamp(day: datetime) -> datetime:
    start_hour, end_hour = random.choice(CAMPAIGN_WINDOWS)
    offset = random.randrange((end_hour - start_hour) * 3600)
    return day.replace(hour=start_hour) + timedelta(seconds=offset)


def build_event(day: datetime, outcome: str) -> dict:
    lo, hi = DURATION_RANGE.get(outcome, (0, 0))
    return {
        "event_id": str(uuid.uuid7()),
        "campaign_id": "CAMP-2026-09-A",
        "borrower_bucket": random.choice(BUCKETS),
        "product_type": random.choice(PRODUCTS),
        "timestamp": random_timestamp(day).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "call_duration_s": random.randint(lo, hi) if hi else 0,
        "outcome": outcome,
        "retry_count": RETRY_COUNT.get(outcome, 0),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=500)
    ap.add_argument("--fail-rate", type=float, default=0.20)
    ap.add_argument("--date", default="2026-09-01", help="campaign day, YYYY-MM-DD")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--out", default="data/call_events.jsonl")
    args = ap.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    day = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    events = [build_event(day, o) for o in build_outcomes(args.count, args.fail_rate)]
    events.sort(key=lambda e: e["timestamp"])

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")

    print(f"wrote {len(events)} events to {out}")
    for name, c in Counter(e["outcome"] for e in events).most_common():
        print(f"  {name:18s} {c:4d}  ({c / len(events):.0%})")


if __name__ == "__main__":
    main()