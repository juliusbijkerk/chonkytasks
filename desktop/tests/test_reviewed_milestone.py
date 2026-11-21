import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import time

import httpx

from fastapi.testclient import TestClient
import pytest

from app import analyze, automations, automation_store, draft_review, mac_automation, main, meeting_automation
from app import completion, office_automation, office_store, privacy_review, workflow_evidence
from app.models import Proposal
from app.office_recipes import RECIPES


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    jobs = tmp_path / 'jobs'
    jobs.mkdir()
    monkeypatch.setattr(main, 'DATA', jobs)
    monkeypatch.setattr(main, 'JOBS', {})
    monkeypatch.setattr(main, 'TASKS', set())
    monkeypatch.setattr(automation_store, 'DATABASE', tmp_path / 'automation.sqlite')
    monkeypatch.delenv('MISTRAL_API_KEY', raising=False)
    monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)
    return tmp_path


def test_review_binds_only_the_selected_jpeg_and_context(sandbox, monkeypatch):
    monkeypatch.setattr(main, 'ensure_ready', lambda: None)
    helper = sandbox / 'mock-helper'
    helper.touch()
    monkeypatch.setattr(mac_automation, 'HELPER', helper)
    bridge_calls = []
    calls = []

    async def keychain(operation):
        bridge_calls.append(operation)
        if operation == 'key_status':
            return {'configured': True}
        assert operation == 'get_key'
        return {'value': 'fake-saved-mistral-key'}

    monkeypatch.setattr(mac_automation, 'call', keychain)
    job = main.new_job('local-file-secret.mp4', 'unreviewed context')
    job.update(status='review', redaction_ready=True, metadata={'duration': 12}, sample_interval=1,
               frames=[{'id': f'f{i:04}', 'timestamp': i * 3} for i in range(1, 4)])
    folder = main.DATA / job['id']
    redacted = folder / 'redacted'
    redacted.mkdir(parents=True)
    images = {frame['id']: b'\xff\xd8sanitized-' + frame['id'].encode() + b'\xff\xd9' for frame in job['frames']}
    for frame_id, jpeg in images.items():
        (redacted / f'{frame_id}.jpg').write_bytes(jpeg)
    (folder / 'frames').mkdir()
    (folder / 'frames/f0001.jpg').write_bytes(b'unredacted-secret')
    main.persist(job)

    def provider(request):
        assert request.url == httpx.URL('https://api.mistral.ai/v1/chat/completions')
        assert request.headers['Authorization'] == 'Bearer fake-saved-mistral-key'
        body = json.loads(request.content)
        assert body['model'] == 'mistral-large-4-0' and 'provider' not in body
        content = body['messages'][1]['content']
        blob = json.dumps(content)
        assert 'safe context' in blob
        assert all(secret not in blob for secret in ['unredacted-secret', 'unreviewed context', 'local-file-secret', 'f0002'])
        if isinstance(content, list):
            calls.append('vision')
            urls = [part['image_url'] for part in content if part['type'] == 'image_url']
            assert [base64.b64decode(url.split(',')[1]) for url in urls] == [images['f0001'], images['f0003']]
            result = {'limitations': [], 'steps': [
                {'title': 'View', 'application': 'Mail', 'description': 'Message visible', 'evidence_type': 'observed',
                 'confidence': 'high', 'uncertainty': '', 'start_frame_id': id, 'end_frame_id': id}
                for id in ('f0001', 'f0003')]}
        else:
            calls.append('report')
            result = {'title': 'Reviewed test', 'summary': 'Visible activity', 'limitations': [],
                'recommendations': [], 'workflow': {'name': 'Review', 'description': '', 'occurrences': [], 'repetition_assessment': ''}}
        event = {'model': body['model'], 'choices': [{'delta': {'content': json.dumps(result)}, 'finish_reason': 'stop'}]}
        return httpx.Response(200, text=f'data: {json.dumps(event)}\n\ndata: [DONE]\n\n')

    client_type = httpx.AsyncClient
    monkeypatch.setattr(completion.httpx, 'AsyncClient', lambda **options: client_type(
        transport=httpx.MockTransport(provider), **options))
    root = f'/api/jobs/{job["id"]}'
    with TestClient(main.app) as client:
        health = client.get('/api/health').json()
        assert health['configured'] and 'fake-saved-mistral-key' not in json.dumps(health)
        chosen = {'frame_ids': ['f0003', 'f0001'], 'context': 'safe context'}
        packet = client.post(root + '/privacy/preview?exact=true', json=chosen).json()
        assert packet['provider'] == 'Mistral'
        assert [frame['id'] for frame in packet['frames']] == ['f0001', 'f0003'] and not calls
        assert base64.b64decode(packet['frames'][0]['image_url'].split(',', 1)[1]) == images['f0001']
        assert client.get(root + '/redacted/f0001.jpg').content == images['f0001']
        approved = {**chosen, 'revision': packet['revision'], 'confirmed': True}
        assert client.post(root + '/privacy/approve', json={**approved, 'context': 'changes'}).status_code == 409
        assert client.post(root + '/privacy/approve', json={**approved, 'confirmed': False}).status_code == 422
        (redacted / 'f0001.jpg').write_bytes(b'\xff\xd8changed\xff\xd9')
        assert client.post(root + '/privacy/approve', json=approved).status_code == 409
        assert not calls and 'get_key' not in bridge_calls
        (redacted / 'f0001.jpg').write_bytes(images['f0001'])
        assert client.post(root + '/privacy/approve', json=approved).status_code == 202
        completed = until(client, root, 'completed')
        assert completed['report']['excluded_frame_ids'] == ['f0002']
        assert calls == ['vision', 'report'] and bridge_calls.count('get_key') == 1
        assert completed['privacy_approval']['provider'] == 'Mistral'


