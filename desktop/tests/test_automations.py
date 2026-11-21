import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from app import automations as service, automation_store as storage, mac_automation as mac, main
from app import meeting_automation as recipe
from app import draft_review


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'DATABASE', tmp_path / 'automations.sqlite3')
    monkeypatch.setattr(service, 'LOCK', asyncio.Lock())
    monkeypatch.setattr(service, 'REMOTE_CACHE', None)
    monkeypatch.setattr(main, 'DATA', tmp_path / 'jobs')
    main.DATA.mkdir()
    monkeypatch.setattr(main, 'JOBS', {})
    monkeypatch.delenv('MISTRAL_API_KEY', raising=False)


def job():
    return {'id': 'a' * 32, 'status': 'completed', 'report': {'recommendations': [
        {'title': 'Draft meeting replies from calendar availability'}]}}


def mock_ready(monkeypatch):
    async def configured(): return True
    async def call(operation, **values):
        assert operation == 'status'
        return {'mail': True, 'calendar': True, 'timezone': 'Europe/London'}
    async def started(): return datetime.now(timezone.utc)
    async def worker_ready(started_at): return None
    monkeypatch.setattr(mac, 'key_configured', configured)
    monkeypatch.setattr(mac, 'call', call)
    monkeypatch.setattr(service, 'start_worker', started)
    monkeypatch.setattr(service, 'wait_for_worker', worker_ready)


def test_activation_requires_confirmed_worker_and_schedule_and_is_idempotent(monkeypatch):
    mock_ready(monkeypatch)
    calls = []
    remote_schedule = None
    async def remote(method, path, **kwargs):
        nonlocal remote_schedule
        calls.append((method, path))
        if path.endswith('whoami'): return {'namespace': 'test'}
        if method == 'POST' and path == '/workflows/schedules':
            body = kwargs['json']
            assert body['deployment_name'] == service.deployment_name()
            assert body['schedule']['input'] == {'automation_id': storage.AUTOMATION_ID}
            assert body['schedule']['intervals'] == [{'every': 'PT30S'}]
            assert body['schedule']['policy']['overlap'] == 1
            assert not storage.get()['enabled']
            remote_schedule = {'workflow_name': service.WORKFLOW_NAME, 'deployment_name': service.deployment_name(), 'paused': False, 'future_executions': None}
            return {'schedule_id': service.schedule_id()}
        return remote_schedule
    monkeypatch.setattr(service, 'remote', remote)
    async def run():
        results = await asyncio.gather(service.activate(job(), 0), service.activate(job(), 0))
        assert all(r['enabled'] and r['status'] == 'active' for r in results)
        assert datetime.fromisoformat(results[0]['watch_from']) > datetime.now(timezone.utc) - timedelta(seconds=2)
    asyncio.run(run())
    assert calls.count(('POST', '/workflows/schedules')) == 1


def test_ambiguous_schedule_creation_reconciles_instead_of_creating_duplicate(monkeypatch):
    mock_ready(monkeypatch)
    exists = False
    posts = 0
    async def remote(method, path, **kwargs):
        nonlocal exists, posts
        if path.endswith('whoami'): return {}
        if method == 'POST':
            posts += 1
            exists = True
            raise ValueError('Connection dropped after creation')
        return {'workflow_name': service.WORKFLOW_NAME, 'deployment_name': service.deployment_name(), 'paused': False} if exists else None
    monkeypatch.setattr(service, 'remote', remote)
    with pytest.raises(ValueError): asyncio.run(service.activate(job(), 0))
    assert storage.get()['status'] == 'error' and not storage.get()['enabled']
    result = asyncio.run(service.activate(job(), 0))
    assert result['status'] == 'active' and posts == 1


def test_worker_failure_never_creates_or_marks_active_schedule(monkeypatch):
    mock_ready(monkeypatch)
    async def failure(started_at): raise ValueError('Worker not connected')
    async def remote(method, path, **kwargs):
        assert path.endswith('whoami')
        return {}
    monkeypatch.setattr(service, 'wait_for_worker', failure)
    monkeypatch.setattr(service, 'remote', remote)
    with pytest.raises(ValueError, match='Worker not connected'):
        asyncio.run(service.activate(job(), 0))
    assert not storage.get()['enabled']


