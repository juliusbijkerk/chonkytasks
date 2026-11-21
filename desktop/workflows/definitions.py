"""Registered workflow definitions; imported after worker authentication is loaded."""
from datetime import timedelta
import mistralai.workflows as workflows


@workflows.activity(name='ai-discovery-scan-meeting-requests-v1',
                    start_to_close_timeout=timedelta(minutes=20), retry_policy_max_attempts=1)
async def scan_meeting_requests() -> dict:
    # Native and HTTP dependencies belong to the activity, outside the
    # deterministic workflow sandbox.
    from app.automations import scan_due_workflows
    return await scan_due_workflows()


@workflows.workflow.define(name='ai-discovery-meeting-replies-v1',
    workflow_display_name='ChonkyTasks workflow checks',
    workflow_description='Check enabled ChonkyTasks workflows and prepare results for human review on this Mac.')
class MeetingReplies:
    @workflows.workflow.entrypoint
    async def run(self, automation_id: str) -> dict:
        if automation_id != 'meeting-reply-drafts':
            raise ValueError('Unknown automation')
        return await scan_meeting_requests()

