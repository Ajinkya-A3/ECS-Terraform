import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS dead_letters (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     TEXT NOT NULL,
    payload      TEXT NOT NULL,
    error        TEXT NOT NULL,
    attempts     INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending | replayed
    replay_count INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
)
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DeadLetterQueue:
    """SQLite-backed DLQ. Swap this class for SQS on AWS; keep the same methods."""

    def __init__(self, path: str):
        self._path = path
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self._path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:  # commit / rollback
                yield conn
        finally:
            conn.close()

    def add(self, event_id: str, payload: dict, error: str, attempts: int) -> int:
        now = _now()
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO dead_letters (event_id, payload, error, attempts, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (event_id, json.dumps(payload), error, attempts, now, now),
            )
            return cur.lastrowid

    def list(self, status: str = "pending", limit: int = 100) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM dead_letters WHERE status = ? ORDER BY id LIMIT ?", (status, limit)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out

    def count(self, status: str = "pending") -> int:
        with self._conn() as c:
            return c.execute(
                "SELECT COUNT(*) FROM dead_letters WHERE status = ?", (status,)
            ).fetchone()[0]

    def mark_replayed(self, dlq_id: int) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE dead_letters SET status='replayed', replay_count=replay_count+1, updated_at=? WHERE id=?",
                (_now(), dlq_id),
            )

    def record_failed_replay(self, dlq_id: int, error: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE dead_letters SET error=?, replay_count=replay_count+1, updated_at=? WHERE id=?",
                (error, _now(), dlq_id),
            )