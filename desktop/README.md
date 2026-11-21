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

For a separate local build, set `CHONKYTASKS_RELEASE_DIR` to the destination and `CHONKYTASKS_FFMPEG_DIR` to a directory containing `ffmpeg` and `ffprobe`. This produces a Finder-launchable `ChonkyTasks.app` with bundled video tools and native helpers. It uses the prepared Python environments in this checkout and keeps its Keychain entry, worker label, and logs separate from the standard build. The separate build supports local ad-hoc signing with `CHONKYTASKS_SIGNING_IDENTITY=-`. macOS may ask for app access again after a rebuild.

Builds reuse the **chonkytasks Local Development** signing identity in the login Keychain. Its public fingerprint is pinned in `~/Library/Application Support/chonkytasks/signing/identity.sha1`; the private key stays in Keychain and is available to `codesign`. Do not delete that identity between builds. Set `CHONKYTASKS_SIGNING_IDENTITY` to use your own persistent signing certificate. The build refuses ad-hoc signing because its identity changes with each native build, causing macOS to forget privacy grants ([Apple's explanation](https://developer.apple.com/documentation/technotes/tn3127-inside-code-signing-requirements)). This local certificate does not change system trust settings or provide notarization for distribution.

After switching from the old ad-hoc build, macOS may request each required permission once more. Allow it for the installed chonkytasks app; subsequent builds use the same identity. Documents, Desktop, Mail, Calendar, and Keychain permissions are separate macOS controls. Close old AI Discovery instances so two differently signed builds do not compete for the same permission record.

This is a local launcher: keep the project folder and its `.venv` available. The app remembers the project location; rebuild after moving the project, keep the app in the project's `dist/` folder, or set `CHONKYTASKS_HOME`. The older `AI_DISCOVERY_HOME` and `FLOWLENS_HOME` overrides remain supported. Startup details remain at `~/Library/Logs/AI Discovery/server.log`.

The interface uses the Mistral orange/yellow/red palette, glass surfaces, and pixel-cat branding from [chonkytasks](https://github.com/juliusbijkerk/chonkytasks/tree/main/ui), with its MIT license preserved in `static/brand/LICENSE`. The existing sidebar, recording upload, reports, automations, and activity flows remain in place. The app follows the system appearance by default and preserves any previously saved preference; the toolbar has no appearance controls. The sidebar mascot hops on hover, and running email checks show a spinning mascot with randomly varied speed. Confirmed reply sends finish with a brief particle celebration that dismisses automatically, on click, or with Escape. Animations respect Reduce Motion. macOS bundle identifiers, Keychain service names, workflow identifiers, stored data, and log paths retain their previous values so the rename does not create new automation identities.

## Run

Requires Python 3.11+ for the pinned setup below and FFmpeg (`brew install ffmpeg` on macOS).

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
cp -n .env.example .env  # preserve an existing .env
# Set OPENROUTER_API_KEY and OPENROUTER_MODEL in .env
bash scripts/start.sh
```

Open **http://127.0.0.1:8000**. Local findings work before any model key is configured. Keep `.env` private and preserve any existing settings. To use another local port: `PORT=8001 bash scripts/start.sh`.

## Use

1. On macOS 15 or later, choose **Record screen**, then **Entire screen** or **Specific window**. Pick the source in the macOS chooser. **Stop recording** saves a silent local video and opens it for analysis. Use the upload area or ⌘O to import an existing MP4, MOV, WebM, MKV, or M4V.
2. Optionally explain the task, then select **Prepare recording**. The app checks the video on this Mac. Choose **View local findings** to stay offline, or inspect the redacted screenshots, include the ones you want to share, and select **Approve and analyze**.
3. Use the sidebar to switch between a new recording, the searchable recordings list, and recent analyses.
4. Review Summary, Analysis steps, and Automations. Click a screenshot or numbered step to seek the video and see its description, application, timing, and evidence. Playback follows the current step. Returns to a previous view are shown separately, including in older reports.
5. Export the report as Markdown. JSON is available from `/api/jobs/<job-id>/export/json`. Reports remain at their `/#job=…` link after restarting the server.
6. Delete a finished recording from the sidebar, recordings list, or report toolbar. Confirming removes the stored copy and report; the original video is preserved. In-progress recordings cannot be deleted.
7. Use the pencil button to rename a recording or add notes, including while analysis is running. Names and notes are saved locally, searchable, and included in exports. They do not change the source file or the context sent for analysis.

During analysis, the progress bar animates and shows elapsed time. Four stages show preparation, screenshot reading, writing results, and validation. Screenshot counts are separate from batch counts; the current batch has a thumbnail preview. The percentage advances with completed stages and batches, not predicted model completion time.

Model requests stream through OpenRouter. The app shows waiting/receiving/checking/retrying states, elapsed request time, stream update counts, and live output token estimates (marked ≈). Estimates use received text length, not stream chunk count; the final provider usage replaces the estimate with its native token total. Streamed reasoning contributes to the estimate but its text is discarded. Raw response and reasoning text are never sent to the progress UI. Progress updates reach the app through server-sent events with reconnection support.

For meaningful repetition detection, include multiple completed examples in the recording. The local pass matches visible sources to results for six supported workflow types and links them back to playback. Limits are 500 MB and 15 minutes per recording. The video preview is silent. Native capture stops shortly before the import limits so it can finish saving the video.

## Local evidence and Mistral analysis

- FFmpeg makes a browser-compatible preview and samples screenshots with their actual presentation timestamps. Pillow/NumPy compare pixels to remove consecutive near-duplicates while retaining revisits and periodic coverage. These operations are deterministic; they use no AI models.
- The bundled macOS helpers use Apple Vision locally to check for matching source/result pairs and prepare masked JPEGs. The local checks can run without a model API key.
- Privacy review places opaque bars over detected sensitive values such as credentials, contact details, and financial or government identifiers. It keeps the surrounding workflow visible. Use **Refresh masks** on an older recording and **Select all screenshots** or individual checkboxes to choose the evidence. Review the exact images before approving. Only the approved screenshots and context go to the configured Mistral vision model through OpenRouter in batches of 12. The report marks frames left out of model analysis.
- Each batch must account for every screenshot exactly once in chronological ranges. Missing screenshots or overlapping steps get one correction attempt; incomplete coverage is rejected rather than silently omitted. Older saved reports are displayed with explicit screenshot-to-step links and marked gaps, without another model call.
- A second call to the same Mistral model identifies recurring workflows and proposes automations. Pydantic validates the response; the server validates references and derives elapsed spans from screenshot timestamps. Overlapping repeated instances are rejected.
- `OPENROUTER_MODEL` defaults to `mistralai/mistral-large-4-0`. Non-`mistralai/` model IDs are rejected. Provider routing can change the host serving the same Mistral model, never the model family. There is no fallback to another vendor's model.

API integration references: [OpenRouter model catalog](https://openrouter.ai/api/v1/models), [Mistral vision documentation](https://docs.mistral.ai/inference/conversations/vision). The configured model must support both image input and JSON output.

## Storage and scope

The server binds to loopback only. The API key stays on the server. Videos, screenshots, reports, and context are stored locally in ignored `data/jobs/`. Selected redacted screenshots, reviewed context, and model-derived observations are sent to OpenRouter/Mistral after approval. The raw recording stays on this Mac. API calls consume your OpenRouter credits.

This is a single-user local application, not an authenticated deployment. Do not expose the port publicly. To remove stored recordings, stop the app and delete their corresponding `data/jobs/<job-id>/` folders. The source Desktop recordings are never modified.

The six supported recipes can be activated after the local checks match two completed examples of each. Other suggestions remain available for review. Screenshots cannot establish every click or successful save. Elapsed screen spans are observations; projected savings are explicitly hypothetical and may be omitted when evidence is insufficient. Sampling can miss fast transitions and small text. After a server restart, unfinished analyses are marked retryable rather than silently resubmitted to a paid API.

## Activate a background automation

Select **Activate** on the meeting-reply card or **Set up** on another workflow card. Add a **Mistral Studio API key with Workflows access**, then allow the **chonkytasks Automations** helper to access Mail and Calendar in the macOS prompts. The OpenRouter key used for recording analysis cannot activate Workflows. The Studio key is validated against the Workflows API and stored in macOS Keychain; it is not written to `.env`, the database, logs, or a LaunchAgent plist. An existing `MISTRAL_API_KEY` environment setting is also supported when no Keychain credential is saved.

Once connected, activation starts a separate worker and creates or reuses the verified 30-second Mistral schedule for this installation. Each workflow can be paused independently. ChonkyTasks pauses the shared schedule when the last workflow is paused. The **Automations** sidebar page shows its status and supports pause/resume. The app confirms **Active** only after a worker heartbeat and an unpaused schedule are reported by Mistral. Missing permissions, offline workers, and unverified connections have distinct states.

**Activity** records actual background checks with start time, elapsed time, messages checked, drafts saved, and errors. The **Results & alerts** filter focuses on drafts and failures; the unread badge highlights these without flagging every empty check. Use **Mark all read** to clear the badge, or **Clear notifications** to dismiss finished entries from both activity filters. Clearing persists across restarts, leaves in-progress checks visible, and preserves the underlying run records, draft records, and duplicate-prevention ledger. New checks and results still appear normally. Run history stays on this Mac for seven days and survives app restarts and recording deletion. Pending office workflow reviews stay visible until approved. The source excerpts and output for those reviews are stored locally. Runs interrupted by a worker restart remain in history with the number of drafts already confirmed saved.

### Office workflows

- **Email to task** and **Email to note** check for new messages in Apple Mail with your chosen subject prefix. A task needs a `Task:` line and a `Due:` date in YYYY-MM-DD format. Approved tasks and notes are saved as local Markdown files.
- **Invoice filing**, **Spreadsheet report**, and **Order draft** each have an app-managed input folder. Open it from **Automations** and put new files there. Invoices use text or Markdown with `Invoice:`, `Vendor:`, `Date:`, and `Total:` fields. Reports use CSV with `Status` and `Amount` columns and can match a single status or include every row. Order requests use text or Markdown with `Order:`, `Customer:`, `SKU:`, and `Quantity:` fields.
- In **Activity**, choose **Review result** to see the input and proposed output. **Approve and save** writes the reviewed result under `data/workflow-library/`. Invoice copies are organized by vendor and month. Reports are saved as CSV and orders as draft JSON. The original inputs are preserved. Choose **Show in Finder** from a saved result to locate the file.
- The office workflows read and prepare their data locally. Mistral Workflows receives the schedule identifier and run counts. Mail access is required only for the two email workflows. Calendar access is required for the meeting workflow.

### Meeting replies

Choose **Review reply** in Activity to preview the original email, recipients and proposed reply. **Keep draft** leaves it in Mail. For an acceptance, choose a calendar and select **Send and add event** to approve both changes. A conflict found before sending leaves the reply in Drafts. If Mail confirms the reply and Calendar needs attention, Activity displays the event separately. Ambiguous sends and saves require verification before any further write. Edits made in Mail require a fresh preview.

- Every 30 seconds, the worker checks messages received in Mail's Inbox after activation/resumption. Existing messages are not processed. It checks up to 10 unprocessed messages per run; an inbox backlog above 500 stops with an explicit error.
- Mistral Small extracts meeting intent and a proposed time from new message text. Mail content is untrusted data and is never evaluated as code or tool instructions. Calendar event titles are not sent to Mistral.
- Calendar availability determines weekday slots between 09:00 and 18:00 in the Mac's time zone, within the next 14 days. A free requested time produces an acceptance. A conflict produces the first later free slot on a 15-minute grid. Ambiguous times produce a clarification draft. The duration defaults to 30 minutes.
- The worker writes reply drafts in Mail. Sending and adding a Calendar event require approval in Activity. The helper verifies the saved body and recipients before reporting a draft. Replies are available in English and French.
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

Tests cover reviewed screenshot approval, video-based workflow discovery, provider streaming, meeting approval, and calendar recovery. The five office workflows are exercised through activation, the shared scheduled check, Activity, and approval using fresh fictional emails and files. Mail, Calendar, Mistral Workflows, and model calls are mocked in those tests. Set `CHONKYTASKS_SYNTHETIC_VIDEOS` to the generated fixture directory to run the full-video tests with the bundled native helpers. Local development can point at compiled helper executables with `CHONKYTASKS_REDACTOR` and `CHONKYTASKS_EVIDENCE_HELPER`.

The UI is static HTML/CSS/JavaScript served by FastAPI. No frontend build step is needed. `requirements.lock.txt` records the tested dependency versions (Python 3.11+); `requirements.txt` contains compatible ranges for other Python versions. Mistral requests use `low` reasoning effort. Large 4 now supports low/medium/high according to [Mistral’s reasoning documentation](https://docs.mistral.ai/inference/conversations/reasoning); the gateway catalog may still advertise the older preview settings. Each attempt has a six-minute wall-clock timeout and a three-minute read timeout. Truncated responses retry with larger output budgets (16k → 32k → 64k), up to three attempts. Interrupted or invalid responses also have bounded retries. A retry reuses saved screenshot observations when they cover every frame and match the configured model, so a summary failure does not require another vision request.
