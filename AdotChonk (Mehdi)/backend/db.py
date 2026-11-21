"""SQLite storage: sessions, events, analyses, tasks, agent runs, drafts."""
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from . import config

# re-entrant: _exec/_query hold it and may call _get_conn(), which takes it again
# on first use (deadlocked any process whose first DB call was a write).
_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    started_at REAL NOT NULL,
    ended_at REAL,
    source TEXT NOT NULL DEFAULT 'extension',
    label TEXT NOT NULL DEFAULT 'observation'
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    app TEXT NOT NULL DEFAULT 'other',
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, ts);
CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    raw TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    name TEXT NOT NULL,
    apps TEXT NOT NULL DEFAULT '[]',
    steps TEXT NOT NULL DEFAULT '[]',
    repetitions INTEGER NOT NULL DEFAULT 1,
    avg_minutes REAL NOT NULL DEFAULT 0,
    weekly_minutes REAL NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0,
    evidence TEXT NOT NULL DEFAULT '[]',
    proposal TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'new',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    mode TEXT NOT NULL,
    demo INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'running',
    started_at REAL NOT NULL,
    finished_at REAL,
    report TEXT
);
CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_run_events ON run_events(run_id, id);
CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    run_id TEXT,
    email_id TEXT,
    to_addr TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    thread_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending', /* pending | approved | skipped | edited | sent */
    created_at REAL NOT NULL
);
"""


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        # timeout + WAL: the desktop agent runs in its own process and writes run
        # events while the server reads/writes the same database
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=15)
        _conn.row_factory = sqlite3.Row
        with _lock:
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.executescript(SCHEMA)
    return _conn


def init() -> None:
    _get_conn()


def _exec(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    with _lock:
        cur = _get_conn().execute(sql, params)
        _get_conn().commit()
        return cur


def _query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        return _get_conn().execute(sql, params).fetchall()


def new_id() -> str:
    return uuid.uuid4().hex[:12]


# ---- sessions & events -------------------------------------------------------

def start_session(label: str, source: str = "extension") -> dict:
    sid = new_id()
    _exec("INSERT INTO sessions(id, started_at, source, label) VALUES (?,?,?,?)",
          (sid, time.time(), source, label))
    return {"id": sid, "label": label, "source": source, "started_at": time.time()}


def stop_session(session_id: str) -> Optional[dict]:
    _exec("UPDATE sessions SET ended_at=? WHERE id=?", (time.time(), session_id))
    rows = _query("SELECT * FROM sessions WHERE id=?", (session_id,))
    return dict(rows[0]) if rows else None


def latest_session(open_only: bool = False, with_events: bool = False) -> Optional[dict]:
    """Most recent non-demo session, optionally only open ones / ones with events."""
    sql = "SELECT * FROM sessions s WHERE s.source != 'demo'"
    if open_only:
        sql += " AND s.ended_at IS NULL"
    if with_events:
        sql += " AND EXISTS (SELECT 1 FROM events e WHERE e.session_id = s.id)"
    rows = _query(sql + " ORDER BY s.started_at DESC LIMIT 1")
    return dict(rows[0]) if rows else None


def count_events(session_id: str) -> int:
    return _query("SELECT COUNT(*) FROM events WHERE session_id=?", (session_id,))[0][0]


def get_session(session_id: str) -> Optional[dict]:
    rows = _query("SELECT * FROM sessions WHERE id=?", (session_id,))
    return dict(rows[0]) if rows else None


def add_events(session_id: str, events: list[dict]) -> int:
    n = 0
    for e in events:
        _exec(
            "INSERT INTO events(session_id, ts, type, app, detail) VALUES (?,?,?,?,?)",
            (session_id, float(e.get("timestamp_ms", 0)) / 1000.0, e.get("type", "unknown"),
             e.get("app", "other"), json.dumps(e.get("detail", {}))),
        )
        n += 1
    return n


def list_events(session_id: str) -> list[dict]:
    rows = _query("SELECT * FROM events WHERE session_id=? ORDER BY ts, id", (session_id,))
    out = []
    for r in rows:
        d = dict(r)
        d["detail"] = json.loads(d["detail"])
        d["timestamp_ms"] = int(d.pop("ts") * 1000)
        out.append(d)
    return out


def latest_session_id() -> Optional[str]:
    rows = _query("SELECT id FROM sessions ORDER BY started_at DESC LIMIT 1")
    return rows[0]["id"] if rows else None


# ---- analyses & tasks --------------------------------------------------------

def save_analysis(session_id: str, analysis: dict) -> None:
    _exec("INSERT INTO analyses(session_id, created_at, raw) VALUES (?,?,?)",
          (session_id, time.time(), json.dumps(analysis)))


def get_latest_analysis() -> Optional[dict]:
    rows = _query("SELECT * FROM analyses ORDER BY created_at DESC LIMIT 1")
    return json.loads(rows[0]["raw"]) if rows else None


def save_tasks(session_id: str, workflows: list[dict]) -> None:
    for w in workflows:
        tid = w.get("id") or new_id()
        _exec(
            """INSERT OR REPLACE INTO tasks
               (id, session_id, name, apps, steps, repetitions, avg_minutes, weekly_minutes,
                confidence, evidence, proposal, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (tid, session_id, w["name"], json.dumps(w.get("apps", [])), json.dumps(w.get("steps", [])),
             int(w.get("times_repeated_in_session", 1)), float(w.get("avg_minutes_per_repetition", 0)),
             float(w.get("estimated_weekly_minutes", 0)), float(w.get("confidence", 0.5)),
             json.dumps(w.get("evidence", [])), json.dumps(w.get("automation_proposal", {})),
             w.get("status", "new"), time.time()),
        )
        w["id"] = tid