def test_resume_catches_pending_message_and_keeps_checkpoint(monkeypatch):
    mock_ready(monkeypatch)
    start = datetime.now(timezone.utc) - timedelta(minutes=5)
    cursor = start + timedelta(minutes=1)
    arrived = start + timedelta(minutes=2)
    storage.update(enabled=False, status='paused', watch_from=start.isoformat(),
                   scan_from=cursor.isoformat(), activated_at=start.isoformat())
    paused = True
    async def remote(method, path, **kwargs):
        nonlocal paused
        if path.endswith('/resume'): paused = False
        return {'workflow_name': service.WORKFLOW_NAME, 'deployment_name': service.deployment_name(), 'paused': paused}
    monkeypatch.setattr(service, 'remote', remote)
    result = asyncio.run(service.activate(job(), 0))
    assert result['watch_from'] == start.isoformat()
    assert result['scan_from'] == cursor.isoformat()
    assert result['activated_at'] == start.isoformat()
    writes = []
    async def native(operation, **values):
        if operation == 'status': return {'mail': True, 'calendar': True, 'timezone': 'Europe/London'}
        if operation == 'inbox':
            return {'messages': [{'id': 7, 'message_id': 'pending-meeting', 'received_at': arrived.timestamp()}]
                    if arrived.timestamp() > values['since'] else []}
        if operation == 'message': return {'message_id': 'pending-meeting', 'subject': 'Meeting request', 'body': 'Can we meet?'}
        if operation == 'busy_times': return {'busy': [], 'timezone': 'Europe/London'}
        if operation == 'draft':
            writes.append(values['id'])
            return {'draft_id': '99'}
        raise AssertionError(operation)
    async def understand(*args): return recipe.MeetingRequest(is_meeting_request=True)
    monkeypatch.setattr(mac, 'call', native)
    monkeypatch.setattr(recipe, 'understand', understand)
    assert asyncio.run(recipe.scan_inbox())['drafted'] == 1
    assert asyncio.run(recipe.scan_inbox())['drafted'] == 0
    assert writes == [7]


def test_failed_first_setup_preserves_starting_boundary_on_retry(monkeypatch):
    mock_ready(monkeypatch)
    async def remote(method, path, **kwargs):
        if storage.get()['status'] == 'activating':
            assert storage.get()['watch_from']
        raise ValueError('Disconnected during setup')
    monkeypatch.setattr(service, 'remote', remote)
    with pytest.raises(ValueError): asyncio.run(service.activate(job(), 0))
    boundary = storage.get()['watch_from']
    async def connected(method, path, **kwargs):
        return {'workflow_name': service.WORKFLOW_NAME, 'deployment_name': service.deployment_name(), 'paused': False}
    monkeypatch.setattr(service, 'remote', connected)
    assert asyncio.run(service.activate(job(), 0))['watch_from'] == boundary


def test_pause_stops_local_processing_even_when_remote_is_unreachable(monkeypatch):
    storage.update(enabled=True, status='active')
    async def remote(*args, **kwargs): raise ValueError('Offline')
    monkeypatch.setattr(service, 'remote', remote)
    assert asyncio.run(service.pause())['status'] == 'paused_local'
    assert asyncio.run(recipe.scan_inbox())['status'] == 'paused'


def test_status_does_not_claim_active_when_worker_or_schedule_is_unavailable(monkeypatch):
    storage.update(enabled=True, status='active')
    async def remote(method, path, **kwargs):
        if '/deployments/' in path: return {'is_active': False}
        return {'paused': False, 'workflow_name': service.WORKFLOW_NAME, 'deployment_name': service.deployment_name()}
    monkeypatch.setattr(service, 'remote', remote)
    assert asyncio.run(service.status())['status'] == 'offline'
    service.clear_cache()
    async def paused(method, path, **kwargs):
        if '/deployments/' in path: return {'is_active': True}
        return {'paused': True, 'workflow_name': service.WORKFLOW_NAME, 'deployment_name': service.deployment_name()}
    monkeypatch.setattr(service, 'remote', paused)
    assert asyncio.run(service.status())['status'] == 'paused'
    assert not storage.get()['enabled']


