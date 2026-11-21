import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import time

from fastapi.testclient import TestClient
import pytest

from app import analyze, automation_store, draft_review, mac_automation, main, meeting_automation
from app.models import Observations, Proposal


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
    monkeypatch.setenv('OPENROUTER_API_KEY', 'fake-key-unused-by-mocked-transport')
    calls = []
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

    async def provider(content, schema, *_args, **_kwargs):
        calls.append(schema)
        blob = json.dumps(content)
        assert 'safe context' in blob
        assert all(secret not in blob for secret in ['unredacted-secret', 'unreviewed context', 'local-file-secret', 'f0002'])
        if schema is Observations:
            urls = [part['image_url']['url'] for part in content if part['type'] == 'image_url']
            assert [base64.b64decode(url.split(',')[1]) for url in urls] == [images['f0001'], images['f0003']]
            return Observations.model_validate({'limitations': [], 'steps': [
                {'title': 'View', 'application': 'Mail', 'description': 'Message visible', 'evidence_type': 'observed',
                 'confidence': 'high', 'uncertainty': '', 'start_frame_id': id, 'end_frame_id': id}
                for id in ('f0001', 'f0003')]})
        return Proposal.model_validate({'title': 'Reviewed test', 'summary': 'Visible activity', 'limitations': [],
            'recommendations': [], 'workflow': {'name': 'Review', 'description': '', 'occurrences': [], 'repetition_assessment': ''}})

    monkeypatch.setattr(analyze, 'complete', provider)
    root = f'/api/jobs/{job["id"]}'
    with TestClient(main.app) as client:
        chosen = {'frame_ids': ['f0003', 'f0001'], 'context': 'safe context'}
        packet = client.post(root + '/privacy/preview?exact=true', json=chosen).json()
        assert [frame['id'] for frame in packet['frames']] == ['f0001', 'f0003'] and not calls
        assert client.get(root + '/redacted/f0001.jpg').content == images['f0001']
        approved = {**chosen, 'revision': packet['revision'], 'confirmed': True}
        assert client.post(root + '/privacy/approve', json={**approved, 'context': 'changes'}).status_code == 409
        assert client.post(root + '/privacy/approve', json={**approved, 'confirmed': False}).status_code == 422
        (redacted / 'f0001.jpg').write_bytes(b'\xff\xd8changed\xff\xd9')
        assert client.post(root + '/privacy/approve', json=approved).status_code == 409
        assert not calls
        (redacted / 'f0001.jpg').write_bytes(images['f0001'])
        assert client.post(root + '/privacy/approve', json=approved).status_code == 202
        completed = until(client, root, 'completed')
        assert completed['report']['excluded_frame_ids'] == ['f0002']
        assert calls == [Observations, Proposal]


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
            assert local['report']['recommendations'][0]['activation_ready']
            assert all(example['source'] < example['result'] for kind in kinds for example in kind['examples'])
        else:
            assert kinds == local['report']['recommendations'] == []


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