def list_tasks(include_dismissed: bool = False) -> list[dict]:
    sql = "SELECT * FROM tasks"
    if not include_dismissed:
        sql += " WHERE status != 'dismissed'"
    sql += " ORDER BY weekly_minutes DESC, created_at DESC"
    out = []
    for r in _query(sql):
        d = dict(r)
        for k in ("apps", "steps", "evidence", "proposal"):
            d[k] = json.loads(d[k])
        out.append(d)
    return out


def get_task(task_id: str) -> Optional[dict]:
    rows = _query("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not rows:
        return None
    d = dict(rows[0])
    for k in ("apps", "steps", "evidence", "proposal"):
        d[k] = json.loads(d[k])
    return d


def set_task_status(task_id: str, status: str) -> None:
    _exec("UPDATE tasks SET status=? WHERE id=?", (status, task_id))


# ---- runs & drafts -----------------------------------------------------------

def create_run(task_id: Optional[str], mode: str, demo: bool) -> str:
    rid = new_id()
    _exec("INSERT INTO runs(id, task_id, mode, demo, status, started_at) VALUES (?,?,?,?,?,?)",
          (rid, task_id, mode, int(demo), "running", time.time()))
    return rid


def add_run_event(run_id: str, kind: str, message: str, data: Any = None) -> None:
    _exec("INSERT INTO run_events(run_id, ts, kind, message, data) VALUES (?,?,?,?,?)",
          (run_id, time.time(), kind, message, json.dumps(data or {})))


def get_run_events(run_id: str, after_id: int = 0) -> list[dict]:
    rows = _query("SELECT * FROM run_events WHERE run_id=? AND id>? ORDER BY id", (run_id, after_id))
    out = []
    for r in rows:
        d = dict(r)
        d["data"] = json.loads(d["data"])
        out.append(d)
    return out


def finish_run(run_id: str, report: dict) -> None:
    _exec("UPDATE runs SET status='finished', finished_at=?, report=? WHERE id=?",
          (time.time(), json.dumps(report), run_id))


def get_run(run_id: str) -> Optional[dict]:
    rows = _query("SELECT * FROM runs WHERE id=?", (run_id,))
    if not rows:
        return None
    d = dict(rows[0])
    d["report"] = json.loads(d["report"]) if d["report"] else None
    return d


def list_runs(limit: int = 10) -> list[dict]:
    out = []
    for r in _query("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)):
        d = dict(r)
        d["report"] = json.loads(d["report"]) if d["report"] else None
        out.append(d)
    return out


def draft_exists_for_thread(thread_id: str) -> bool:
    return bool(_query("SELECT 1 FROM drafts WHERE thread_id=? LIMIT 1", (thread_id,)))


def set_draft_status_by_thread(thread_id: str, status: str) -> None:
    _exec("UPDATE drafts SET status=? WHERE thread_id=?", (status, thread_id))


def save_draft(run_id: Optional[str], email_id: Optional[str], to_addr: str, subject: str,
               body: str, thread_id: Optional[str] = None) -> str:
    did = new_id()
    _exec("INSERT INTO drafts(id, run_id, email_id, to_addr, subject, body, thread_id, status, created_at)"
          " VALUES (?,?,?,?,?,?,?,?,?)",
          (did, run_id, email_id, to_addr, subject, body, thread_id, "pending", time.time()))
    return did


def list_drafts(run_id: Optional[str] = None, status: Optional[str] = None) -> list[dict]:
    sql = "SELECT * FROM drafts"
    cond, params = [], []
    if run_id:
        cond.append("run_id=?"); params.append(run_id)
    if status:
        cond.append("status=?"); params.append(status)
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    sql += " ORDER BY created_at DESC"
    return [dict(r) for r in _query(sql, tuple(params))]


def get_draft(draft_id: str) -> Optional[dict]:
    rows = _query("SELECT * FROM drafts WHERE id=?", (draft_id,))
    return dict(rows[0]) if rows else None


def set_draft_status(draft_id: str, status: str, body: Optional[str] = None) -> None:
    if body is not None:
        _exec("UPDATE drafts SET status=?, body=? WHERE id=?", (status, body, draft_id))
    else:
        _exec("UPDATE drafts SET status=? WHERE id=?", (status, draft_id))