def test_connection_validates_workflows_access_before_storing_key(monkeypatch):
    saved = []
    async def remote(method, path, **kwargs):
        assert path == '/workflows/workers/whoami'
        assert kwargs['key'] == 'private-test-key'
        raise ValueError('Workflows not enabled')
    async def native(*args, **kwargs): saved.append(kwargs)
    monkeypatch.setattr(service, 'remote', remote)
    monkeypatch.setattr(mac, 'call', native)
    with TestClient(main.app) as client:
        result = client.post('/api/connections/mistral', json={'api_key': 'private-test-key'})
        assert result.status_code == 400 and 'private-test-key' not in result.text
        assert saved == []
        assert client.post('/api/connections/mistral', json={'api_key': 'secret'}, headers={'Origin': 'https://elsewhere.example'}).status_code == 403
        async def success(*args, **kwargs): return {}
        monkeypatch.setattr(service, 'remote', success)
        assert client.post('/api/connections/mistral', json={'api_key': 'private-test-key'}).json() == {'connected': True}
    assert saved == [{'value': 'private-test-key'}]
    assert 'private-test-key' not in storage.DATABASE.read_bytes().decode('utf-8', errors='ignore')


def test_missing_connections_and_unsupported_suggestions_cannot_activate(monkeypatch):
    record = job()
    main.JOBS[record['id']] = record
    async def settings(): return {'connected': False, 'mail': False, 'calendar': False, 'runtime_ready': True}
    monkeypatch.setattr(service, 'settings', settings)
    with TestClient(main.app) as client:
        body = {'job_id': record['id'], 'recommendation_index': 0}
        assert client.post('/api/automations/activate', json=body).status_code == 409
        assert not storage.get()['enabled']
        record['report']['recommendations'][0]['title'] = 'Update a spreadsheet'
        assert client.post('/api/automations/activate', json=body).status_code == 422
        assert client.post('/api/automations/activate', json=body, headers={'Origin': 'https://elsewhere.example'}).status_code == 403


def test_resume_does_not_depend_on_recording_still_existing(monkeypatch):
    storage.update(source_job_id='b' * 32, source_recommendation={'title': 'Meeting drafts', 'automation_recipe': 'meeting_reply_drafts'}, status='paused')
    async def settings(): return dict.fromkeys(['connected', 'mail', 'calendar', 'runtime_ready'], True)
    async def activate(record, index):
        assert record['id'] not in main.JOBS
        assert service.supported(record['report']['recommendations'][index])
        return storage.update(enabled=True, status='active')
    monkeypatch.setattr(service, 'settings', settings)
    monkeypatch.setattr(service, 'activate', activate)
    with TestClient(main.app) as client:
        assert client.post('/api/automations/resume').status_code == 202
    assert storage.get()['status'] == 'active'


def test_calendar_selection_excludes_conflicts_past_times_and_weekends():
    current = datetime(2026, 11, 23, 8, tzinfo=timezone.utc)  # Monday
    request = recipe.MeetingRequest(is_meeting_request=True, requested_start='2026-11-23T09:00:00+00:00', duration_minutes=60)
    busy = [{'start': datetime(2026, 11, 23, 9, tzinfo=timezone.utc).timestamp(),
             'end': datetime(2026, 11, 23, 10, tzinfo=timezone.utc).timestamp()}]
    slot = recipe.choose_slot(request, busy, current, 'Europe/London')
    assert slot == datetime(2026, 11, 23, 10, tzinfo=timezone.utc)
    request.requested_start = '2026-11-22T09:00:00+00:00'
    assert recipe.choose_slot(request, [], current, 'Europe/London') is None
    request.requested_start = '2026-11-24T09:00:00'  # No offset: don't invent one.
    assert recipe.choose_slot(request, [], current, 'Europe/London') is None
    request.requested_start = '2026-11-28T10:00:00+00:00'  # Saturday
    assert recipe.choose_slot(request, [], current, 'Europe/London').weekday() < 5


def test_slot_comparison_handles_timezone_offsets_and_boundary_adjacency():
    current = datetime(2026, 11, 23, 8, tzinfo=timezone.utc)
    request = recipe.MeetingRequest(is_meeting_request=True, requested_start='2026-11-23T10:00:00+01:00')
    slot = recipe.choose_slot(request, [{'start': current.timestamp(), 'end': current.timestamp() + 3600}], current, 'Europe/London')
    assert slot.hour == 9 and slot.utcoffset() == timedelta(0)