def test_refresh_preserves_local_report_and_invalidates_old_evidence(sandbox, monkeypatch):
    job = main.new_job('synthetic.mp4', '')
    job.update(status='completed', local_only=True, redaction_ready=True,
               frames=[{'id': 'f0001', 'timestamp': 0}])
    job['report'] = workflow_evidence.local_report(job)
    main.persist(job)
    image = main.DATA / job['id'] / 'redacted/f0001.jpg'
    image.parent.mkdir()
    image.write_bytes(b'\xff\xd8older-masks\xff\xd9')
    updated = b'\xff\xd8sensitive-values\xff\xd9'

    def prepare(folder, frames):
        assert folder == image.parent.parent and frames == job['frames']
        image.write_bytes(updated)

    monkeypatch.setattr(privacy_review, 'prepare', prepare)
    route = f"/api/jobs/{job['id']}/privacy"
    with TestClient(main.app) as client:
        selection = {'frame_ids': ['f0001']}
        before = client.post(route + '/preview?exact=true', json=selection).json()
        response = client.post(route + '/refresh')
        assert response.status_code == 200, response.text
        assert response.json()['redaction_policy'] == privacy_review.REDACTION_POLICY
        assert response.json()['report'] == job['report'] and response.json()['local_only']
        assert not response.json().get('privacy_approval')
        assert client.post(route + '/approve', json={
            **selection, 'revision': before['revision'], 'confirmed': True}).status_code == 409
        after = client.post(route + '/preview?exact=true', json=selection).json()
        assert after['revision'] != before['revision']
        assert base64.b64decode(after['frames'][0]['image_url'].split(',', 1)[1]) == updated


def test_legacy_analysis_retry_requires_new_destination_approval(sandbox, monkeypatch):
    monkeypatch.setattr(main, 'ensure_ready', lambda: None)
    job = main.new_job('synthetic.mp4', '')
    job.update(status='failed', redaction_ready=True, frames=[{'id': 'f0001', 'timestamp': 0}],
               privacy_approval={'model': 'mistralai/mistral-large-4-0', 'revision': 'a' * 64})
    main.persist(job)

    def unexpected_launch(*args, **kwargs):
        pytest.fail('A provider change must return to review before any analysis runs')

    monkeypatch.setattr(main, 'launch', unexpected_launch)
    with TestClient(main.app) as client:
        response = client.post(f"/api/jobs/{job['id']}/retry")
        assert response.status_code == 202
        assert response.json()['status'] == 'review'
        assert response.json()['privacy_approval'] is None


