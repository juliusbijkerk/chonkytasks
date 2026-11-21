"""Check local inputs when the existing scheduler ticks and enqueue local proposals."""
from datetime import datetime
from email.utils import parseaddr
import fcntl
import hashlib
import math
import os
from pathlib import Path
import stat
from time import monotonic

from . import automation_store as storage, mac_automation as mac, office_recipes as recipes, office_store as office

MAX_CHECKS = 20
MAX_SOURCE_PREVIEW = 16000
MAIL_TICK_SECONDS = 90


def inbox_path(kind):
    if kind not in recipes.RECIPES or recipes.RECIPES[kind]['source'] != 'files':
        raise ValueError('This workflow has no input folder.')
    return storage.DATABASE.parent / 'workflow-inbox' / kind


def ensure_inbox(kind):
    folder = inbox_path(kind)
    folder.parent.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with office.child_directory(folder.parent.parent, (folder.parent.name, folder.name), create=True):
            pass
    except OSError:
        raise ValueError('Use an ordinary folder for the workflow inbox and try again.') from None
    return folder


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _read_snapshot(directory, name):
    """Read a bounded regular file and ensure the name still points to that version."""
    descriptor = None
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, 'rb') as stream:
            descriptor = None
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= recipes.MAX_INPUT_BYTES:
                return None
            data = stream.read(recipes.MAX_INPUT_BYTES + 1)
            after = os.fstat(stream.fileno())
            linked = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if len(data) == before.st_size and _stamp(before) == _stamp(after) == _stamp(linked):
                return data
    except (FileNotFoundError, IsADirectoryError):
        return None
    except OSError:
        # Symlinks, renamed files and transient I/O can be skipped without
        # following the path or stopping other inputs from being checked.
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return None


def _source_key(*parts):
    return hashlib.sha256('\0'.join(parts).encode()).hexdigest()


def _excerpt(source):
    if len(source) <= MAX_SOURCE_PREVIEW:
        return source
    return source[:MAX_SOURCE_PREVIEW] + '\n\n[Source excerpt ends here]'


def _check_files(state, run_id, counts):
    kind = state['kind']
    folder = ensure_inbox(kind)
    with office.child_directory(folder.parent.parent, (folder.parent.name, folder.name)) as base:
        with os.scandir(base) as entries:
            names = sorted(entry.name for entry in entries if entry.is_file(follow_symlinks=False))
        for name in names:
            if counts['checked'] >= MAX_CHECKS or not office.is_current(state):
                break
            if (name.startswith('.') or '\\' in name or any(ord(c) < 32 for c in name) or
                    Path(name).suffix.lower() not in recipes.RECIPES[kind]['extensions']):
                continue
            data = _read_snapshot(base, name)
            if data is None:
                continue
            source_key = _source_key(name, hashlib.sha256(data).hexdigest())
            if office.seen(kind, source_key):
                continue
            counts['checked'] += 1
            try:
                output = recipes.prepare(kind, data, name, status_filter=state['config'].get('status_filter', ''))
            except ValueError:
                office.record_skip(state, source_key, 'rejected')
                counts['rejected'] += 1
                continue
            source = _excerpt(data.decode('utf-8-sig'))
            if office.queue(state, run_id, source_key, name, source, output):
                counts['drafted'] += 1


def _mail_items(bridge_response):
    eligible = []
    for item in bridge_response['messages']:
        received = item.get('received_at')
        if isinstance(received, (float, int)) and math.isfinite(received):
            eligible.append(item)
    return sorted(eligible, key=lambda item: item['received_at'])


def _subject_matches(subject, prefix):
    return isinstance(subject, str) and subject.strip().casefold().startswith(prefix.casefold())


async def _check_mail(state, run_id, counts):
    kind = state['kind']
    watch_from = math.floor(datetime.fromisoformat(state['activated_at']).timestamp())
    since = max(watch_from, state['scan_from'] or watch_from)
    config = state['config']
    deadline = monotonic() + MAIL_TICK_SECONDS
    items = _mail_items(await mac.call('inbox', since=since - 1.0))
    latest, retry_from = since, None
    for item in items:
        if counts['checked'] >= MAX_CHECKS or monotonic() >= deadline or not office.is_current(state):
            break
        received_at = item['received_at']
        if received_at < since:
            continue
        latest = max(latest, received_at)
        identity = item.get('message_id')
        identifier = item.get('id')
        if not isinstance(identity, str) or not identity.strip() or type(identifier) is not int or identifier <= 0:
            continue
        if not _subject_matches(item.get('subject'), config['subject_prefix']):
            continue
        source_key = _source_key(identity)
        if office.seen(kind, source_key):
            continue
        counts['checked'] += 1
        message = await mac.call('message', id=identifier, full_body=True)
        # Mail's numeric identifier can point at a different inbox item after the
        # list call. Only prepare output from the exact message that was listed.
        if message.get('message_id') != identity:
            retry_from = received_at if retry_from is None else min(retry_from, received_at)
            continue
        if not _subject_matches(message.get('subject'), config['subject_prefix']):
            continue
        sender = message.get('sender', '')
        if config['sender'] and parseaddr(sender)[1].casefold() != config['sender'].casefold():
            office.record_skip(state, source_key, 'unmatched')
            continue
        subject, body = message['subject'], message.get('body', '')
        try:
            if message.get('body_truncated') or ('body_truncated' not in message and isinstance(body, str) and len(body) >= 12000):
                raise ValueError('The full email body could not be read.')
            output = recipes.prepare(kind, b'', 'mail', mail=message)
        except ValueError:
            office.record_skip(state, source_key, 'rejected')
            counts['rejected'] += 1
            continue
        source = _excerpt(f'From: {sender}\nSubject: {subject}\n\n{body}')
        if office.queue(state, run_id, source_key, subject, source, output):
            counts['drafted'] += 1
    # Revisit two seconds around the watermark so equal receipt times remain
    # eligible after a batch stops. The ledger excludes prior results.
    if retry_from is not None:
        latest = min(latest, retry_from)
    return max(since, latest - 2)


def _completed_message(counts):
    if counts['rejected']:
        label = 'item needs' if counts['rejected'] == 1 else 'items need'
        return f"{counts['rejected']} {label} a supported source format."
    if counts['drafted']:
        return 'Results are ready to review.'
    return 'Check completed.'


async def _scan(state):
    run_id = office.begin_run(state)
    counts = {'checked': 0, 'drafted': 0, 'rejected': 0}
    scan_from = None
    try:
        if state['source'] == 'mail':
            scan_from = await _check_mail(state, run_id, counts)
        else:
            _check_files(state, run_id, counts)
        message = _completed_message(counts)
        status = 'failed' if counts['rejected'] and not counts['drafted'] else 'completed'
        detail = message if counts['rejected'] else ''
    except Exception as error:
        message = str(error) if isinstance(error, mac.MacAutomationError) else (
            'The Inbox could not be checked. Open Mail and check its connection.' if state['source'] == 'mail'
            else 'The workflow folder could not be checked. Open the folder and try again.')
        detail, status = message, 'failed'
    office.finish_run(state, run_id, counts, status=status, message=message, detail=detail, scan_from=scan_from)
    return {**counts, 'status': status}


async def scan_enabled():
    """Scan enabled recipes from the shared worker tick, with no external writes."""
    lock_path = storage.DATABASE.with_suffix('.office-scan.lock')
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with os.fdopen(os.open(lock_path, os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600), 'rb') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {}
        results = {}
        for state in office.states():
            if state['enabled']:
                results[state['kind']] = await _scan(state)
        return results