def prepare_inbox(monkeypatch, *, draft_fails=False, pause_during_model=False):
    start = datetime.now(timezone.utc)
    storage.update(enabled=True, status='active', watch_from=(start - timedelta(seconds=30)).isoformat(), drafts_created=0)
    calls = []
    async def native(operation, **values):
        calls.append(operation)
        if operation == 'status': return {'mail': True, 'calendar': True, 'timezone': 'Europe/London'}
        if operation == 'inbox':
            assert values['since'] >= (start - timedelta(seconds=31)).timestamp()
            return {'messages': [{'id': 7, 'message_id': 'test-message', 'subject': 'Meeting', 'received_at': start.timestamp()}]}
        if operation == 'message': return {'message_id': 'test-message', 'subject': 'Meeting', 'body': 'Can we meet?'}
        if operation == 'busy_times': return {'busy': [], 'timezone': 'Europe/London'}
        if operation == 'draft':
            assert values['id'] == 7 and values['message_id'] == 'test-message'
            assert 'Could you confirm' in values['body']
            if draft_fails: raise ValueError('Mail disconnected after saving')
            return {'draft_id': '99'}
        raise AssertionError(operation)
    async def understand(*args):
        if pause_during_model: storage.update(enabled=False, status='paused')
        return recipe.MeetingRequest(is_meeting_request=True)
    monkeypatch.setattr(mac, 'call', native)
    monkeypatch.setattr(recipe, 'understand', understand)
    return calls


def test_repeated_runs_create_one_draft_and_do_not_store_email_content(monkeypatch):
    calls = prepare_inbox(monkeypatch)
    assert asyncio.run(recipe.scan_inbox())['drafted'] == 1
    assert asyncio.run(recipe.scan_inbox())['drafted'] == 0
    assert calls.count('draft') == 1 and 'send' not in calls
    assert storage.get()['drafts_created'] == 1
    assert 'Can we meet?' not in storage.DATABASE.read_bytes().decode('utf-8', errors='ignore')
    events = storage.activity()['items']
    assert len(events) == 2 and all(e['status'] == 'completed' for e in events)
    assert sum(e['drafted'] for e in events) == 1
    assert storage.activity()['unread_count'] == 1


def test_uncertain_save_is_not_duplicated_on_resume(monkeypatch):
    calls = prepare_inbox(monkeypatch, draft_fails=True)
    with pytest.raises(ValueError, match='Check Drafts'): asyncio.run(recipe.scan_inbox())
    assert storage.get()['status'] == 'error' and not storage.get()['enabled']
    storage.update(enabled=True, status='active')
    asyncio.run(recipe.scan_inbox())
    assert calls.count('draft') == 1
    failed = [e for e in storage.activity()['items'] if e['status'] == 'failed']
    assert len(failed) == 1 and failed[0]['drafted'] == 0


def test_save_acknowledgement_failure_recovers_by_reading_without_another_write(monkeypatch):
    calls = prepare_inbox(monkeypatch)
    original = mac.call
    recovery = []
    async def native(operation, **values):
        if operation == 'draft':
            calls.append('draft')
            raise mac.MacAutomationError('mail_error_5003', 'Save acknowledgement failed')
        if operation == 'recover_draft':
            recovery.append(values)
            assert values['id'] == 7 and values['message_id'] == 'test-message'
            assert 'Could you confirm' in values['body']
            assert datetime.now(timezone.utc).timestamp() - 3 < values['since']
            return {'draft_id': '123'}
        return await original(operation, **values)
    monkeypatch.setattr(mac, 'call', native)
    assert asyncio.run(recipe.scan_inbox())['drafted'] == 1
    assert storage.get()['enabled'] and storage.get()['drafts_created'] == 1
    assert storage.activity()['items'][0]['drafts'][0]['status'] == 'ready'
    assert asyncio.run(recipe.scan_inbox())['drafted'] == 0
    assert calls.count('draft') == 1 and len(recovery) == 1


def test_process_crash_during_save_is_reported_once_without_repeating_write(monkeypatch):
    calls = prepare_inbox(monkeypatch)
    fingerprint = hashlib.sha256(b'test-message').hexdigest()
    storage.record_message(fingerprint, 'creating')
    with pytest.raises(ValueError, match='interrupted'): asyncio.run(recipe.scan_inbox())
    storage.update(enabled=True, status='active')
    asyncio.run(recipe.scan_inbox())
    assert 'draft' not in calls and storage.message_state(fingerprint) == 'uncertain'


def test_pause_while_model_is_running_prevents_draft_creation(monkeypatch):
    calls = prepare_inbox(monkeypatch, pause_during_model=True)
    assert asyncio.run(recipe.scan_inbox())['drafted'] == 0
    assert 'draft' not in calls and storage.get()['status'] == 'paused'


