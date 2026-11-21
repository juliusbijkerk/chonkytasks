# ✈️ AutoPilot — hackathon MVP

AutoPilot helps non-technical businesses (here: a job seeker) adopt AI for their **repetitive
tasks**. You click *Start observation*, the app records your browser actions for a set window,
a Mistral model analyses the log, detects the repetitive workflow, shows you what it costs per
week — and an AI agent can then **do the task for you** (safely: dry-run first, drafts never sent,
tracker backed up before every write).

## Demo scenario (the one that must work)

1. Open Gmail, search rejection emails ("unfortunately", "not moving forward", …).
2. Reply to each with a polite request for feedback (**created as a draft, never auto-sent**).
3. Open `applications.xlsx` and set that application's Status to **Declined**.
4. Mark rows still "Applied" with no reply after 21 days as **Ghosted**.
5. Dashboard shows time spent vs time saved (h/week, £/month).

## Quick start (Demo Mode — works with zero setup except optionally the API key)

```bash
cd autopilot
pip install -r requirements.txt
cp .env.example .env          # add MISTRAL_API_KEY to enable Mistral analysis/agents/voice
                              # without a key, Demo Mode still works (deterministic fallback)
python -m backend.demo_data.make_sample_xlsx   # creates backend/demo_data/applications.xlsx
uvicorn backend.main:app --reload --port 8000
```

Open **http://localhost:8000/** → press **✨ Load sample session** → the dashboard shows 3
detected repetitive tasks (rejection emails ×6, report assembly ×2, form filling ×2) → on the
top task press **▶ Run agent now** (mode: *Draft and ask me*) → watch the live agent feed, review
the 5 drafted replies, tracker rows flip to *Declined*, stale rows to *Ghosted*, and the impact
numbers update.

API demo without the UI:

```bash
curl -X POST "http://localhost:8000/api/sessions/load-sample?analyze=true"
TASK=$(curl -s http://localhost:8000/api/dashboard | python -c "import json,sys;print(json.load(sys.stdin)['tasks'][0]['id'])")
curl -X POST "http://localhost:8000/api/agents/$TASK/run" -H 'Content-Type: application/json' \
     -d '{"mode":"dry_run","demo_mode":true}'          # preview
curl -X POST "http://localhost:8000/api/agents/$TASK/run" -H 'Content-Type: application/json' \
     -d '{"mode":"draft","demo_mode":true}'            # real run (drafts + tracker)
curl -N "http://localhost:8000/api/agents/runs/<run_id>/events"   # live SSE feed
```

## Real Gmail (optional, drafts only)

1. Create an OAuth client (type *Desktop*) in Google Cloud Console with the Gmail API enabled;
   download `credentials.json` into the project root.
2. `python -m backend.gmail_client auth` (opens a browser once, writes `token.json`).
3. Scopes requested: `gmail.readonly` + `gmail.compose` — **drafts only; AutoPilot never sends
   without an explicit "send approved drafts" action**.
4. Run the agent with `demo_mode=false`.

## Chrome extension (real observation)

1. `chrome://extensions` → enable *Developer mode* → *Load unpacked* → select `extension/`.
2. Click the ✈️ toolbar icon → *Start observation* (5/15/30 min). A red **REC** badge shows.
3. Work normally. Events (navigations, clicks, field labels — **never typed values**) are batched
   to `POST /api/events` on localhost.
4. Press *Stop & analyse* in the web UI (or let the timer end) → analysis runs → dashboard updates.

## Architecture

```
autopilot/
  backend/
    main.py          # FastAPI routes (sessions, events, dashboard, agent runs, drafts, voice, export)
    analyzer.py      # event log -> repetitive workflows; numbers computed in Python (grounded),
                     # wording/proposals from Mistral Large (JSON mode); deterministic fallback
    agent.py         # rejection flow; real Mistral Agent (beta.agents + function tools) when a
                     # key is present, deterministic pipeline otherwise; every step is a run event
    gmail_client.py  # OAuth + search + create_draft (never send)
    excel_client.py  # openpyxl read/update, fuzzy match (rapidfuzz), ghosted rule, timestamped backups
    voice.py         # Voxtral transcription + command routing (optional)
    db.py            # SQLite: sessions, events, analyses, tasks, runs, run_events, drafts
    demo_data/       # sample_events.json, sample_emails.json, make_sample_xlsx.py
  frontend/          # "My Repetitive Tasks" dashboard (vanilla JS + Chart.js)
  extension/         # MV3 observer (manifest, background, content, popup)
  exported_skills/   # automations exported as Vibe-skill-style artifacts (SKILL.md + run.py + schedule.json)
```

