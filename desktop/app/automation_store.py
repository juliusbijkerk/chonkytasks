"""Local state shared by the UI server and the independently running worker."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import uuid

from . import office_store

ROOT = Path(__file__).resolve().parent.parent
DATABASE = ROOT / "data" / "automations.sqlite3"
AUTOMATION_ID = "meeting-reply-drafts"
NO_PENDING_OFFICE_REVIEW = "NOT EXISTS (SELECT 1 FROM office_reviews WHERE run_id=runs.id AND status='ready')"


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connection():
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATABASE, timeout=15)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS automations (id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS messages (
            fingerprint TEXT PRIMARY KEY, status TEXT NOT NULL, updated_at TEXT NOT NULL,
            draft_id TEXT, error TEXT);
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
            status TEXT NOT NULL, title TEXT NOT NULL, checked INTEGER NOT NULL DEFAULT 0,
            drafted INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '');
        CREATE INDEX IF NOT EXISTS runs_started ON runs(started_at DESC);
        CREATE TABLE IF NOT EXISTS drafts (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL, draft_id TEXT NOT NULL,
            original_id INTEGER NOT NULL, original_message_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'ready', updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS meeting_plans (
            draft_id TEXT PRIMARY KEY, plan TEXT NOT NULL, calendar_id TEXT,
            event_state TEXT NOT NULL DEFAULT 'planned', event_id TEXT);
        CREATE TABLE IF NOT EXISTS acknowledged_runs (run_id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS dismissed_runs (run_id TEXT PRIMARY KEY);
    """)
    try:
        office_store.prepare_schema(db)
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def installation_id():
    with connection() as db:
        db.execute("INSERT OR IGNORE INTO settings VALUES ('installation', ?)", (uuid.uuid4().hex[:16],))
        return db.execute("SELECT value FROM settings WHERE key='installation'").fetchone()[0]


def get():
    with connection() as db:
        row = db.execute("SELECT data FROM automations WHERE id=?", (AUTOMATION_ID,)).fetchone()
        return json.loads(row[0]) if row else {"id": AUTOMATION_ID, "status": "inactive", "enabled": False,
            "title": "Meeting reply drafts", "drafts_created": 0, "last_checked_at": None, "message": ""}


def update(**values):
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT data FROM automations WHERE id=?", (AUTOMATION_ID,)).fetchone()
        state = json.loads(row[0]) if row else {"id": AUTOMATION_ID, "status": "inactive", "enabled": False,
            "title": "Meeting reply drafts", "drafts_created": 0, "last_checked_at": None}
        state.update(values)
        db.execute("INSERT OR REPLACE INTO automations VALUES (?, ?)", (AUTOMATION_ID, json.dumps(state)))
        return state


def message_state(fingerprint):
    with connection() as db:
        row = db.execute("SELECT status FROM messages WHERE fingerprint=?", (fingerprint,)).fetchone()
        return row[0] if row else None


def record_message(fingerprint, status, draft_id=None):
    with connection() as db:
        db.execute("INSERT OR REPLACE INTO messages VALUES (?, ?, ?, ?, NULL)",
                   (fingerprint, status, now(), draft_id))


def recover_uncertain_drafts():
    with connection() as db:
        return db.execute("UPDATE messages SET status='uncertain', updated_at=? WHERE status='creating'",
                          (now(),)).rowcount