def test_cursor_advances_without_losing_messages_at_batch_boundary(monkeypatch):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    storage.update(enabled=True, status='active', watch_from=(now - timedelta(minutes=10)).isoformat())
    messages = [{'id': n, 'message_id': str(n), 'received_at': now.timestamp()} for n in range(1, 13)]
    cutoffs, read = [], []
    async def native(operation, **values):
        if operation == 'status': return {'mail': True, 'calendar': True, 'timezone': 'Europe/London'}
        if operation == 'inbox':
            cutoffs.append(values['since'])
            return {'messages': [m for m in messages if m['received_at'] > values['since']]}
        if operation == 'message':
            read.append(values['id'])
            return {'message_id': str(values['id']), 'subject': 'Receipt', 'body': 'Thank you'}
        raise AssertionError(operation)
    async def understand(*args): return recipe.MeetingRequest(is_meeting_request=False)
    monkeypatch.setattr(mac, 'call', native)
    monkeypatch.setattr(recipe, 'understand', understand)
    assert asyncio.run(recipe.scan_inbox())['checked'] == 10
    assert asyncio.run(recipe.scan_inbox())['checked'] == 2
    assert cutoffs[1] == now.timestamp() - 1 and cutoffs[1] > cutoffs[0]
    assert read == list(range(1, 13))


def test_native_bridge_launches_app_with_private_files_and_cleans_up(monkeypatch, tmp_path):
    from pathlib import Path
    helper = tmp_path / 'Helper.app/Contents/MacOS/AutomationHelper'
    helper.parent.mkdir(parents=True)
    helper.touch()
    monkeypatch.setattr(mac, 'HELPER', helper)
    monkeypatch.setattr(mac.sys, 'platform', 'darwin')
    paths = []
    async def launch(*args, **kwargs):
        assert args[0:4] == ('/usr/bin/open', '-n', '-g', '-W')
        assert args[-1] == str(helper.parents[2])
        assert 'private-test-credential' not in ' '.join(args)
        source, dest = Path(args[args.index('--stdin') + 1]), Path(args[args.index('--stdout') + 1])
        assert source.stat().st_mode & 0o777 == 0o600
        assert dest.stat().st_mode & 0o777 == 0o600
        assert json.loads(source.read_text())['value'] == 'private-test-credential'
        dest.write_text('{"saved":true}')
        paths.extend([source, dest])
        class Process:
            returncode = 0
            async def wait(self): return 0
        return Process()
    monkeypatch.setattr(mac.asyncio, 'create_subprocess_exec', launch)
    assert asyncio.run(mac.call('save_key', value='private-test-credential')) == {'saved': True}
    assert all(not p.exists() for p in paths)


def test_settings_links_allow_only_known_privacy_panes(monkeypatch):
    calls = []
    async def native(operation, **values):
        calls.append((operation, values))
        return {'opened': True}
    monkeypatch.setattr(mac, 'call', native)
    with TestClient(main.app) as client:
        assert client.post('/api/connections/mac/settings/Calendars').status_code == 200
        assert client.post('/api/connections/mac/settings/Automation').status_code == 200
        assert client.post('/api/connections/mac/settings/AnythingElse').status_code == 404
    assert calls == [('open_settings', {'pane': 'Calendars'}), ('open_settings', {'pane': 'Automation'})]


def test_calendar_permission_is_requested_separately(monkeypatch):
    calls = []
    async def native(operation, **values):
        calls.append((operation, values))
        return {'calendar': True, 'mail': False}
    monkeypatch.setattr(mac, 'call', native)
    with TestClient(main.app) as client:
        assert client.post('/api/connections/mac?service=calendar').status_code == 200
        assert client.post('/api/connections/mac?service=unknown').status_code == 422
    assert calls == [('permissions', {'service': 'calendar'})]


def test_repeated_activation_reuses_a_healthy_worker(monkeypatch, tmp_path):
    python = tmp_path / 'python'
    helper = tmp_path / 'helper'
    python.touch(); helper.touch()
    monkeypatch.setattr(service, 'PYTHON', python)
    monkeypatch.setattr(mac, 'HELPER', helper)
    monkeypatch.setattr(service, 'LAUNCH_AGENT', tmp_path / 'agent.plist')
    calls = []
    async def launch(*args, **kwargs):
        calls.append(args)
        class Process:
            returncode = 0
            async def wait(self): return 0
            async def communicate(self): return b'state = running', None
        return Process()
    heartbeat = datetime.now(timezone.utc)
    async def remote(*args, **kwargs):
        return {'is_active': True, 'active_worker_count': 1, 'last_heartbeat': heartbeat.isoformat()}
    monkeypatch.setattr(service.asyncio, 'create_subprocess_exec', launch)
    monkeypatch.setattr(service, 'remote', remote)
    async def run():
        await service.start_worker()
        assert await service.start_worker() == heartbeat
    asyncio.run(run())
    assert sum('bootstrap' in args for args in calls) == 1
    assert sum('bootout' in args for args in calls) == 1