## Mistral prize tracks

- **Agents (bonus):** with `MISTRAL_API_KEY` set and `USE_MISTRAL_AGENT=1`, the run is driven by a
  real Mistral Agent (`beta.agents.create`) with function tools `search_gmail`, `classify_email`,
  `read_tracker`, `update_tracker_row`, `create_gmail_draft`, `apply_ghosted_rule` — the model
  chooses the calls; each appears in the UI feed as `TOOL_CALL` / `TOOL_RESULT` ("under the hood").
  Falls back to the deterministic pipeline on any error.
- **Voxtral (special):** mic button on the dashboard (`POST /api/voice`) transcribes speech
  (`audio.transcriptions.complete`, model from `MISTRAL_VOICE_MODEL`) and routes commands like
  "automate the rejection emails and show me the drafts".
- **Vibe:** `POST /api/agents/{task_id}/export-vibe-skill` writes `exported_skills/<slug>/` with
  `SKILL.md` (Vibe skill frontmatter), `run.py`, and `schedule.json` (weekly cron `0 9 * * MON`).
- Model IDs are configurable in `.env` (`MISTRAL_LARGE_MODEL`, `MISTRAL_SMALL_MODEL`,
  `MISTRAL_VOICE_MODEL`); default large = `mistral-large-latest` (set the exact Mistral Large 4 /
  "Le Chonk" ID from console.mistral.ai when you have it).

## Safety rules (by design)

- Observer never records typed text, passwords, or email bodies; emails/numbers masked; localhost only.
- Agent runs are dry-run first; Gmail replies are **drafts**; sending is a separate explicit action.
- `applications.xlsx` is backed up (`*.bak-<timestamp>`) before every write.
- Every Mistral call: JSON mode + one retry on error/malformed output.

## 3-minute demo script

1. `uvicorn backend.main:app --reload` → open http://localhost:8000 (0:00)
2. "This is AutoPilot. I press **Load sample session** — this is a 30-min recording of my browser." (0:20)
3. "It found 3 repetitive tasks. Top one: **handling rejection emails — 6 times, ~2.2 h/week**." (0:40)
4. "I click **Run agent now**. Watch the live feed: it classifies 8 emails, finds 5 rejections…" (1:10)
5. "…drafts 5 polite feedback requests for my approval — nothing is sent automatically — and updates
   my tracker: 4 rows → Declined, 3 stale rows → Ghosted. Backup saved first." (1:40)
6. "Impact: ~1.5 h/week saved, ~£95/month. And I can **export this as a Vibe skill** to run every Monday." (2:10)
7. "With the extension installed it records my real work; with a Mistral key the analysis, the agent
   and the voice commands all run on Mistral models." (2:30)

## Known limitations / next steps

- Demo Mode simulates Gmail with `sample_emails.json`; real Gmail path works but needs OAuth setup.
- Weekly estimates assume the recorded session ≈ one workday (× `WORKDAYS_PER_WEEK`).
- Only the rejection-email workflow has a real agent; the other two show proposals + labelled preview.
- Ghosted rule checks the tracker only (no reply detection in Gmail yet).
- SSE run feed replays stored events after completion (no live tail if you open it late).
- Chrome extension untested on all sites; SPA navigation detection is heuristic (1.5 s URL poll).

## Assumptions made (per instructions, no questions asked)

- No `MISTRAL_API_KEY` was present in the environment → Demo Mode ships with a deterministic
  fallback so the demo works regardless; drop the key into `.env` at the venue to enable Mistral.
- Hourly rate £15, 3.5 manual minutes per rejection email, 21-day ghost threshold (all in `.env`).
- "Mistral Large 4 / Le Chonk" exact model ID not verifiable without a key → `mistral-large-latest`
  default, configurable via `MISTRAL_LARGE_MODEL`.