def record_draft(fingerprint, draft_id, *, run_id=None, original_id=None, original_message_id=None, meeting=None):
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT status FROM messages WHERE fingerprint=?", (fingerprint,)).fetchone()
        db.execute("INSERT OR REPLACE INTO messages VALUES (?, 'drafted', ?, ?, NULL)",
                   (fingerprint, now(), draft_id))
        if run_id and original_id and original_message_id:
            db.execute("INSERT OR IGNORE INTO drafts VALUES (?, ?, ?, ?, ?, 'ready', ?)",
                       (fingerprint, run_id, draft_id, original_id, original_message_id, now()))
            if meeting:
                db.execute('INSERT OR IGNORE INTO meeting_plans (draft_id, plan) VALUES (?, ?)',
                           (fingerprint, json.dumps(meeting)))
        row = db.execute("SELECT data FROM automations WHERE id=?", (AUTOMATION_ID,)).fetchone()
        if row and (not previous or previous[0] != "drafted"):
            state = json.loads(row[0])
            state["drafts_created"] = state.get("drafts_created", 0) + 1
            state["last_draft_at"] = now()
            db.execute("UPDATE automations SET data=? WHERE id=?", (json.dumps(state), AUTOMATION_ID))


def begin_run(title):
    run_id, started = uuid.uuid4().hex, now()
    with connection() as db:
        # Called with the worker's exclusive scan lock, so old running entries
        # belong to an interrupted worker, never a concurrent live scan.
        db.execute("UPDATE runs SET status='interrupted', finished_at=?, message='The background service restarted before this check finished.' WHERE status='running' AND NOT EXISTS (SELECT 1 FROM office_run_ids WHERE run_id=runs.id)", (started,))
        db.execute("INSERT INTO runs (id, started_at, status, title, message) VALUES (?, ?, 'running', ?, 'Checking Mail')", (run_id, started, title))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        db.execute(f"DELETE FROM runs WHERE started_at < ? AND status != 'running' AND {NO_PENDING_OFFICE_REVIEW}", (cutoff,))
        db.execute("DELETE FROM dismissed_runs WHERE NOT EXISTS (SELECT 1 FROM runs WHERE runs.id=dismissed_runs.run_id)")
    return run_id


def update_run(run_id, *, checked=0, drafted=0, message='', status='running'):
    with connection() as db:
        db.execute("UPDATE runs SET checked=?, drafted=?, message=?, status=?, finished_at=? WHERE id=?",
                   (checked, drafted, message, status, None if status == 'running' else now(), run_id))


def activity(before=None, limit=50, results_only=False):
    with connection() as db:
        db.row_factory = sqlite3.Row
        visible = "NOT EXISTS (SELECT 1 FROM dismissed_runs WHERE run_id=runs.id)"
        rows = db.execute(f"SELECT * FROM runs WHERE {visible} AND (? IS NULL OR started_at < ?) AND (? = 0 OR status IN ('failed', 'interrupted') OR drafted > 0) ORDER BY started_at DESC LIMIT ?", (before, before, results_only, limit + 1)).fetchall()
        seen = db.execute("SELECT value FROM settings WHERE key='activity_seen_at'").fetchone()
        seen_at = seen['value'] if seen else ''
        unread = db.execute(f"SELECT count(*) FROM runs WHERE {visible} AND finished_at > ? AND (status IN ('failed', 'interrupted') OR drafted > 0) AND NOT EXISTS (SELECT 1 FROM acknowledged_runs WHERE run_id=runs.id)", (seen_at,)).fetchone()[0]
        clearable = db.execute(f"SELECT count(*) FROM runs WHERE {visible} AND finished_at IS NOT NULL AND status != 'running' AND {NO_PENDING_OFFICE_REVIEW}").fetchone()[0]
        cleared = db.execute("SELECT value FROM settings WHERE key='activity_cleared_at'").fetchone()
        items = [dict(row) for row in rows[:limit]]
        for item in items:
            acknowledged = db.execute('SELECT 1 FROM acknowledged_runs WHERE run_id=?', (item['id'],)).fetchone()
            item['unread'] = bool(not acknowledged and item['finished_at'] and item['finished_at'] > seen_at and (item['status'] in ('failed', 'interrupted') or item['drafted'] > 0))
            item['drafts'] = [dict(row) for row in db.execute("""SELECT d.id,
                CASE WHEN d.status='sent' AND p.calendar_id IS NOT NULL AND p.event_state!='created'
                     THEN 'calendar_attention' ELSE d.status END AS status
                FROM drafts d LEFT JOIN meeting_plans p ON p.draft_id=d.id WHERE d.run_id=?""", (item['id'],))]
            kind = office_store.run_kind(db, item['id'])
            if kind:
                item['workflow_kind'] = kind
                item['workflow_drafts'] = office_store.activity_drafts(db, item['id'])
        return {'items': items, 'unread_count': unread, 'clearable_count': clearable,
                'cleared_at': cleared['value'] if cleared else None,
                'next_before': items[-1]['started_at'] if len(rows) > limit else None}


def mark_activity_read():
    with connection() as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES ('activity_seen_at', ?)", (now(),))


def clear_activity():
    """Dismiss finished notifications without touching runs, drafts, or live checks."""
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        cleared = db.execute(f"""INSERT OR IGNORE INTO dismissed_runs
            SELECT id FROM runs WHERE finished_at IS NOT NULL AND status != 'running' AND {NO_PENDING_OFFICE_REVIEW}""").rowcount
        cleared_at = now()
        db.execute("INSERT OR REPLACE INTO settings VALUES ('activity_cleared_at', ?)", (cleared_at,))
        return {'cleared': cleared, 'cleared_at': cleared_at}


def get_draft(draft_id):
    with connection() as db:
        db.row_factory = sqlite3.Row
        row = db.execute('''SELECT d.*, p.plan, p.calendar_id, p.event_state, p.event_id
                            FROM drafts d LEFT JOIN meeting_plans p ON d.id=p.draft_id WHERE d.id=?''', (draft_id,)).fetchone()
        if not row:
            return None
        record = dict(row)
        record['meeting'] = json.loads(record.pop('plan') or 'null')
        return record


def claim_delivery(draft_id, calendar_id):
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        claimed = db.execute("UPDATE drafts SET status='sending', updated_at=? WHERE id=? AND status='ready'",
                             (now(), draft_id)).rowcount
        if claimed:
            db.execute("UPDATE meeting_plans SET calendar_id=?, event_state='pending' WHERE draft_id=?",
                       (calendar_id, draft_id))
        return bool(claimed)


def calendar_status(draft_id, state, *, expected, event_id=None):
    with connection() as db:
        changed = db.execute('UPDATE meeting_plans SET event_state=?, event_id=? WHERE draft_id=? AND event_state=?',
                             (state, event_id, draft_id, expected)).rowcount == 1
        if changed and state == 'created':
            acknowledge_delivery(db, draft_id)
        return changed


def acknowledge_delivery(db, draft_id):
    db.execute("""INSERT OR IGNORE INTO acknowledged_runs SELECT run_id FROM drafts WHERE id=?
        AND NOT EXISTS (SELECT 1 FROM drafts pending LEFT JOIN meeting_plans p ON p.draft_id=pending.id
            WHERE pending.run_id=drafts.run_id AND (pending.status!='sent' OR (p.calendar_id IS NOT NULL AND p.event_state!='created')))
        AND EXISTS (SELECT 1 FROM runs WHERE runs.id=drafts.run_id AND runs.status='completed')""", (draft_id,))


def draft_status(draft_id, status, *, expected=None):
    with connection() as db:
        changed = db.execute('UPDATE drafts SET status=?, updated_at=? WHERE id=? AND (? IS NULL OR status=?)',
                             (status, now(), draft_id, expected, expected)).rowcount == 1
        if changed and status == 'sent':
            # Clear only this result, preserving unread failures and other drafts.
            acknowledge_delivery(db, draft_id)
        return changed


def recover_pending_sends():
    with connection() as db:
        db.execute("UPDATE drafts SET status='send_uncertain', updated_at=? WHERE status='sending'", (now(),))
        db.execute("UPDATE meeting_plans SET event_state='uncertain' WHERE event_state='creating'")
