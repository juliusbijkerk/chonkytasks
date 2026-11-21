"""A narrow JSON bridge to the signed macOS helper; no shell or generated scripts."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

DEFAULT_HELPER = Path.home() / 'Applications/chonkytasks.app/Contents/Library/LoginItems/chonkytasks Automations.app/Contents/MacOS/AutomationHelper'
HELPER = Path(os.environ.get('CHONKYTASKS_AUTOMATION_HELPER', str(DEFAULT_HELPER)))
ERRORS = {
    'mail_permission': 'Allow chonkytasks Automations to control Mail in System Settings → Privacy & Security → Automation.',
    'calendar_permission': 'Allow calendar access in System Settings → Privacy & Security → Calendars.',
    'calendar_conflict': 'The proposed time is no longer free.',
    'invalid_calendar': 'The chosen calendar is unavailable or read-only.',
    'invalid_event': 'The approved event is invalid or has already started.',
    'inbox_backlog': 'There are too many pending messages to scan at once. No messages have been skipped.',
    'mail_error': 'Mail could not complete this action. Open Mail and check that your account is connected.',
    'keychain_error': 'The macOS Keychain could not be accessed. Unlock your login keychain and retry.',
    'native_timeout': 'Mail did not respond in time. Open Mail and check that it is responding, then resume.',
    'mail_timeout': 'Mail took too long to read the Inbox. Open Mail and let it finish syncing, then resume.',
    'draft_changed': 'This draft changed in Mail. Reopen the preview before sending.',
    'mail_error_5004': 'This draft changed or is no longer available. Reopen the preview before sending.',
    'mail_error_5005': 'This draft has attachments. Send it from Mail to preserve them.',
}


class MacAutomationError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


async def call(operation, **values):
    if sys.platform != 'darwin' or not HELPER.is_file():
        raise ValueError('Open the latest chonkytasks macOS app to connect Mail and Calendar.')
    # LaunchServices gives this app its own privacy identity, consistently from
    # both the UI and launchd. A raw subprocess inherits the Python host's TCC
    # identity, preventing prompts and losing access when the UI closes.
    # Private, short-lived files carry JSON; secrets never enter command arguments.
    with tempfile.TemporaryDirectory(prefix='ai-discovery-bridge-') as folder:
        request, response = Path(folder) / 'input', Path(folder) / 'output'
        request.touch(mode=0o600)
        response.touch(mode=0o600)
        request.write_text(json.dumps({'operation': operation, **values}))
        process = await asyncio.create_subprocess_exec('/usr/bin/open', '-n', '-g', '-W',
            '--stdin', str(request), '--stdout', str(response), '--stderr', '/dev/null',
            '-a', str(HELPER.parents[2]), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(process.wait(), timeout=180 if operation == 'permissions' else 75)
        except asyncio.TimeoutError:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise MacAutomationError('native_timeout', 'The macOS connection timed out.') from None
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        stdout = response.read_bytes()
    try:
        result = json.loads(stdout)
    except ValueError:
        raise ValueError('The macOS connection did not respond. Reopen chonkytasks and retry.') from None
    if result.get('error'):
        code = result['error']
        message = ERRORS.get(code, ERRORS['mail_error'] if code.startswith('mail_error_') else 'The macOS connection could not complete this action.')
        raise MacAutomationError(code, message)
    return result


async def api_key():
    saved = (await call('get_key'))['value'] if HELPER.exists() else ''
    return saved or os.getenv('MISTRAL_API_KEY', '').strip()


async def key_configured():
    if os.getenv('MISTRAL_API_KEY', '').strip():
        return True
    if not HELPER.exists():
        return False
    return (await call('key_status'))['configured']
