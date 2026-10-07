"""Persistent call decisions and a single-server queue. Digits are never stored."""

import secrets
import sqlite3
import time
from contextlib import contextmanager

SESSION_SECONDS = 300
GLOBAL_CALL_LIMIT = 20
CALLER_CALL_LIMIT = 5
COOLDOWN_SECONDS = 30


class Store:
    def __init__(self, state):
        self.path = state / "state.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS calls (
                    sid TEXT PRIMARY KEY, caller TEXT NOT NULL,
                    created REAL NOT NULL, nonce TEXT NOT NULL,
                    outcome TEXT NOT NULL DEFAULT 'open'
                );
                CREATE INDEX IF NOT EXISTS calls_created ON calls(created);
                CREATE TABLE IF NOT EXISTS jobs (
                    sid TEXT PRIMARY KEY REFERENCES calls(sid),
                    status TEXT NOT NULL DEFAULT 'queued', created REAL NOT NULL,
                    started REAL, finished REAL, exit_code INTEGER
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def open_call(self, sid, caller):
        now = time.time()
        with self.transaction() as db:
            existing = db.execute("SELECT * FROM calls WHERE sid=?", (sid,)).fetchone()
            if existing:
                if (existing["caller"] == caller and existing["outcome"] == "open"
                        and existing["created"] > now - SESSION_SECONDS):
                    return existing["nonce"]
                return None
            counts = db.execute(
                "SELECT COUNT(*) AS total, COALESCE(SUM(caller=?), 0) AS caller_total "
                "FROM calls WHERE created>?", (caller, now - SESSION_SECONDS)
            ).fetchone()
            allowed = counts["total"] < GLOBAL_CALL_LIMIT and counts["caller_total"] < CALLER_CALL_LIMIT
            nonce = secrets.token_urlsafe(24)
            db.execute("INSERT INTO calls(sid, caller, created, nonce, outcome) VALUES (?, ?, ?, ?, ?)",
                       (sid, caller, now, nonce, "open" if allowed else "denied"))
            return nonce if allowed else None

    def activate(self, sid, caller, nonce, code_matches):
        now = time.time()
        with self.transaction() as db:
            call = db.execute("SELECT * FROM calls WHERE sid=?", (sid,)).fetchone()
            if (not call or call["caller"] != caller
                    or not secrets.compare_digest(call["nonce"], nonce)):
                return "denied"
            # A retry gets the original decision. It cannot change a rejected attempt.
            if call["outcome"] != "open":
                return call["outcome"]
            if now - call["created"] >= SESSION_SECONDS or not code_matches:
                outcome = "denied"
            elif db.execute("SELECT 1 FROM jobs WHERE status IN ('queued', 'running') "
                            "OR created>? LIMIT 1", (now - COOLDOWN_SECONDS,)).fetchone():
                outcome = "busy"
            else:
                db.execute("INSERT INTO jobs(sid, created) VALUES (?, ?)", (sid, now))
                outcome = "accepted"
            db.execute("UPDATE calls SET outcome=? WHERE sid=?", (outcome, sid))
            return outcome

    def recover(self):
        # Called only by the worker holding the process lock. Never rerun an uncertain job.
        with self.transaction() as db:
            return db.execute("UPDATE jobs SET status='interrupted', finished=? "
                              "WHERE status='running'", (time.time(),)).rowcount

    def claim(self):
        with self.transaction() as db:
            # Do not unexpectedly execute an old request after a prolonged worker outage.
            db.execute("UPDATE jobs SET status='expired', finished=? WHERE status='queued' AND created<?",
                       (time.time(), time.time() - SESSION_SECONDS))
            row = db.execute("SELECT sid FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET status='running', started=? WHERE sid=?", (time.time(), row["sid"]))
            return row["sid"]

    def finish(self, sid, status, exit_code):
        with self.transaction() as db:
            db.execute("UPDATE jobs SET status=?, exit_code=?, finished=? WHERE sid=?",
                       (status, exit_code, time.time(), sid))

    def jobs(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT 20")]
