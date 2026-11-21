# chonkytasks

Watch someone work, find the tasks they repeat, rank them by measured time,
then compile and run an agent that does one of them.

Prompting an agent automates what you remember. Recording finds what you
forgot, and shows whether it is worth automating.

Built in a day at the Mistral hackathon, London, 21 November 2026.

## How it works

```
recording / screenshots
  -> sample_frames.py   frames (+ Voxtral transcript of the narration)
  -> observe.py         vision pass: what happened in each frame
  -> rank.py            group repeated loops, rank by minutes per month
  -> compile.py         turn one loop into skill.json (a list of actions)
  -> run.py             replay it in a real browser with Playwright
```

All model calls go to the Mistral API (vision, reasoning and Voxtral).

**The model writes data, never code.** `skill.json` is a list of actions such
as `fill`, `click` and `extract` with semantic locators. `run.py` is a fixed
interpreter over that list, and nothing generated is executed as Python. That
makes the replay:

- **robust:** locators like role and label are resolved at run time instead of replaying pixels.
- **auditable:** `--dry-run` prints every step before anything happens.
- **safe by default:** anything that saves, sends, deletes or pays stops and
  asks first. Credentials never enter the pipeline: the vision pass redacts
  them, and skills start after login.

## Run it

Requirements: macOS or Linux, [uv](https://docs.astral.sh/uv/), `ffmpeg`, and a
Mistral API key. Each script declares its own dependencies, so `uv` installs
them on first run.

```bash
uv run --with playwright playwright install chromium   # once

# Worked example, no API key needed: fills and submits a fake CRM form
./pipeline/run.py demo_01 --headed --slow 300 \
    --param company="Acme Ltd" --param email="a@acme.test"
```

The full pipeline:

```bash
export MISTRAL_API_KEY=...                  # console.mistral.ai/api-keys
./pipeline/doctor.py && source out/env.sh   # checks which models your key can use

./ui/server.py                              # demo UI at http://localhost:8799
```

The UI lets you record your screen in the browser or upload a video or
screenshots, then runs each stage. To run the stages by hand:

```bash
./pipeline/sample_frames.py recording.mp4 --every 3
./pipeline/observe.py
./pipeline/rank.py
./pipeline/compile.py opp_01
./pipeline/run.py opp_01 --dry-run          # read it before you trust it
./pipeline/run.py opp_01 --headed
```

## Repo layout

| Path | What |
|---|---|
| `pipeline/` | the stages above, plus `doctor.py` (setup checks) and `record.sh` |
| `ui/` | stdlib-only demo server and single-page UI; `/llms.txt` describes the API |
| `fixtures/fake-crm.html` | offline target for the worked example |
| `out/` | generated output, git-ignored except `out/skills/demo_01` |
| `CONTRACT.md` | data shapes passed between stages |
| `TEAM.md`, `RECORD.md` | the team brief and how the demo clip was recorded |

## Privacy

Recordings, frames and everything derived from them stay in `out/` on your
machine and are ignored by git. Frames are sent to the Mistral API for the
vision pass. The demo server only listens on `127.0.0.1`. Don't leave it running
while you browse other sites.

## License

MIT, see `LICENSE`.
