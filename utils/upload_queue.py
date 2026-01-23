import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional, Tuple


class SQLiteUploadQueue:
    """Tiny persistent queue backed by SQLite for event uploads."""

    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.execute('PRAGMA journal_mode=WAL;')
        self.conn.execute('PRAGMA synchronous=NORMAL;')
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self):
        with self._lock:
            self.conn.execute(
                'CREATE TABLE IF NOT EXISTS queue ('
                'id INTEGER PRIMARY KEY AUTOINCREMENT,'
                'payload TEXT NOT NULL,'
                'retries INTEGER NOT NULL DEFAULT 0,'
                'next_retry REAL NOT NULL DEFAULT 0,'
                'created REAL NOT NULL'
                ')'
            )
            if self.conn.in_transaction:
                try:
                    self.conn.commit()
                except sqlite3.OperationalError:
                    pass

    def enqueue(self, payload: dict):
        data = json.dumps(payload, ensure_ascii=False)
        now = time.time()
        with self._lock:
            self.conn.execute(
                'INSERT INTO queue (payload, retries, next_retry, created) VALUES (?, 0, 0, ?)',
                (data, now)
            )
            if self.conn.in_transaction:
                try:
                    self.conn.commit()
                except sqlite3.OperationalError:
                    pass

    def next_job(self) -> Optional[Tuple[int, dict, int]]:
        now = time.time()
        with self._lock:
            row = self.conn.execute(
                'SELECT id, payload, retries FROM queue WHERE next_retry <= ? ORDER BY id LIMIT 1',
                (now,)
            ).fetchone()
            if not row:
                return None
            job_id, payload_json, retries = row
            try:
                payload = json.loads(payload_json)
            except json.JSONDecodeError:
                payload = {}
            return job_id, payload, int(retries)

    def mark_success(self, job_id: int):
        with self._lock:
            self.conn.execute('DELETE FROM queue WHERE id=?', (job_id,))
            if self.conn.in_transaction:
                try:
                    self.conn.commit()
                except sqlite3.OperationalError:
                    pass

    def mark_failure(self, job_id: int, retries: int, delay_seconds: float):
        next_retry = time.time() + max(1.0, float(delay_seconds))
        with self._lock:
            self.conn.execute(
                'UPDATE queue SET retries=?, next_retry=? WHERE id=?',
                (retries, next_retry, job_id)
            )
            if self.conn.in_transaction:
                try:
                    self.conn.commit()
                except sqlite3.OperationalError:
                    pass

    def pending(self) -> int:
        with self._lock:
            row = self.conn.execute('SELECT COUNT(1) FROM queue').fetchone()
            return int(row[0]) if row else 0

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass
