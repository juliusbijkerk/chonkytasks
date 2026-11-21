"""The local approval ledger and approved artifact library."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import PurePosixPath
import sqlite3
import stat
import uuid

from . import automation_store as storage
from .office_recipes import RECIPES

REVIEW_FIELDS = """id, kind, source_key, source_name, source_preview, relative_path,
    mime_type, preview, content, status, revision, approved_at, run_id"""
PUBLIC_REVIEW_FIELDS = """id, kind, source_name, source_preview, relative_path,
    mime_type, preview, status, revision, approved_at"""


def prepare_schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS office_workflows (
            kind TEXT PRIMARY KEY, source_job_id TEXT NOT NULL, config TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'paused',
            generation INTEGER NOT NULL DEFAULT 1, activated_at TEXT,
            scan_from REAL, checked_at TEXT, message TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS office_run_ids (run_id TEXT PRIMARY KEY, kind TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS office_items (
            kind TEXT NOT NULL, source_key TEXT NOT NULL, status TEXT NOT NULL,
            PRIMARY KEY (kind, source_key));
        CREATE TABLE IF NOT EXISTS office_reviews (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, source_key TEXT NOT NULL, run_id TEXT NOT NULL,
            source_name TEXT NOT NULL, source_preview TEXT NOT NULL, relative_path TEXT NOT NULL,
            mime_type TEXT NOT NULL, preview TEXT NOT NULL, content BLOB NOT NULL,
            status TEXT NOT NULL DEFAULT 'ready', revision TEXT NOT NULL, approved_at TEXT,
            UNIQUE (kind, source_key));
        CREATE INDEX IF NOT EXISTS office_reviews_run ON office_reviews(run_id, status);
    """)


def _workflow(kind):
    if kind not in RECIPES:
        raise ValueError('This workflow is unavailable.')
    recipe = RECIPES[kind]
    return recipe['title'], recipe['source']


def _config(kind, config):
    _, source = _workflow(kind)
    if not isinstance(config, dict):
        raise ValueError('Provide the workflow settings.')
    if source == 'mail':
        subject = _setting(config, 'subject_prefix', 100)
        if not subject:
            raise ValueError('Enter the beginning of the email subject to match.')
        sender = _setting(config, 'sender', 254)
        if sender and (sender.count('@') != 1 or not all(sender.split('@')) or
                       any(c.isspace() or c in '<>(),;:"' for c in sender)):
            raise ValueError('Enter a single sender email address.')
        return {'subject_prefix': subject, 'sender': sender}
    if kind == 'spreadsheetReport':
        return {'status_filter': _setting(config, 'status_filter', 100)}
    return {}


def _setting(config, key, max_length):
    value = config.get(key, '')
    if not isinstance(value, str) or len(value) > max_length or any(ord(c) < 32 for c in value):
        raise ValueError('Check the length and characters in your workflow settings.')
    return value.strip()


def _state(row):
    if row is None:
        return None
    result = dict(row)
    result['title'], result['source'] = _workflow(result['kind'])
    result['config'] = json.loads(result['config'])
    result['enabled'] = bool(result['enabled'])
    if result['source'] == 'files':
        from .office_automation import inbox_path
        result['input_folder'] = str(inbox_path(result['kind']))
    return result


def _get(db, kind):
    db.row_factory = sqlite3.Row
    return _state(db.execute('SELECT * FROM office_workflows WHERE kind=?', (kind,)).fetchone())


def get(kind):
    with storage.connection() as db:
        return _get(db, kind)


def states():
    with storage.connection() as db:
        db.row_factory = sqlite3.Row
        return [_state(row) for row in db.execute('SELECT * FROM office_workflows ORDER BY kind')]


def configure(kind, source_job_id, config):
    normalized = _config(kind, config)
    if not isinstance(source_job_id, str) or not source_job_id:
        raise ValueError('Choose a workflow from an analyzed recording.')
    if _workflow(kind)[1] == 'files':
        from .office_automation import ensure_inbox
        ensure_inbox(kind)
    with storage.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT config FROM office_workflows WHERE kind=?', (kind,)).fetchone()
        config_json = json.dumps(normalized, sort_keys=True)
        db.execute("""
            INSERT INTO office_workflows(kind, source_job_id, config) VALUES (?, ?, ?)
            ON CONFLICT(kind) DO UPDATE SET source_job_id=excluded.source_job_id,
                config=excluded.config, generation=office_workflows.generation+1
            WHERE source_job_id!=excluded.source_job_id OR config!=excluded.config
        """, (kind, source_job_id, config_json))
        if old is not None and old[0] != config_json:
            db.execute("DELETE FROM office_items WHERE kind=? AND status!='queued'", (kind,))
        return _get(db, kind)


def set_status(kind, status, enabled, message=''):
    now = storage.now()
    with storage.connection() as db:
        changed = db.execute("""
            UPDATE office_workflows SET status=?, enabled=?, message=?, generation=generation+1,
                activated_at=CASE WHEN ? THEN COALESCE(activated_at, ?) ELSE activated_at END
            WHERE kind=?
        """, (status, enabled, message, enabled, now, kind)).rowcount
        if not changed:
            raise ValueError('Set up this workflow first.')
        return _get(db, kind)


def set_enabled(kind, enabled):
    return set_status(kind, 'active' if enabled else 'paused', enabled)


def finish_run(state, run_id, counts, *, status, message, detail, scan_from=None):
    finished_at = storage.now()
    with storage.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("""
            UPDATE office_workflows SET checked_at=?, message=?, scan_from=COALESCE(?, scan_from)
            WHERE kind=? AND generation=?
        """, (finished_at, detail, scan_from, state['kind'], state['generation']))
        db.execute("""
            UPDATE runs SET checked=?, drafted=?, message=?, status=?, finished_at=? WHERE id=?
        """, (counts['checked'], counts['drafted'], message, status, finished_at, run_id))
        if counts['drafted']:
            _acknowledge_reviewed_run(db, run_id)


def _same_configuration(db, state):
    return db.execute("""
        SELECT 1 FROM office_workflows WHERE kind=? AND generation=? AND enabled=1
    """, (state['kind'], state['generation'])).fetchone() is not None


def is_current(state):
    with storage.connection() as db:
        return _same_configuration(db, state)


def begin_run(state):
    run_id = uuid.uuid4().hex
    started = storage.now()
    kind = state['kind']
    context = 'Checking Mail' if state['source'] == 'mail' else 'Checking the workflow folder'
    with storage.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        # The runner holds its cross-process lock. An older office run marked
        # running can only have been interrupted, including after queueing items.
        db.execute("""
            UPDATE runs SET status='interrupted', finished_at=?,
                drafted=(SELECT count(*) FROM office_reviews WHERE run_id=runs.id),
                message='The background service restarted before this check finished.'
            WHERE status='running' AND id IN (SELECT run_id FROM office_run_ids WHERE kind=?)
        """, (started, kind))
        db.execute("""INSERT INTO runs(id, started_at, status, title, message)
                      VALUES (?, ?, 'running', ?, ?)""", (run_id, started, state['title'], context))
        db.execute('INSERT INTO office_run_ids VALUES (?, ?)', (run_id, kind))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        db.execute("""
            DELETE FROM runs WHERE started_at<? AND status!='running'
            AND EXISTS (SELECT 1 FROM office_run_ids WHERE run_id=runs.id)
            AND NOT EXISTS (SELECT 1 FROM office_reviews WHERE run_id=runs.id AND status='ready')
        """, (cutoff,))
        for table in ('dismissed_runs', 'acknowledged_runs'):
            db.execute(f'DELETE FROM {table} WHERE NOT EXISTS (SELECT 1 FROM runs WHERE id={table}.run_id)')
        db.execute('DELETE FROM office_run_ids WHERE NOT EXISTS (SELECT 1 FROM runs WHERE id=office_run_ids.run_id)')
    return run_id


def run_kind(db, run_id):
    row = db.execute('SELECT kind FROM office_run_ids WHERE run_id=?', (run_id,)).fetchone()
    return row[0] if row is not None else None


def activity_drafts(db, run_id):
    rows = db.execute('SELECT id, kind, status FROM office_reviews WHERE run_id=? ORDER BY rowid', (run_id,))
    return [{'id': row[0], 'kind': row[1], 'status': row[2]} for row in rows]


def seen(kind, source_key):
    with storage.connection() as db:
        return db.execute('SELECT 1 FROM office_items WHERE kind=? AND source_key=?', (kind, source_key)).fetchone() is not None


def record_skip(state, source_key, status):
    with storage.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        if _same_configuration(db, state):
            db.execute('INSERT OR IGNORE INTO office_items VALUES (?, ?, ?)', (state['kind'], source_key, status))


def _revision(values):
    metadata = [values[key] for key in (
        'id', 'kind', 'source_key', 'source_name', 'source_preview', 'relative_path', 'mime_type', 'preview')]
    metadata.append(hashlib.sha256(values['content']).hexdigest())
    return hashlib.sha256(json.dumps(metadata, ensure_ascii=True).encode()).hexdigest()


def _safe_relative(path):
    relative = PurePosixPath(path)
    if (relative.is_absolute() or not relative.name or '..' in relative.parts or '\\' in path
            or any(ord(c) < 32 for c in path)):
        raise ValueError('The proposed artifact has an invalid file name.')
    return relative


