"""Small SQLite state: what we distributed (needed for safe pruning) and an audit log."""
from __future__ import annotations

import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS copies (
    dst        TEXT PRIMARY KEY,
    src        TEXT NOT NULL,
    digest     TEXT NOT NULL,
    project    TEXT NOT NULL,
    rule       TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS copies_rule ON copies(project, rule);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    action  TEXT NOT NULL,     -- copied | skipped | pruned | failed | denied
    project TEXT, rule TEXT, src TEXT, dst TEXT, digest TEXT, message TEXT
);
"""


class State:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    def record_copy(self, dst: str, src: str, digest: str, project: str, rule: str):
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO copies(dst,src,digest,project,rule,updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(dst) DO UPDATE SET src=excluded.src, digest=excluded.digest, "
                "project=excluded.project, rule=excluded.rule, updated_at=excluded.updated_at",
                (dst, src, digest, project, rule, time.time()),
            )

    def forget(self, dst: str):
        with self.lock, self.db:
            self.db.execute("DELETE FROM copies WHERE dst=?", (dst,))

    def managed(self, project: str, rule: str) -> list[sqlite3.Row]:
        with self.lock:
            return list(self.db.execute("SELECT * FROM copies WHERE project=? AND rule=?", (project, rule)))

    def event(self, action: str, project: str = "", rule: str = "", src: str = "", dst: str = "",
              digest: str = "", message: str = ""):
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO events(ts,action,project,rule,src,dst,digest,message) VALUES(?,?,?,?,?,?,?,?)",
                (time.time(), action, project, rule, src, dst, digest, message),
            )

    def events(self, limit: int = 100, project: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM events", []
        if project:
            q, args = q + " WHERE project=?", [project]
        with self.lock:
            rows = self.db.execute(q + " ORDER BY id DESC LIMIT ?", (*args, limit))
            return [dict(r) for r in rows]

    def counters(self) -> dict[str, int]:
        with self.lock:
            return {r[0]: r[1] for r in self.db.execute("SELECT action, COUNT(*) FROM events GROUP BY action")}