def test_activity_pagination_filters_and_read_state():
    first = storage.begin_run('Meeting replies')
    storage.update_run(first, checked=1, drafted=1, status='completed')
    second = storage.begin_run('Meeting replies')
    storage.update_run(second, checked=0, status='completed')
    third = storage.begin_run('Meeting replies')
    storage.update_run(third, status='failed', message='Mail is unavailable')
    with TestClient(main.app) as client:
        latest = client.get('/api/activity?limit=2').json()
        assert [e['id'] for e in latest['items']] == [third, second]
        assert latest['unread_count'] == 2
        older = client.get('/api/activity', params={'before': latest['next_before'], 'limit': 2}).json()
        assert [e['id'] for e in older['items']] == [first]
        assert older['next_before'] is None
        results = client.get('/api/activity?results_only=true').json()
        assert [e['id'] for e in results['items']] == [third, first]
        assert client.post('/api/activity/read', headers={'Origin':'https://elsewhere.example'}).status_code == 403
        assert client.post('/api/activity/read').status_code == 200
        assert client.get('/api/activity').json()['unread_count'] == 0
        assert all(not e['unread'] for e in client.get('/api/activity').json()['items'])
        assert client.get('/api/activity?limit=10000').status_code == 422
    fourth = storage.begin_run('Meeting replies')
    storage.update_run(fourth, drafted=1, status='completed')
    assert storage.activity()['unread_count'] == 1


def test_clear_notifications_preserves_records_drafts_and_in_progress_results():
    drafted = storage.begin_run('Meeting replies')
    storage.record_draft('preserved-reply', '99', run_id=drafted, original_id=7, original_message_id='original')
    storage.update_run(drafted, checked=1, drafted=1, status='completed')
    failed = storage.begin_run('Meeting replies')
    storage.update_run(failed, status='failed', message='Mail unavailable')
    quiet = storage.begin_run('Meeting replies')
    storage.update_run(quiet, status='completed')
    running = storage.begin_run('Meeting replies')
    with TestClient(main.app) as client:
        filtered = client.get('/api/activity?results_only=true').json()
        assert len(filtered['items']) == 2 and filtered['clearable_count'] == 3
        assert client.post('/api/activity/clear', headers={'Origin': 'https://elsewhere.example'}).status_code == 403
        assert storage.activity()['unread_count'] == 2
        response = client.post('/api/activity/clear')
        assert response.status_code == 200 and response.json()['cleared'] == 3
        activity = client.get('/api/activity').json()
        assert [item['id'] for item in activity['items']] == [running]
        assert activity['unread_count'] == activity['clearable_count'] == 0
        assert activity['cleared_at'] == response.json()['cleared_at']
        assert client.get('/api/activity?results_only=true').json()['items'] == []
        assert client.post('/api/activity/clear').json()['cleared'] == 0
    # Dismissal survives another application session and does not delete history
    # or the ledger that prevents duplicate drafts.
    with TestClient(main.app) as client:
        assert [item['id'] for item in client.get('/api/activity').json()['items']] == [running]
        with storage.connection() as db:
            assert db.execute('SELECT count(*) FROM runs').fetchone()[0] == 4
        assert storage.get_draft('preserved-reply')['status'] == 'ready'
        assert storage.message_state('preserved-reply') == 'drafted'
        storage.update_run(running, status='failed', message='New result after clearing')
        activity = client.get('/api/activity?results_only=true').json()
        assert [item['id'] for item in activity['items']] == [running]
        assert activity['unread_count'] == activity['clearable_count'] == 1


def test_new_activity_paginates_without_restoring_cleared_entries():
    old = storage.begin_run('Old check')
    storage.update_run(old, status='failed')
    storage.clear_activity()
    new = []
    for _ in range(3):
        run = storage.begin_run('New check')
        storage.update_run(run, status='completed', drafted=1)
        new.append(run)
    latest = storage.activity(limit=2)
    older = storage.activity(before=latest['next_before'], limit=2)
    assert [item['id'] for item in latest['items']] == list(reversed(new[1:]))
    assert [item['id'] for item in older['items']] == new[:1]
    assert older['next_before'] is None
    assert latest['unread_count'] == latest['clearable_count'] == 3