def queue(state, run_id, source_key, source_name, source_preview, output):
    relative = _safe_relative(output.relative_path)
    row = {'id': uuid.uuid4().hex, 'kind': state['kind'], 'source_key': source_key,
           'run_id': run_id, 'source_name': source_name, 'source_preview': source_preview,
           'relative_path': str(relative), 'mime_type': output.mime_type,
           'preview': output.preview, 'content': output.content}
    row['revision'] = _revision(row)
    with storage.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        if not _same_configuration(db, state):
            return False
        created = db.execute("INSERT OR IGNORE INTO office_items VALUES (?, ?, 'queued')",
                             (state['kind'], source_key)).rowcount
        if created:
            db.execute("""
                INSERT INTO office_reviews (
                    id, kind, source_key, run_id, source_name, source_preview, relative_path,
                    mime_type, preview, content, revision
                ) VALUES (
                    :id, :kind, :source_key, :run_id, :source_name, :source_preview, :relative_path,
                    :mime_type, :preview, :content, :revision
                )
            """, row)
        return bool(created)


def _public(row):
    if row is None:
        return None
    result = dict(row)
    result['target_path'] = result['relative_path']
    if result['status'] == 'saved':
        result['output_path'] = str(_artifact_path(row))
    return result


def _artifact_path(row):
    relative = _safe_relative(row['relative_path'])
    return storage.DATABASE.parent / 'workflow-library' / row['kind'] / relative.parent / row['id'] / relative.name


@contextmanager
def child_directory(root, parts, *, create=False):
    """Hold a directory descriptor while staying inside app-owned folders."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(root, flags)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                else:
                    os.fsync(descriptor)
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _verify_artifact(directory, name, expected):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != len(expected) or stream.read(len(expected) + 1) != expected:
            raise ValueError('An existing file has different contents. Move it out of the workflow library and retry.')
        after = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (info.st_ino, info.st_dev, info.st_ctime_ns) != (after.st_ino, after.st_dev, after.st_ctime_ns):
            raise ValueError('The library result changed while it was being saved. Try the approval again.')


def _save_artifact(row):
    path = _artifact_path(row)
    parent, name = path.parent.relative_to(storage.DATABASE.parent), path.name
    try:
        with child_directory(storage.DATABASE.parent, parent.parts, create=True) as directory:
            try:
                _verify_artifact(directory, name, row['content'])
                os.fsync(directory)
                return
            except FileNotFoundError:
                pass
            temporary = '.' + uuid.uuid4().hex + '.tmp'
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(row['content'])
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                except FileExistsError:
                    pass
            finally:
                os.unlink(temporary, dir_fd=directory)
            # A retry sees the same approved bytes at the same path if the app
            # stopped after linking the complete file but before committing SQLite.
            _verify_artifact(directory, name, row['content'])
            os.fsync(directory)
    except OSError:
        raise ValueError('The workflow library could not save this result. Check access to the folder and retry.') from None


def preview(review_id):
    with storage.connection() as db:
        db.row_factory = sqlite3.Row
        return _public(db.execute(f'SELECT {PUBLIC_REVIEW_FIELDS} FROM office_reviews WHERE id=?', (review_id,)).fetchone())


def _acknowledge_reviewed_run(db, run_id):
    db.execute("""
        INSERT OR IGNORE INTO acknowledged_runs
        SELECT id FROM runs WHERE id=? AND status='completed' AND NOT EXISTS (
            SELECT 1 FROM office_reviews WHERE run_id=runs.id AND status='ready')
    """, (run_id,))


def approve(review_id, revision):
    with storage.connection() as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN IMMEDIATE')
        row = db.execute(f'SELECT {REVIEW_FIELDS} FROM office_reviews WHERE id=?', (review_id,)).fetchone()
        if row is None:
            raise ValueError('This proposal is unavailable.')
        if (not isinstance(revision, str) or not revision.isascii() or
                not hmac.compare_digest(_revision(row), revision) or revision != row['revision']):
            raise ValueError('The proposal has changed. Review it again before approving.')
        _save_artifact(row)
        if row['status'] == 'ready':
            db.execute("UPDATE office_reviews SET status='saved', approved_at=? WHERE id=?",
                       (storage.now(), review_id))
            _acknowledge_reviewed_run(db, row['run_id'])
        return _public(db.execute(f'SELECT {PUBLIC_REVIEW_FIELDS} FROM office_reviews WHERE id=?', (review_id,)).fetchone())


def output(review_id):
    with storage.connection() as db:
        db.row_factory = sqlite3.Row
        row = db.execute(f"SELECT {REVIEW_FIELDS} FROM office_reviews WHERE id=? AND status='saved'", (review_id,)).fetchone()
        if row is None or _revision(row) != row['revision']:
            return None
        result = {key: row[key] for key in ('content', 'relative_path', 'mime_type', 'revision')}
        result['output_path'] = str(_artifact_path(row))
        return result
