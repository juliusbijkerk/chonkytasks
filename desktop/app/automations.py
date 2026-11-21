"""Activation and status for explicitly supported, versioned workflow recipes."""
import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import plistlib
import time

import httpx

from . import automation_store as storage, mac_automation as mac

WORKFLOW_NAME = 'ai-discovery-meeting-replies-v1'
CHECK_INTERVAL_SECONDS = 30
LABEL = 'com.mistralhackathon.aidiscovery.workflows'
PYTHON = storage.ROOT / '.workflows-venv/bin/python'
LAUNCH_AGENT = Path.home() / f'Library/LaunchAgents/{LABEL}.plist'
LOCK = asyncio.Lock()
REMOTE_CACHE = None


def supported(recommendation):
    if recommendation.get('automation_recipe') == 'meeting_reply_drafts':
        return True
    # Compatibility for the existing report, without a paid re-analysis.
    return recommendation.get('title', '').casefold().strip() == 'draft meeting replies from calendar availability'


def deployment_name():
    return 'ai-discovery-' + storage.installation_id()


def schedule_id():
    return deployment_name() + '-meeting-replies'


async def remote(method, path, *, key=None, allow_missing=False, **kwargs):
    key = key or await mac.api_key()
    if not key:
        raise ValueError('Connect Mistral Studio to activate this automation.')
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            result = await client.request(method, 'https://api.mistral.ai/v1' + path,
                                          headers={'Authorization': f'Bearer {key}'}, **kwargs)
    except httpx.RequestError:
        raise ValueError('Mistral could not be reached. Check your connection and retry.') from None
    if allow_missing and result.status_code == 404:
        return None
    if result.status_code not in (200, 201, 202, 204):
        messages = {401: 'This Mistral key was rejected. Check the key in Connections.',
            403: 'This key does not have Workflows access. Enable Workflows in Mistral Studio.',
            402: 'Your Mistral account needs credits before this automation can run.',
            429: 'Mistral is busy. Try again shortly.'}
        raise ValueError(messages.get(result.status_code, 'Mistral could not confirm this change. Your automation is not active; retry.'))
    if not result.content:
        return {}
    try:
        return result.json()
    except ValueError:
        raise ValueError('Mistral returned an unreadable status. Retry shortly.') from None


async def connect(key):
    if storage.get().get('enabled'):
        raise ValueError('Pause the automation before changing its Mistral connection.')
    if key.startswith('sk-or-'):
        raise ValueError('Use a Mistral Studio API key here. An OpenRouter key cannot activate Workflows.')
    # Verifies Workflows access, not merely access to an inference model.
    await remote('GET', '/workflows/workers/whoami', key=key)
    await mac.call('save_key', value=key)
    clear_cache()
    return {'connected': True}


def clear_cache():
    global REMOTE_CACHE
    REMOTE_CACHE = None


async def settings():
    try:
        configured = await mac.key_configured()
        permissions = await mac.call('status') if mac.HELPER.exists() else {'mail': False, 'calendar': False}
        return {'connected': configured, 'mail': permissions['mail'], 'calendar': permissions['calendar'],
                'calendar_status': permissions.get('calendar_status', 'not_determined'),
                'runtime_ready': PYTHON.exists() and mac.HELPER.exists(), 'message': ''}
    except ValueError as error:
        return {'connected': False, 'mail': False, 'calendar': False, 'runtime_ready': False, 'message': str(error)}


