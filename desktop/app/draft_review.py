"""Review live Mail drafts; sending requires a current, explicit UI decision."""
import logging
import hashlib
import json
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


def accepting(record):
    return (record.get('meeting') or {}).get('kind') == 'accept'


def event_revision(record, calendar_id):
    return hashlib.sha256(json.dumps([record['id'], calendar_id, record['meeting']], sort_keys=True).encode()).hexdigest()


def event_arguments(record, calendar_id):
    return {**record['meeting'], 'calendar_id': calendar_id,
            'token': hashlib.sha256(record['id'].encode()).hexdigest()}


def delivery_status(record):
    if accepting(record) and record['event_state'] != 'created':
        return {'status': 'calendar_attention', 'event_state': record['event_state'],
                'meeting': record['meeting'], 'calendar_id': record['calendar_id'],
                'event_revision': event_revision(record, record['calendar_id'])}
    return {'status': 'sent', **({'event_created': True} if accepting(record) else {})}


async def preview(draft_id):
    record = get(draft_id)
    if record['status'] == 'sent':
        return {'id': draft_id, **delivery_status(record)}
    if record['status'] != 'ready':
        return {'id': draft_id, 'status': record['status']}
    try:
        draft = await mac.call('draft_preview', id=int(record['draft_id']))
        original = await mac.call('message', id=record['original_id'])
        if original['message_id'] != record['original_message_id']:
            raise ReviewError('The original email is no longer available in the Inbox.')
        proposal = {'id': draft_id, 'status': 'ready', 'draft': draft, 'original': original,
                    'meeting': record['meeting']}
        if accepting(record):
            calendars = await mac.call('calendar_targets')
            proposal['calendars'] = [{**item, 'event_revision': event_revision(record, item['id'])}
                                     for item in calendars['calendars']]
            proposal['default_calendar'] = calendars.get('default_id')
        return proposal
    except ValueError as error:
        raise ReviewError(str(error)) from None


async def send(draft_id, revision, calendar_id=None, reviewed_event=None):
    record = get(draft_id)
    if record['status'] == 'sent':
        return delivery_status(record)
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
    if accepting(record):
        if not calendar_id or reviewed_event != event_revision(record, calendar_id):
            raise ReviewError('Review the proposed event and choose a calendar before approving.')
        try:
            availability = await mac.call('check_event', **event_arguments(record, calendar_id))
        except ValueError as error:
            raise ReviewError(str(error)) from None
        if not availability.get('available'):
            raise ReviewError('This time is no longer free. The reply is still in Drafts.')
        claimed = storage.claim_delivery(draft_id, calendar_id)
    else:
        claimed = storage.draft_status(draft_id, 'sending', expected='ready')
    if not claimed:
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
    if accepting(record):
        return await finish_calendar(draft_id, reviewed_event)
    return delivery_status(get(draft_id))


async def finish_calendar(draft_id, reviewed_event):
    record = get(draft_id)
    if record['status'] != 'sent' or not accepting(record) or not record['calendar_id']:
        raise ReviewError('Approve the reply and calendar event first.')
    calendar_id = record['calendar_id']
    if reviewed_event != event_revision(record, calendar_id):
        raise ReviewError('The calendar action changed. Review it again.')
    state = record['event_state']
    args = event_arguments(record, calendar_id)
    if state == 'uncertain':
        # A lost response may follow a successful save. Only verify the event.
        try:
            found = await mac.call('find_event', **args)
        except ValueError:
            return delivery_status(record)
        if found.get('event_id'):
            storage.calendar_status(draft_id, 'created', expected='uncertain', event_id=found['event_id'])
        return delivery_status(get(draft_id))
    if state not in ('pending', 'conflict') or not storage.calendar_status(draft_id, 'creating', expected=state):
        return delivery_status(get(draft_id))
    try:
        result = await mac.call('create_event', **args)
        if not result.get('event_id'):
            raise ValueError('Calendar save unconfirmed')
        storage.calendar_status(draft_id, 'created', expected='creating', event_id=result['event_id'])
    except Exception as error:
        logging.getLogger(__name__).warning('Calendar event could not be confirmed (%s)', getattr(error, 'code', type(error).__name__))
        safe_failure = isinstance(error, mac.MacAutomationError) and error.code in (
            'calendar_conflict', 'calendar_permission', 'invalid_calendar', 'invalid_event')
        storage.calendar_status(draft_id, 'conflict' if safe_failure else 'uncertain', expected='creating')
    return delivery_status(get(draft_id))