def test_interrupted_runs_remain_in_history_with_confirmed_draft_count():
    first = storage.begin_run('Meeting replies')
    storage.update_run(first, checked=2, drafted=1, message='Reading another message')
    storage.begin_run('Meeting replies')
    prior = next(e for e in storage.activity()['items'] if e['id'] == first)
    assert prior['status'] == 'interrupted' and prior['drafted'] == 1
    assert prior['finished_at'] is not None and prior['unread']


def test_mail_read_timeout_retries_without_disabling_or_queuing_more_reads(monkeypatch):
    storage.update(enabled=True, status='active', watch_from=storage.now())
    reads = []
    async def native(operation, **values):
        if operation == 'status': return {'mail': True, 'calendar': True, 'timezone': 'Europe/London'}
        if operation == 'inbox':
            reads.append(operation)
            if len(reads) == 1: raise mac.MacAutomationError('mail_timeout', 'Mail is busy')
            return {'messages': []}
        raise AssertionError(operation)
    monkeypatch.setattr(mac, 'call', native)
    assert asyncio.run(recipe.scan_inbox())['status'] == 'deferred'
    assert storage.get()['enabled'] and storage.get()['retry_after']
    assert asyncio.run(recipe.scan_inbox())['status'] == 'deferred'
    assert len(reads) == 1
    assert [e['status'] for e in storage.activity()['items']] == ['deferred', 'failed']
    storage.update(retry_after=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    assert asyncio.run(recipe.scan_inbox())['status'] == 'completed'
    assert storage.get()['retry_after'] is None and storage.get()['enabled']


def test_draft_timeout_still_pauses_instead_of_retrying_a_write(monkeypatch):
    prepare_inbox(monkeypatch)
    original = mac.call
    async def native(operation, **values):
        if operation == 'draft': raise mac.MacAutomationError('native_timeout', 'Save timed out')
        return await original(operation, **values)
    monkeypatch.setattr(mac, 'call', native)
    with pytest.raises(ValueError, match='Check Drafts'): asyncio.run(recipe.scan_inbox())
    assert not storage.get()['enabled'] and storage.get()['status'] == 'error'


@pytest.mark.parametrize('draft_id', ['0', '', None])
def test_unconfirmed_draft_is_not_reported_as_saved_or_retried(monkeypatch, draft_id):
    prepare_inbox(monkeypatch)
    original = mac.call
    writes = []
    async def native(operation, **values):
        if operation == 'draft':
            writes.append(values['id'])
            return {'draft_id': draft_id}
        return await original(operation, **values)
    monkeypatch.setattr(mac, 'call', native)
    with pytest.raises(ValueError, match='Check Drafts'): asyncio.run(recipe.scan_inbox())
    assert storage.get()['drafts_created'] == 0
    assert storage.activity()['items'][0]['drafted'] == 0
    storage.update(enabled=True, status='active')
    assert asyncio.run(recipe.scan_inbox())['drafted'] == 0
    assert writes == [7]


def review_fixture(monkeypatch):
    run_id = storage.begin_run('Meeting replies')
    storage.record_draft('review-test', '99', run_id=run_id, original_id=7, original_message_id='original-id')
    storage.update_run(run_id, checked=1, drafted=1, status='completed')
    calls = []
    async def native(operation, **values):
        calls.append(operation)
        if operation == 'draft_preview':
            return {'revision': 'a' * 64, 'subject': 'Re: Meeting', 'body': 'Private draft',
                    'sender': 'me@example.com', 'to': ['person@example.com'], 'cc': [], 'bcc': [], 'attachments': 0}
        if operation == 'message':
            return {'message_id': 'original-id', 'subject': 'Meeting', 'sender': 'person@example.com', 'body': 'Private original'}
        if operation == 'send_draft':
            assert values['id'] == 99 and values['original_id'] == 7
            assert values['original_message_id'] == 'original-id' and values['revision'] == 'a' * 64
            await asyncio.sleep(0)
            return {'accepted': True}
        raise AssertionError(operation)
    monkeypatch.setattr(mac, 'call', native)
    return calls


def test_draft_preview_is_read_only_and_contains_both_emails(monkeypatch):
    calls = review_fixture(monkeypatch)
    with TestClient(main.app) as client:
        response = client.get('/api/drafts/review-test')
        assert response.status_code == 200
        assert response.json()['original']['body'] == 'Private original'
        assert response.json()['draft']['body'] == 'Private draft'
        assert client.get('/api/drafts/unknown').status_code == 404
        assert client.get('/api/activity').json()['items'][0]['drafts'] == [{'id': 'review-test', 'status': 'ready'}]
    assert calls == ['draft_preview', 'message']
    assert b'Private original' not in storage.DATABASE.read_bytes()
    assert b'Private draft' not in storage.DATABASE.read_bytes()


def test_send_requires_explicit_confirmation_current_preview_and_same_origin(monkeypatch):
    calls = review_fixture(monkeypatch)
    with TestClient(main.app) as client:
        url = '/api/drafts/review-test/send'
        assert client.post(url, json={'revision': 'a' * 64}).status_code == 422
        assert client.post(url, json={'revision': 'a' * 64, 'confirmed': False}).status_code == 422
        assert client.post(url, json={'revision': 'a' * 64, 'confirmed': True}, headers={'Origin': 'https://elsewhere.example'}).status_code == 403
        assert client.post(url, json={'revision': 'b' * 64, 'confirmed': True}).status_code == 409
        assert storage.get_draft('review-test')['status'] == 'ready'
        assert 'send_draft' not in calls
        assert client.post(url, json={'revision': 'a' * 64, 'confirmed': True}).json() == {'status': 'sent'}
        assert client.post(url, json={'revision': 'a' * 64, 'confirmed': True}).json() == {'status': 'sent'}
    assert calls.count('send_draft') == 1


def test_concurrent_send_clicks_dispatch_only_once(monkeypatch):
    calls = review_fixture(monkeypatch)
    async def run():
        results = await asyncio.gather(draft_review.send('review-test', 'a' * 64),
                                       draft_review.send('review-test', 'a' * 64), return_exceptions=True)
        assert any(result == {'status': 'sent'} for result in results)
    asyncio.run(run())
    assert calls.count('send_draft') == 1


def test_ambiguous_send_is_never_retried(monkeypatch):
    review_fixture(monkeypatch)
    original = mac.call
    sends = []
    async def native(operation, **values):
        if operation == 'send_draft':
            sends.append(values)
            raise mac.MacAutomationError('native_timeout', 'Response lost')
        return await original(operation, **values)
    monkeypatch.setattr(mac, 'call', native)
    with pytest.raises(draft_review.ReviewError, match='could not confirm'):
        asyncio.run(draft_review.send('review-test', 'a' * 64))
    assert storage.get_draft('review-test')['status'] == 'send_uncertain'
    with pytest.raises(draft_review.ReviewError): asyncio.run(draft_review.send('review-test', 'a' * 64))
    assert len(sends) == 1


def test_native_revision_rejection_allows_a_fresh_review(monkeypatch):
    review_fixture(monkeypatch)
    original = mac.call
    async def native(operation, **values):
        if operation == 'send_draft': raise mac.MacAutomationError('draft_changed', 'Changed since preview')
        return await original(operation, **values)
    monkeypatch.setattr(mac, 'call', native)
    with pytest.raises(draft_review.ReviewError, match='Changed'):
        asyncio.run(draft_review.send('review-test', 'a' * 64))
    assert storage.get_draft('review-test')['status'] == 'ready'


def test_interrupted_send_is_marked_uncertain_after_restart(monkeypatch):
    review_fixture(monkeypatch)
    storage.draft_status('review-test', 'sending', expected='ready')
    with TestClient(main.app) as client:
        assert client.get('/api/drafts/review-test').json()['status'] == 'send_uncertain'


def test_sending_clears_only_its_notification_and_preserves_history(monkeypatch):
    review_fixture(monkeypatch)
    other = storage.begin_run('Another check')
    storage.update_run(other, status='failed', message='Mail unavailable')
    assert storage.activity()['unread_count'] == 2
    asyncio.run(draft_review.send('review-test', 'a' * 64))
    activity = storage.activity()
    assert activity['unread_count'] == 1
    sent = next(item for item in activity['items'] if item['drafts'])
    assert not sent['unread'] and sent['drafts'][0]['status'] == 'sent'
    assert next(item for item in activity['items'] if item['id'] == other)['unread']


def test_run_with_multiple_drafts_stays_unread_until_all_are_sent(monkeypatch):
    review_fixture(monkeypatch)
    record = storage.get_draft('review-test')
    storage.record_draft('another-reply', '100', run_id=record['run_id'], original_id=8, original_message_id='another-original')
    storage.draft_status('review-test', 'sent')
    assert storage.activity()['unread_count'] == 1
    storage.draft_status('another-reply', 'sent')
    assert storage.activity()['unread_count'] == 0