async def start_worker():
    if not PYTHON.is_file() or not mac.HELPER.is_file():
        raise ValueError('The background service is missing. Rebuild chonkytasks with its Workflows runtime.')
    log = Path.home() / 'Library/Logs/AI Discovery/workflows.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
    payload = {'Label': LABEL, 'ProgramArguments': [str(PYTHON), '-m', 'workflows.worker'],
        'WorkingDirectory': str(storage.ROOT), 'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 60,
        # launchd's Background tier throttles Python enough to exceed Temporal's
        # workflow-task deadlines. Standard still runs without a window but keeps
        # normal CPU/IO scheduling for the worker and its heartbeats.
        'ProcessType': 'Standard', 'EnvironmentVariables': {'DEPLOYMENT_NAME': deployment_name(), 'PYTHONUNBUFFERED': '1'},
        'StandardOutPath': str(log), 'StandardErrorPath': str(log)}
    domain = f'gui/{os.getuid()}'
    if LAUNCH_AGENT.exists() and plistlib.loads(LAUNCH_AGENT.read_bytes()) == payload:
        check = await asyncio.create_subprocess_exec('/bin/launchctl', 'print', domain + '/' + LABEL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        output, _ = await check.communicate()
        if check.returncode == 0 and b'state = running' in output:
            deployment = await remote('GET', '/workflows/deployments/' + deployment_name(),
                allow_missing=True, params={'workflow_name': WORKFLOW_NAME})
            if deployment and deployment.get('is_active') and deployment.get('active_worker_count', 0) > 0:
                # Reuse a healthy registered worker. Restarting it interrupts its
                # registration and can make repeated Activate clicks stall forever.
                heartbeat = deployment.get('last_heartbeat')
                if heartbeat:
                    return datetime.fromisoformat(heartbeat.replace('Z', '+00:00'))
            state = storage.get()
            started = state.get('worker_started_at')
            if started and 'error' not in state.get('worker_phase', '').lower():
                timestamp = datetime.fromisoformat(started)
                if datetime.now(timezone.utc) - timestamp < timedelta(seconds=120):
                    return timestamp
    LAUNCH_AGENT.write_bytes(plistlib.dumps(payload))
    LAUNCH_AGENT.chmod(0o600)
    # Replacing this app's own worker is idempotent. Credentials are read from
    # Keychain in the child process, never embedded in the plist or argv.
    stop = await asyncio.create_subprocess_exec('/bin/launchctl', 'bootout', domain + '/' + LABEL,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await stop.wait()
    start = await asyncio.create_subprocess_exec('/bin/launchctl', 'bootstrap', domain, str(LAUNCH_AGENT),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    if await start.wait() != 0:
        raise ValueError('macOS could not start the background service. Check Login Items in System Settings and retry.')
    return datetime.now(timezone.utc)


async def wait_for_worker(started_at):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        result = await remote('GET', '/workflows/deployments/' + deployment_name(), allow_missing=True,
                              params={'workflow_name': WORKFLOW_NAME})
        if result and result.get('is_active') and result.get('active_worker_count', 0) > 0:
            heartbeat = result.get('last_heartbeat')
            if heartbeat and datetime.fromisoformat(heartbeat.replace('Z', '+00:00')) >= started_at:
                return
        state = storage.get()
        if state.get('worker_phase') and state.get('status') == 'activating':
            storage.update(message=state['worker_phase'])
        await asyncio.sleep(2)
    phase = storage.get().get('worker_phase', 'Connecting to Mistral')
    raise ValueError(f'{phase}. The connection is taking longer than expected. Retry to continue setup.')


def schedule_payload():
    return {'schedule_id': schedule_id(), 'workflow_identifier': WORKFLOW_NAME, 'deployment_name': deployment_name(),
        'schedule': {'input': {'automation_id': storage.AUTOMATION_ID}, 'intervals': [{'every': f'PT{CHECK_INTERVAL_SECONDS}S'}],
            'start_at': datetime.now(timezone.utc).isoformat(),
            'policy': {'overlap': 1, 'catchup_window_seconds': 300, 'pause_on_failure': True}}}


def check_schedule(result):
    if result.get('workflow_name') != WORKFLOW_NAME or result.get('deployment_name') != deployment_name():
        raise ValueError('This schedule belongs to a different workflow. Activation stopped.')


async def activate(job, index):
    async with LOCK:
        recommendations = job.get('report', {}).get('recommendations', [])
        if index < 0 or index >= len(recommendations) or not supported(recommendations[index]):
            raise ValueError('This suggestion does not yet have an executable automation.')
        if not await mac.key_configured():
            raise ValueError('Connect Mistral Studio to activate this automation.')
        permissions = await mac.call('status')
        if not permissions['mail'] or not permissions['calendar']:
            raise ValueError('Connect Mail and Calendar before activating this automation.')
        state = storage.get()
        if state.get('enabled'):
            return state
        storage.update(status='activating', enabled=False, message='Starting the background service',
                       source_job_id=job['id'], title=recommendations[index]['title'],
                       # Keep the original inbox boundary across pauses, errors,
                       # and slow setup. Resuming must not discard pending mail.
                       watch_from=state.get('watch_from') or storage.now())
        clear_cache()
        try:
            await remote('GET', '/workflows/workers/whoami')
            started_at = await start_worker()
            await wait_for_worker(started_at)
            storage.update(message='Scheduling future checks')
            existing = await remote('GET', '/workflows/schedules/' + schedule_id(), allow_missing=True)
            if existing:
                check_schedule(existing)
                await remote('PATCH', '/workflows/schedules/' + schedule_id(),
                    json={'schedule': {'intervals': [{'every': f'PT{CHECK_INTERVAL_SECONDS}S'}]}})
                if existing.get('paused'):
                    await remote('POST', '/workflows/schedules/' + schedule_id() + '/resume', json={'note': 'Resumed in chonkytasks'})
            else:
                await remote('POST', '/workflows/schedules', json=schedule_payload())
            confirmed = await remote('GET', '/workflows/schedules/' + schedule_id())
            check_schedule(confirmed)
            if confirmed.get('paused', True):
                raise ValueError('Mistral has not activated the schedule. Retry shortly.')
            return storage.update(status='active', enabled=True, message='', schedule_id=schedule_id(),
                deployment_name=deployment_name(), activated_at=state.get('activated_at') or storage.now(),
                check_interval_seconds=CHECK_INTERVAL_SECONDS,
                next_run=(confirmed.get('future_executions') or [])[:1])
        except asyncio.CancelledError:
            storage.update(status='error', enabled=False, message='Setup was interrupted. Activate again to finish it.')
            raise
        except Exception as error:
            message = str(error) if isinstance(error, ValueError) else 'Activation could not finish. Retry from chonkytasks.'
            storage.update(status='error', enabled=False, message=message)
            raise ValueError(message) from None
        finally:
            clear_cache()


async def pause():
    async with LOCK:
        # Stop local writes even if the remote pause request cannot be delivered.
        storage.update(enabled=False, status='pausing', message='Pausing future checks')
        clear_cache()
        try:
            existing = await remote('GET', '/workflows/schedules/' + schedule_id(), allow_missing=True)
            if existing:
                check_schedule(existing)
                await remote('POST', '/workflows/schedules/' + schedule_id() + '/pause', json={'note': 'Paused in chonkytasks'})
                confirmed = await remote('GET', '/workflows/schedules/' + schedule_id())
                if not confirmed.get('paused'):
                    raise ValueError('Mistral has not confirmed the pause.')
            return storage.update(status='paused', enabled=False, message='')
        except ValueError:
            return storage.update(status='paused_local', enabled=False,
                message='Paused on this Mac. Reconnect to confirm the pause in Mistral.')


async def status():
    global REMOTE_CACHE
    state = storage.get()
    if not state.get('enabled'):
        return state
    if REMOTE_CACHE and time.monotonic() - REMOTE_CACHE[0] < 15:
        return {**state, **REMOTE_CACHE[1]}
    try:
        schedule = await remote('GET', '/workflows/schedules/' + schedule_id(), allow_missing=True)
        deployment = await remote('GET', '/workflows/deployments/' + deployment_name(), allow_missing=True,
                                  params={'workflow_name': WORKFLOW_NAME})
        if not schedule:
            return storage.update(status='error', enabled=False, message='The Mistral schedule was removed. Activate again to restore it.')
        check_schedule(schedule)
        if schedule.get('paused'):
            return storage.update(status='paused', enabled=False, message='Paused in Mistral Studio.')
        verified = {'status': 'active' if deployment and deployment.get('is_active') else 'offline',
                    'message': '' if deployment and deployment.get('is_active') else 'Waiting for the background service on this Mac.',
                    'next_run': (schedule.get('future_executions') or [])[:1]}
    except ValueError:
        verified = {'status': 'unverified', 'message': 'Cannot verify Mistral right now. Last saved activation is preserved.'}
    REMOTE_CACHE = (time.monotonic(), verified)
    return {**state, **verified}
