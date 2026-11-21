"""Launched independently of the UI by this app's macOS LaunchAgent."""
import asyncio
import logging
import os

from dotenv import load_dotenv

from app import automation_store as storage, mac_automation as mac


def progress(message):
    storage.update(worker_phase=message, worker_updated_at=storage.now())
    print(message, flush=True)


class StartupProgress(logging.Handler):
    stages = {
        'Worker runtime config resolved': 'Mistral account verified',
        'creating temporal service client': 'Connecting to the Mistral scheduler',
        'connected to temporal service client': 'Registering the automation with Mistral',
        'Registered activities': 'Validating the background task',
        'Starting Temporal worker': 'Background service connected',
    }

    def emit(self, record):
        # Log fixed stage names only, never SDK configuration or credentials.
        text = record.getMessage()
        for prefix, label in self.stages.items():
            if text.startswith(prefix):
                progress(label)
                return
        if record.levelno >= logging.ERROR:
            progress('The background service encountered an error. Retry activation.')


async def main():
    storage.update(worker_started_at=storage.now())
    progress('Loading the saved Mistral connection')
    load_dotenv(storage.ROOT / '.env')
    key = await mac.api_key()
    if not key:
        raise ValueError('Mistral is not connected')
    os.environ['MISTRAL_API_KEY'] = key
    # The SDK builds its discovery configuration at import time.
    # Load the Keychain credential first; run_worker(api_key=...) alone does
    # not set the discovery token in SDK 3.20.
    import mistralai.workflows as workflows
    from workflows.definitions import MeetingReplies
    for name in ('mistralai.workflows.core.worker', 'mistralai.workflows.core.config.config_discovery',
                 'mistralai.workflows.core.temporal.temporal_client'):
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        logger.addHandler(StartupProgress())
        logger.propagate = False
    progress('Verifying the Mistral connection')
    await workflows.run_worker([MeetingReplies], api_key=key)


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception:
        # Do not put API payloads, message text, or credentials into launchd logs.
        print('The background service could not connect. Check Connections in chonkytasks.', flush=True)
        raise SystemExit(1)
