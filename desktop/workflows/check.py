"""Validate the installed SDK contract without network access or Mac permissions."""
import asyncio
from mistralai.client.models import ScheduleDefinition
from mistralai.workflows.core.sandbox import get_sandbox_restrictions
from temporalio.workflow import _Definition
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner
from workflows.definitions import MeetingReplies

async def main():
    SandboxedWorkflowRunner(restrictions=get_sandbox_restrictions()).prepare_workflow(
        _Definition.must_from_class(MeetingReplies))
    ScheduleDefinition.model_validate({'input': {'automation_id': 'meeting-reply-drafts'},
        'intervals': [{'every': 'PT30S'}],
        'policy': {'overlap': 1, 'catchup_window_seconds': 300, 'pause_on_failure': True}})
    print('Workflows sandbox and schedule schema validated. No network requests made.')

if __name__ == '__main__':
    asyncio.run(main())