def until(client, url, expected, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = client.get(url).json()
        if result['status'] in {expected, 'failed'}:
            assert result['status'] == expected, result.get('message')
            return result
        time.sleep(.15)
    pytest.fail(f'Job did not reach {expected}')


@pytest.mark.skipif(not os.getenv('CHONKYTASKS_SYNTHETIC_VIDEOS'), reason='set the generated fixture directory to run the native video test')
@pytest.mark.parametrize('name,per_kind', [('Extended workweek.mp4', 2), ('Extended no-match control.mp4', 0)])
def test_whole_synthetic_video_to_local_report(sandbox, monkeypatch, name, per_kind):
    async def no_provider(*_args, **_kwargs):
        pytest.fail('Local-only analysis called a provider')
    monkeypatch.setattr(analyze, 'complete', no_provider)
    video = Path(os.environ['CHONKYTASKS_SYNTHETIC_VIDEOS']) / name
    with TestClient(main.app) as client, video.open('rb') as clip:
        upload = client.post('/api/jobs', files={'file': (video.name, clip, 'video/mp4')})
        assert upload.status_code == 202, upload.text
        url = '/api/jobs/' + upload.json()['id']
        awaiting = until(client, url, 'review', timeout=260)
        assert awaiting['redaction_ready'], awaiting.get('redaction_message')
        first_frame = awaiting['frames'][0]['id']
        snapshot = client.get(f'{url}/redacted/{first_frame}.jpg')
        assert snapshot.status_code == 200 and snapshot.content.startswith(b'\xff\xd8')
        assert snapshot.content != client.get(f'{url}/frames/{first_frame}.jpg').content
        local = client.post(url + '/privacy/local').json()
        kinds = local['report']['local_findings']
        assert local['status'] == 'completed' and local['local_only'] and not local.get('privacy_approval')
        assert local['report']['local_evidence_status'] == 'checked'
        if per_kind:
            assert len(kinds) == 6 and all(len(kind['examples']) == per_kind for kind in kinds)
            assert [r['automation_recipe'] for r in local['report']['recommendations'] if r['activation_ready']][0] == 'meeting_reply_drafts'
            assert {r['automation_recipe'] for r in local['report']['recommendations'] if r['activation_ready']} == {
                'meeting_reply_drafts', 'mailToTask', 'mailToNote', 'invoiceFiling', 'spreadsheetReport', 'orderDraft'}
            exercise_office_workflows(client, local, monkeypatch)
            assert all(example['source'] < example['result'] for kind in kinds for example in kind['examples'])
        else:
            assert kinds == local['report']['recommendations'] == []


def exercise_office_workflows(client, job, monkeypatch):
    settings = {'mailToTask': {'subject_prefix': '[Follow-up]'}, 'mailToNote': {'subject_prefix': '[Field note]'},
                'invoiceFiling': {}, 'spreadsheetReport': {'status_filter': 'Settled'}, 'orderDraft': {}}
    remote_schedule = None
    created = []
    pauses = []
    native_reads = []
    messages = {}

    async def remote(method, path, **kwargs):
        nonlocal remote_schedule
        if path == '/workflows/workers/whoami':
            assert method == 'GET'
            return {}
        if path.startswith('/workflows/deployments/'):
            assert method == 'GET'
            return {'is_active': True}
        if path == '/workflows/schedules':
            assert method == 'POST' and remote_schedule is None
            created.append(kwargs['json'])
            remote_schedule = {'deployment_name': automations.deployment_name(),
                'workflow_name': automations.WORKFLOW_NAME, 'paused': False}
            return {}
        if path.endswith('/pause'):
            assert method == 'POST'
            pauses.append(path)
            remote_schedule['paused'] = True
        elif path.endswith('/resume'):
            assert method == 'POST'
            remote_schedule['paused'] = False
        elif method == 'PATCH':
            assert kwargs['json'] == {'schedule': {'intervals': [{'every': 'PT30S'}]}}
        else:
            assert method == 'GET'
        return remote_schedule

    async def native(action, **arguments):
        native_reads.append(action)
        if action == 'status':
            return {'mail': True, 'calendar': False}
        if action == 'inbox':
            selected = [mail for mail in messages.values() if mail['received_at'] > arguments['since']]
            return {'messages': [{key: mail[key] for key in ('id', 'message_id', 'subject', 'received_at')}
                                 for mail in selected]}
        if action == 'message':
            assert arguments['full_body'] is True
            return messages[arguments['id']]
        pytest.fail(f'Tried using a real or write-capable Mail operation: {action}')

    async def ready(*_args):
        return True

    async def verify_settings():
        return {'connected': True, 'mail': True, 'calendar': False, 'runtime_ready': True}

    monkeypatch.setattr(automations, 'LOCK', asyncio.Lock())
    monkeypatch.setattr(automations, 'REMOTE_CACHE', None)
    monkeypatch.setattr(automations, 'remote', remote)
    monkeypatch.setattr(automations, 'settings', verify_settings)
    monkeypatch.setattr(automations, 'start_worker', ready)
    monkeypatch.setattr(automations, 'wait_for_worker', ready)
    monkeypatch.setattr(mac_automation, 'call', native)
    monkeypatch.setattr(mac_automation, 'key_configured', ready)
    indexes = {item['automation_recipe']: i for i, item in enumerate(job['report']['recommendations'])}
    activation = {'job_id': job['id'], 'recommendation_index': indexes['mailToTask']}
    assert client.post('/api/automations/activate', json=activation).status_code == 422
    for kind, config in settings.items():
        response = client.post('/api/automations/activate', json={
            'job_id': job['id'], 'recommendation_index': indexes[kind], 'config': config})
        assert response.status_code == 202, response.text
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        states = client.get('/api/automations').json()['office_workflows']
        if len(states) == len(settings) and all(state['status'] == 'active' for state in states):
            break
        time.sleep(.05)
    assert {state['kind'] for state in states} == set(settings), states
    assert all(state['enabled'] and state['status'] == 'active' for state in states), states
    assert len(created) == 1
    assert created[0]['schedule']['input'] == {'automation_id': automation_store.AUTOMATION_ID}
    assert created[0]['schedule']['intervals'] == [{'every': 'PT30S'}]

    received = int(time.time())
    for index, (subject, sender, body) in enumerate([
        ('[Follow-up] Inventory', 'Inez Bell <inez@example.test>',
         'New stock arrived today.\nTask: Recount the cobalt trays\nDue: 2030-09-16'),
        ('[Field note] Site walk', 'Peter Fox <peter@example.test>',
         'Three skylights need new seals. Contact the glazing lead next week.'),
        ('Coffee?', 'Gina Park <gina@example.test>', 'Do you want a latte?'),
    ], 1):
        messages[index] = {'id': index, 'message_id': f'fresh-synthetic-{index}@example.test',
                           'subject': subject, 'sender': sender, 'body': body, 'received_at': received}
    invoice = b'Invoice: FS-405\nVendor: Fable Timber\nDate: 2030-08-12\nTotal: 421.72 GBP\n'
    inputs = {'invoiceFiling': ('invoice-fs405.txt', invoice),
              'spreadsheetReport': ('branch-rollup.csv',
                  b'Status,Amount\nSettled,4.20\nOpen,900.00\nSETTLED,0.31\nResettled,180.00\n'),
              'orderDraft': ('fresh-order.txt',
                  b'Order: TST-204\nCustomer: Whitestone Makers\nSKU: PLN-149\nQuantity: 12\n')}
    for kind, (name, payload) in inputs.items():
        (office_automation.inbox_path(kind) / name).write_bytes(payload)
    # Invoke the body registered with the same worker activity as a scheduled tick.
    result = client.portal.call(automations.scan_due_workflows)
    assert {kind: counts['drafted'] for kind, counts in result['office'].items()} == dict.fromkeys(settings, 1)
    assert result['meeting'] == {'checked': 0, 'drafted': 0, 'status': 'paused'}
    assert not any(secret in json.dumps(result) for secret in ['Inez', 'Fable', 'Whitestone', 'PLN-149', 'skylights'])
    assert set(native_reads) == {'status', 'inbox', 'message'}

    activity = '/api/activity?results_only=true'
    def entries():
        return {item['workflow_kind']: item['workflow_drafts'][0] for item in client.get(activity).json()['items']
                if item.get('workflow_drafts')}

    reviews = entries()
    assert set(reviews) == set(settings)
    assert client.post('/api/activity/clear').json()['cleared'] == 0
    assert entries() == reviews
    outputs = {}
    for kind, pending in reviews.items():
        url = f"/api/workflow-drafts/{pending['id']}"
        reviewed = client.get(url).json()
        assert reviewed['status'] == 'ready' and reviewed['kind'] == kind and reviewed['source_preview']
        assert reviewed['preview'] and client.get(url + '/output').status_code == 404
        library = automation_store.DATABASE.parent / 'workflow-library' / kind
        assert not any(path.is_file() for path in library.glob('**/*'))
        approved = {'revision': reviewed['revision'], 'confirmed': True}
        assert client.post(url + '/approve', json={**approved, 'revision': 'f' * 64}).status_code == 409
        assert client.post(url + '/approve', json={**approved, 'confirmed': False}).status_code == 422
        assert client.post(url + '/approve', json=approved, headers={'Origin': 'https://elsewhere.example'}).status_code == 403
        finalized = client.post(url + '/approve', json=approved)
        assert finalized.status_code == 200, finalized.text
        saved = finalized.json()
        assert saved['status'] == 'saved' and saved['approved_at']
        assert client.post(url + '/approve', json=approved).json() == saved
        artifact = client.get(url + '/output')
        assert artifact.status_code == 200 and artifact.headers['content-disposition'].startswith('attachment;')
        assert Path(saved['output_path']).read_bytes() == artifact.content
        outputs[kind] = saved['relative_path'], artifact.content
    assert b'Recount the cobalt trays' in outputs['mailToTask'][1] and b'2030-09-16' in outputs['mailToTask'][1]
    assert b'Three skylights need new seals' in outputs['mailToNote'][1] and b'peter@example.test' in outputs['mailToNote'][1]
    assert outputs['invoiceFiling'] == ('fable-timber/2030-08/invoice-fs405.txt', invoice)
    assert outputs['spreadsheetReport'][1] == b'Status,Rows,Total\r\nSettled,2,4.51\r\n'
    assert json.loads(outputs['orderDraft'][1]) == {
        'order': 'TST-204', 'customer': 'Whitestone Makers', 'sku': 'PLN-149', 'quantity': 12, 'status': 'draft'}
    assert client.get(activity).json()['unread_count'] == 0

    order_route = '/api/automations/orderDraft'
    assert client.post(order_route + '/pause').json()['status'] == 'paused' and not pauses
    (office_automation.inbox_path('orderDraft') / 'next-order.txt').write_bytes(
        b'Order: TST-205\nCustomer: Finch Boatworks\nSKU: MTS-2\nQuantity: 3\n')
    next_result = client.portal.call(automations.scan_due_workflows)
    assert 'orderDraft' not in next_result['office']
    assert all(not counts['drafted'] for counts in next_result['office'].values())
    assert client.post(order_route + '/resume').status_code == 202
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not office_store.get('orderDraft')['enabled']:
        time.sleep(.05)
    assert office_store.get('orderDraft')['enabled']
    next_result = client.portal.call(automations.scan_due_workflows)
    assert next_result['office']['orderDraft']['drafted'] == 1
    assert all(not counts['drafted'] for kind, counts in next_result['office'].items() if kind != 'orderDraft')
    for kind in settings:
        assert client.post(f'/api/automations/{kind}/pause').json()['status'] == 'paused'
    assert len(created) == len(pauses) == 1 and remote_schedule['paused']


def test_all_office_recipes_run_and_require_approval(sandbox, monkeypatch):
    # This covers the same scheduler and approval path on machines without the Vision helpers.
    job = main.new_job('synthetic-office.mp4', '')
    job.update(status='completed', stage='completed', local_evidence_status='checked', local_findings=[
        {'kind': kind, 'examples': [{'source': 0, 'result': 1}, {'source': 2, 'result': 3}]}
        for kind in RECIPES
    ])
    job['report'] = workflow_evidence.local_report(job)
    main.persist(job)
    with TestClient(main.app) as client:
        exercise_office_workflows(client, job, monkeypatch)


def test_meeting_decisions_follow_the_requested_time():
    received = datetime(2026, 11, 21, 14, tzinfo=timezone.utc)
    request = meeting_automation.simple_request('Are you available to talk on the 23rd at 4pm?', received, 'Europe/London')
    assert request.requested_start == '2026-11-23T16:00:00+00:00'
    free = meeting_automation.choose_slot(request, [], received, 'Europe/London')
    conflict = [{'start': free.timestamp(), 'end': (free + timedelta(hours=1)).timestamp()}]
    later = meeting_automation.choose_slot(request, conflict, received, 'Europe/London')
    assert meeting_automation.meeting_plan(request, free, 'Europe/London')['kind'] == 'accept'
    assert meeting_automation.meeting_plan(request, later, 'Europe/London')['kind'] == 'alternative'
    assert later.hour == 17
    assert meeting_automation.meeting_plan(request, None, 'Europe/London') == {'kind': 'clarify'}
    assert meeting_automation.simple_request('Old thread only\n\nOn Tuesday someone wrote:\nAre you available to talk on the 23rd at 4pm?',
                                             received, 'Europe/London') is None


def test_repeated_source_result_pairs_gate_activation():
    frames = [{'id': f'f{i:04}', 'timestamp': i} for i in range(4)]
    steps = [{ 'start_frame_id': f['id'], 'end_frame_id': f['id'], 'evidence_type': 'observed',
               'evidence_role': 'source' if i % 2 == 0 else 'result'} for i, f in enumerate(frames)]
    pairs = [{'source_frame_id': frames[i]['id'], 'result_frame_id': frames[i + 1]['id']} for i in (0, 2)]

    def proposal(evidence_pairs):
        return Proposal.model_validate({'title': 'Review', 'summary': '', 'limitations': [],
            'workflow': {'name': 'Review', 'description': '', 'occurrences': [], 'repetition_assessment': ''},
            'recommendations': [{'title': 'Handle meetings', 'summary': '', 'confidence': 'high',
                'automation_recipe': 'meeting_reply_drafts', 'evidence_frame_ids': ['f0000'],
                'evidence_pairs': evidence_pairs, 'automated_steps': ['Check requests'], 'human_steps': [],
                'integrations': [], 'implementation_steps': ['Connect apps'], 'effort': 'small',
                'savings': {'estimated_seconds_per_run': None, 'assumptions': []}, 'assumptions': []}]})

    assert analyze.finalize(proposal(pairs), steps, frames)['recommendations'][0]['activation_ready']
    assert not analyze.finalize(proposal([pairs[0], pairs[0]]), steps, frames)['recommendations'][0]['activation_ready']
    steps[-1]['evidence_type'] = 'inferred'
    assert not analyze.finalize(proposal(pairs), steps, frames)['recommendations'][0]['activation_ready']
    with pytest.raises(ValueError, match='unreviewed screenshot'):
        analyze.finalize(proposal([{'source_frame_id': 'f0000', 'result_frame_id': 'f9999'}]), steps, frames)


@pytest.mark.parametrize('result', ['success', 'changed', 'uncertain'])
def test_acceptance_review_controls_both_writes(sandbox, monkeypatch, result):
    start = datetime.now(timezone.utc).timestamp() + 7200
    plan = {'kind': 'accept', 'title': 'Meeting', 'start': start, 'end': start + 1800, 'timezone': 'UTC'}
    run = automation_store.begin_run('Meeting test')
    automation_store.record_draft('draft-one', '99', run_id=run, original_id=7, original_message_id='fake', meeting=plan)
    calls = []

    async def native(command, **values):
        calls.append(command)
        if command == 'draft_preview':
            return {'revision': 'b' * 64, 'body': 'Works for me', 'to': ['fake@example.test']}
        if command == 'message':
            return {'message_id': 'fake', 'body': 'Can we meet?'}
        if command == 'calendar_targets':
            return {'calendars': [{'id': 'calendar-a', 'title': 'Sandbox'}], 'default_id': 'calendar-a'}
        if command == 'check_event':
            return {'available': result != 'changed'}
        if command == 'send_draft':
            return {'accepted': True}
        if command == 'create_event':
            assert values['start'] == plan['start'] and values['calendar_id'] == 'calendar-a'
            if result == 'uncertain':
                raise mac_automation.MacAutomationError('native_timeout', 'Response lost')
            return {'event_id': 'mock-event'}
        if command == 'find_event':
            return {'event_id': 'mock-event'}
        raise AssertionError(command)

    monkeypatch.setattr(mac_automation, 'call', native)
    with TestClient(main.app) as client:
        route = '/api/drafts/draft-one'
        shown = client.get(route).json()
        stamp = shown['calendars'][0]['event_revision']
        assert shown['meeting'] == plan and 'send_draft' not in calls and 'create_event' not in calls
        approval = {'revision': 'b' * 64, 'confirmed': True, 'calendar_id': 'calendar-a', 'event_revision': stamp}
        assert client.post(route + '/send', json={**approval, 'calendar_id': 'calendar-b'}).status_code == 409
        assert 'send_draft' not in calls
        response = client.post(route + '/send', json=approval)
        if result == 'changed':
            assert response.status_code == 409 and 'send_draft' not in calls and 'create_event' not in calls
            return
        if result == 'uncertain':
            assert response.json()['status'] == 'calendar_attention'
            assert client.post(route + '/calendar', json={'event_revision': stamp, 'confirmed': True}).json()['event_created']
        else:
            assert response.json()['event_created']
        assert client.post(route + '/send', json=approval).json()['status'] == 'sent'
        assert calls.count('send_draft') == calls.count('create_event') == 1
