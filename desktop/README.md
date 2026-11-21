# chonkytasks

A local MVP that turns a screen recording into an evidence-backed automation proposal. Built from [the project brief](project-description.md).

## macOS app

Open **chonkytasks.app** in your home folder's **Applications** folder (or the shortcut at **dist/chonkytasks.app**). The app starts its own local server and keeps the same minimal recording-and-results interface. Quit with ⌘Q to stop the server.

To rebuild on macOS 13 or later, complete the Python/FFmpeg setup below and install Apple's Command Line Tools (`xcode-select --install`), then run:

```bash
bash macos/setup_workflows.sh
bash macos/build.sh
open "dist/chonkytasks.app"
```

The build uses the project's `.venv`, signs the app in a temporary folder outside synced Documents, and installs it to `~/Applications/chonkytasks.app` with a shortcut in `dist/`. The installed signature is verified before finishing. Keeping the bundle outside synced Documents prevents sync metadata from invalidating it later. Finder launches include the standard Homebrew tool locations, so FFmpeg works without a terminal shell profile.

Builds reuse the **chonkytasks Local Development** signing identity in the login Keychain. Its public fingerprint is pinned in `~/Library/Application Support/chonkytasks/signing/identity.sha1`; the private key stays in Keychain and is available to `codesign`. Do not delete that identity between builds. Set `CHONKYTASKS_SIGNING_IDENTITY` to use your own persistent signing certificate. The build refuses ad-hoc signing because its identity changes with each native build, causing macOS to forget privacy grants ([Apple's explanation](https://developer.apple.com/documentation/technotes/tn3127-inside-code-signing-requirements)). This local certificate does not change system trust settings or provide notarization for distribution.

After switching from the old ad-hoc build, macOS may request each required permission once more. Allow it for the installed chonkytasks app; subsequent builds use the same identity. Documents, Desktop, Mail, Calendar, and Keychain permissions are separate macOS controls. Close old AI Discovery instances so two differently signed builds do not compete for the same permission record.

This is a local launcher: keep the project folder and its `.venv` available. The app remembers the project location; rebuild after moving the project, keep the app in the project's `dist/` folder, or set `CHONKYTASKS_HOME`. The older `AI_DISCOVERY_HOME` and `FLOWLENS_HOME` overrides remain supported. Startup details remain at `~/Library/Logs/AI Discovery/server.log`.

The interface uses the Mistral orange/yellow/red palette, glass surfaces, and pixel-cat branding from [chonkytasks](https://github.com/juliusbijkerk/chonkytasks/tree/main/ui), with its MIT license preserved in `static/brand/LICENSE`. The existing sidebar, recordings library, reports, automations, and activity flows remain in place. The app opens in the light Mistral appearance, including when macOS uses dark mode or an older dark preference was saved; the toolbar has no appearance controls. The sidebar mascot hops on hover, and running email checks show a spinning mascot with randomly varied speed. Confirmed reply sends finish with a brief particle celebration that dismisses automatically, on click, or with Escape. Animations respect Reduce Motion. macOS bundle identifiers, Keychain service names, workflow identifiers, stored data, and log paths retain their previous values so the rename does not create new automation identities.

## Run

Requires Python 3.11+ for the pinned setup below and FFmpeg (`brew install ffmpeg` on macOS).

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
cp -n .env.example .env  # preserve an existing .env
# Set OPENROUTER_API_KEY and OPENROUTER_MODEL in .env
bash scripts/start.sh
```

Open **http://127.0.0.1:8000**. The existing `.env` in this workspace is already configured. Never overwrite it or commit it. To use another local port: `PORT=8001 bash scripts/start.sh`.

## Use

1. Open **New recording**, optionally describe the task, and select **Start recording**. Choose a screen or window using the system picker.
2. Work as usual, then select **Stop & analyze**. A stop control remains in the toolbar while browsing other pages. The completed video is saved and analyzed automatically.
3. Use the sidebar to switch between a new recording, the searchable recordings list, and recent analyses.
4. Review Summary, Analysis steps, and Automations. Click a screenshot or numbered step to seek the video and see its description, application, timing, and evidence. Playback follows the current step. Returns to a previous view are shown separately, including in older reports.
5. Export the report as Markdown. JSON is available from `/api/jobs/<job-id>/export/json`. Reports remain at their `/#job=…` link after restarting the server.
6. Delete a finished recording from the sidebar, recordings list, or report toolbar. Confirming removes the stored copy and report; the original video is preserved. In-progress recordings cannot be deleted.
7. Use the pencil button to rename a recording or add notes, including while analysis is running. Names and notes are saved locally, searchable, and included in exports. They do not change the source file or the context sent for analysis.

Direct recording in the Mac app uses ScreenCaptureKit and requires macOS 15 or later. The system picker controls which screen or window is shared; microphone and system audio are off. Recording stops automatically before the 15-minute or 500 MB analysis limits. A finished native capture is retained locally if analysis cannot start, and can be retried without creating duplicate analyses. Existing recordings are preserved. In a supported desktop browser, screen recording uses `getDisplayMedia` and `MediaRecorder`; keep the page open until the video has been accepted for analysis. Importing an existing video remains available through the Mac app’s **Open recording…** menu or Dock drop.

During analysis, the progress bar animates and shows elapsed time. Four stages show preparation, screenshot reading, writing results, and validation. Screenshot counts are separate from batch counts; the current batch has a thumbnail preview. The percentage advances with completed stages and batches, not predicted model completion time.

Model requests stream through OpenRouter. The app shows waiting/receiving/checking/retrying states, elapsed request time, stream update counts, and live output token estimates (marked ≈). Estimates use received text length, not stream chunk count; the final provider usage replaces the estimate with its native token total. Streamed reasoning contributes to the estimate but its text is discarded. Raw response and reasoning text are never sent to the progress UI. Progress updates reach the app through server-sent events with reconnection support.

For meaningful repetition detection, record the same business task 2–3 times over roughly five minutes. Short clips are supported, with explicit evidence limitations. Limits: 500 MB and 130 seconds per recording. Audio is not analyzed; the video preview is silent.

## Mistral-only pipeline

- FFmpeg makes a browser-compatible preview and samples screenshots with their actual presentation timestamps. Pillow/NumPy compare pixels to remove consecutive near-duplicates while retaining revisits and periodic coverage. These operations are deterministic; they use no AI models.
- Up to 72 selected screenshots, at up to 1920 px width, go to the configured Mistral vision model through OpenRouter in batches of 12. The model reconstructs visible activity, uncertainty, and evidence references.
- Each batch must account for every screenshot exactly once in chronological ranges. Missing screenshots or overlapping steps get one correction attempt; incomplete coverage is rejected rather than silently omitted. Older saved reports are displayed with explicit screenshot-to-step links and marked gaps, without another model call.
- A second call to the same Mistral model identifies recurring workflows and proposes automations. Pydantic validates the response; the server validates references and derives elapsed spans from screenshot timestamps. Overlapping repeated instances are rejected.
- `OPENROUTER_MODEL` defaults to `mistralai/mistral-large-4-0`. Non-`mistralai/` model IDs are rejected. Provider routing can change the host serving the same Mistral model, never the model family. There is no fallback to another vendor's model.

API integration references: [OpenRouter model catalog](https://openrouter.ai/api/v1/models), [Mistral vision documentation](https://docs.mistral.ai/inference/conversations/vision). The configured model must support both image input and JSON output.

## Storage and scope

The server binds to loopback only. The API key stays on the server. Videos, derived screenshots, reports, and context are stored locally in ignored `data/jobs/`. Selected screenshots, supplied context, and derived observations are sent to OpenRouter/Mistral for processing. No raw recording is uploaded to those services. API calls consume your OpenRouter credits.

This is a single-user local application, not an authenticated deployment. Do not expose the port publicly. To remove stored recordings, stop the app and delete their corresponding `data/jobs/<job-id>/` folders. The source Desktop recordings are never modified.

Only the Mail + Calendar meeting-reply recipe currently supports activation. Other suggestions remain proposals and explicitly show that setup is not supported. Screenshots cannot establish every click or successful save. Elapsed screen spans are observations; projected savings are explicitly hypothetical and may be omitted when evidence is insufficient. Sampling can miss fast transitions and small text. After a server restart, unfinished analyses are marked retryable rather than silently resubmitted to a paid API.

## Activate a background automation

The **Activate** button on the meeting-reply card opens the initial connection flow. Add a **Mistral Studio API key with Workflows access**, then allow the **chonkytasks Automations** helper to access Mail and Calendar in the macOS prompts. The OpenRouter key used for recording analysis cannot activate Workflows. The Studio key is validated against the Workflows API and stored in macOS Keychain; it is not written to `.env`, the database, logs, or a LaunchAgent plist. An existing `MISTRAL_API_KEY` environment setting is also supported when no Keychain credential is saved.

Once connected, **Activate** starts a separate worker and creates a verified Mistral schedule. The **Automations** sidebar page shows its status and supports pause/resume. The app confirms **Active** only after a worker heartbeat and an unpaused schedule are reported by Mistral. Missing permissions, offline workers, and unverified connections have distinct states.

**Activity** records actual background checks with start time, elapsed time, messages checked, drafts saved, and errors. The **Results & alerts** filter focuses on drafts and failures; the unread badge highlights these without flagging every empty check. Use **Mark all read** to clear the badge, or **Clear notifications** to dismiss finished entries from both activity filters. Clearing persists across restarts, leaves in-progress checks visible, and preserves the underlying run records, draft records, and duplicate-prevention ledger. New checks and results still appear normally. History stays on this Mac for seven days, survives app restarts and recording deletion, and contains no email bodies. Runs interrupted by a worker restart remain in history with the number of drafts already confirmed saved.

Choose **Review reply** in Activity to preview the original email, recipients and proposed reply. **Keep draft** leaves it in Mail. **Send reply** explicitly approves sending the current preview; edits made in Mail require a fresh preview. A confirmed send clears that result's unread notification, while keeping its history and other unread results. Ambiguous send responses are never automatically retried. Drafts with attachments added in Mail must be sent from Mail to preserve those attachments.

- Every 30 seconds, the worker checks messages received in Mail's Inbox after activation/resumption. Existing messages are not processed. It checks up to 10 unprocessed messages per run; an inbox backlog above 500 stops with an explicit error.
- Mistral Small extracts meeting intent and a proposed time from new message text. Mail content is untrusted data and is never evaluated as code or tool instructions. Calendar event titles are not sent to Mistral.
- Calendar availability determines proposed weekday slots between 09:00 and 17:00, in the Mac's timezone, within the next 14 days. The duration comes from the request, defaulting to 30 minutes. Ambiguous dates/times produce a clarification draft.
- The background workflow only creates reply drafts; it never sends email or changes calendar events. The user can review and send from Activity or Mail. The helper verifies the saved body and recipients in Drafts before reporting success. Drafts use a concise English or French template based on the message language.
- A local ledger prevents duplicates across retries and restarts. Draft verification tolerates Mail's added whitespace while checking the substantive text. If a save acknowledgement fails, a read-only check looks for the exact new reply by thread, sender, recipient, body and save time. An unconfirmed save still pauses for review and is never blindly repeated. Pause disables local writes even when the cloud pause request fails.
- Resuming preserves the original inbox boundary and checkpoint, catching up on messages received during a pause or setup failure.
- A Mail read timeout is recorded in Activity and retried after a two-minute recovery delay. Scheduled runs still occur every 30 seconds; during the delay they log **Waiting for Mail** without piling more requests onto a busy mailbox. Missing permissions and uncertain draft saves still pause for user attention.
- The worker is independent of the app window and restarts at login. This Mac must be awake, logged in, online, and have working Mail accounts. API usage consumes Mistral credits. Pausing in Studio is also supported.

The runtime is isolated in `.workflows-venv` (Python 3.12, pinned dependencies in `workflows/requirements.lock.txt`). Install it with `bash macos/setup_workflows.sh` before building. Validate the real SDK sandbox and schedule schema without network access using `.workflows-venv/bin/python -m workflows.check`. The worker loads its Keychain credential before importing the SDK, whose discovery configuration is initialized at import time. Native/HTTP imports stay inside the activity, outside the deterministic workflow sandbox.

The native helper is signed inside the app bundle and launched through LaunchServices so permissions work independently of the Python host. If an earlier permission request was declined, Connections provides direct links to Calendar and Automation settings and refreshes when you return. The LaunchAgent is created only when the user activates the recipe, at `~/Library/LaunchAgents/com.mistralhackathon.aidiscovery.workflows.plist`; diagnostics are in `~/Library/Logs/AI Discovery/workflows.log`. The ledger and activation state live in `data/automations.sqlite3`, independently of recording deletion. The inbox cursor advances after each handled message, with an overlap to preserve simultaneous arrivals across batches. Pause an automation before removing the app or project folder.

The workflow definition is in `workflows/definitions.py`; `workflows/worker.py` launches it. Its name and recipe are fixed, rather than executing generated source code. Remote schedules contain only the local automation identifier, and workflow activity results contain counts rather than mail contents. API integration follows [Mistral workers](https://docs.mistral.ai/studio/workflows/concepts/workers) and [schedules](https://docs.mistral.ai/studio/workflows/building-workflows/scheduling).

## Verification

```bash
.venv/bin/python -m pytest -q
node --check static/app.js
```

Tests cover frame selection and revisits, actual video extraction and timestamps, screenshot coverage and correction, Mistral-only enforcement, streamed token accounting and retry limits, progress event cleanup, metadata persistence, evidence validation, duplicate occurrence rejection, upload limits, deletion, failure handling, and local API protections. Tests do not call a paid model API.

The UI is static HTML/CSS/JavaScript served by FastAPI. No frontend build step is needed. `requirements.lock.txt` records the tested dependency versions (Python 3.11+); `requirements.txt` contains compatible ranges for other Python versions. Mistral requests use `low` reasoning effort. Large 4 now supports low/medium/high according to [Mistral’s reasoning documentation](https://docs.mistral.ai/inference/conversations/reasoning); the gateway catalog may still advertise the older preview settings. Each attempt has a six-minute wall-clock timeout and a three-minute read timeout. Truncated responses retry with larger output budgets (16k → 32k → 64k), up to three attempts. Interrupted or invalid responses also have bounded retries. A retry reuses saved screenshot observations when they cover every frame and match the configured model, so a summary failure does not require another vision request.
