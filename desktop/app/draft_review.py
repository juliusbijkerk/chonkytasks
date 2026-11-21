"""Review live Mail drafts; sending requires a current, explicit UI decision."""
import logging
from . import automation_store as storage, mac_automation as mac


class ReviewError(ValueError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def get(draft_id):
    record = storage.get_draft(draft_id)
    if not record:
        raise ReviewError('Draft not found.', 404)
    return record


async def preview(draft_id):
    record = get(draft_id)
    if record['status'] != 'ready':
        return {'id': draft_id, 'status': record['status']}
    try:
        draft = await mac.call('draft_preview', id=int(record['draft_id']))
        original = await mac.call('message', id=record['original_id'])
        if original['message_id'] != record['original_message_id']:
            raise ReviewError('The original email is no longer available in the Inbox.')
        return {'id': draft_id, 'status': 'ready', 'draft': draft, 'original': original}
    except ValueError as error:
        raise ReviewError(str(error)) from None


async def send(draft_id, revision):
    record = get(draft_id)
    if record['status'] == 'sent':
        return {'status': 'sent'}
    if record['status'] != 'ready':
        raise ReviewError('This reply may already have been sent. Check Mail before taking further action.')
    # Preflight before claiming the send; another click can race this read but
    # only one caller can atomically claim the pending draft below.
    try:
        current = await mac.call('draft_preview', id=int(record['draft_id']))
    except ValueError as error:
        raise ReviewError(str(error)) from None
    if current['revision'] != revision:
        raise ReviewError('This draft changed in Mail. Reopen the preview before sending.')
    if current.get('attachments'):
        raise ReviewError('Send this draft from Mail to preserve its attachments.')
    if not current.get('to'):
        raise ReviewError('The draft has no recipient. Add one in Mail, then reopen the preview.')
    if not storage.draft_status(draft_id, 'sending', expected='ready'):
        raise ReviewError('This reply is already being sent.')
    try:
        result = await mac.call('send_draft', id=int(record['draft_id']), revision=revision,
            original_id=record['original_id'], original_message_id=record['original_message_id'])
        if not result.get('accepted'):
            raise ValueError('Mail did not confirm sending.')
    except Exception as error:
        logging.getLogger(__name__).warning('Mail send could not be confirmed (%s)', getattr(error, 'code', type(error).__name__))
        # A lost response never triggers a second send. Only a native revision
        # rejection, before creating or sending anything, is safe to retry.
        changed = isinstance(error, mac.MacAutomationError) and error.code == 'draft_changed'
        storage.draft_status(draft_id, 'ready' if changed else 'send_uncertain', expected='sending')
        raise ReviewError(str(error) if changed else 'Mail could not confirm sending. Check Sent and Outbox in Mail; this reply will not be sent again automatically.') from None
    storage.draft_status(draft_id, 'sent', expected='sending')
    return {'status': 'sent'}
