# Workday X-ray — team brief

**What it is:** watch someone work for 15 minutes, find the loops they repeat,
rank them by measured time, then generate and run the agent that does one.

**Why not just prompt an agent:** prompting automates what you remember.
Recording finds what you forgot, and proves it is worth the money.

## The demo we are building towards

1. Here is 15 minutes of real work (ours, recorded this morning).
2. Here are the 4 loops it found, ranked, with measured minutes.
3. Here is the top one, 6.2 hours a month.
4. Click. The agent runs it live. 95 seconds becomes 4.
5. "At a 5-person company over a week that is 200 hours of signal."

Step 4 is the moment that wins. Protect the time to build it.

## Stage ownership

| Stage | Owns | Reads | Writes |
|---|---|---|---|
| Capture | recording + Voxtral narration | mp4 | frames, narration.txt |
| Observe | vision pass | frames | `ObservedStep[]` |
| Rank | clustering + ROI | `ObservedStep[]` | `Opportunity[]` |
| Execute | agent generation + live run | `Opportunity[]` | a running thing |
| Demo | UI, pitch, fallback video | everything | the 5 minutes |

Nobody reads a stage that is not theirs. See `CONTRACT.md`. That file is the
whole reason a merge at 14:00 is possible.

## Run it

```
export MISTRAL_API_KEY=...            # console.mistral.ai/api-keys
./pipeline/doctor.py && source out/env.sh    # DO THIS FIRST, see below

./pipeline/sample_frames.py recording.mp4 --every 3   # mp4   -> frames
./pipeline/observe.py                                 # frames -> steps.json
./pipeline/rank.py                                    # steps  -> opportunities.json
./pipeline/compile.py opp_01                          # opp    -> skill.json
./pipeline/run.py opp_01 --dry-run                    # read it before you trust it
./pipeline/run.py opp_01 --headed --slow 300          # replay it live
```

**Tested and working:** the sampler, and the whole executor (compile output shape,
dry run, live replay, failure path). There is a worked example you can run right
now with no API key:

```
./pipeline/run.py demo_01 --headed --slow 300 \
    --param company="Acme Ltd" --param email="a@acme.test"
```

It fills and submits `fixtures/fake-crm.html` and prints `0.5s (human: 95s, 190x
faster)`. Use it to rehearse the demo while the vision half is still being built.

**Untested:** the three model calls (`observe`, `rank`, `compile`), because there
was no API key on this machine. Fix the model id first:
`curl -H "Authorization: Bearer $MISTRAL_API_KEY" https://api.mistral.ai/v1/models`
then set `MISTRAL_VISION_MODEL` / `MISTRAL_MODEL` to what is actually there.

## Why the executor is built this way

The model emits **data, never code**. `skill.json` is a list of actions; `run.py`
is a fixed interpreter over them. Nothing generated is executed as Python.

That buys three things, in order of how much a judge will care:

1. **It replays reliably.** Semantic locators (`get_by_role`, `get_by_label`)
   resolve at runtime. Pixel-replay RPA dies the moment the page shifts, which
   on stage is always.
2. **It is auditable.** A reviewer reads the skill before it runs. `--dry-run`
   prints it. That is the answer to "would you let this loose on your CRM?"
3. **It is safe by default.** Anything that saves, sends, deletes or pays is
   marked `confirm` and stops to ask. `--yes` skips the prompts for the demo.
   Credentials never enter the pipeline: the vision pass redacts them, and the
   compiler is told to start skills *after* login rather than automate it.

## Prize alignment

- **Voxtral, £250.** Narrate while recording. Vision gets *what* happened, voice
  gets *why* ("I check the PO first because finance bounces it otherwise").
  That is where the tacit knowledge lives. It is not prize-chasing, it is the
  missing half of the signal.
- **Agents/Workflows bonus, £500.** Build observe -> rank -> execute as a Mistral
  Workflow rather than three scripts in a trenchcoat, and say so when presenting.

## Rules for today

- Commit every 15 minutes. Shared repo, not four laptops.
- Pick the recorded task before writing more code. It must be: 2 minutes to
  perform, repeatable without a login, and obviously tedious.
- Nothing on screen you would not show a stranger. No passwords, no real
  customer data. The pipeline redacts, but do not rely on it.
- Record the 90 second fallback video by 17:00. Wifi dies at the worst moment.
- 18:00 is a hard stop for submission.

---

## Demo UI

```
./ui/server.py            # http://localhost:8799
```

Stdlib only, no build step, works with the wifi off. Left column is the ranked
opportunities, right is the compiled skill. Click Run and the actions go green
one by one with a live timer, ending on "103x faster". Tested against `demo_01`.

`out/opportunities.json` currently holds **placeholder data** so the UI has
something to draw. `rank.py` overwrites it with the real thing.

## What we demo: the shopping comparison, not a fake portal

The recorded task is **Julius comparing prices for something dull across a few
retail sites**, ending at the basket. Reasons, in order:

1. It is real. He genuinely lost hours to this kind of thing, and the narration
   will contain real rules ("I never trust a review count under 50"), which is
   exactly the tacit knowledge a typed prompt would miss.
2. Everyone in the room has done it, so no setup is needed in the pitch.
3. The output is a visible artifact: a table of options, prices, URLs.
4. **It stops at "Add to basket" and asks.** The safety gate is not a slide,
   it is a thing that happens on stage. That is the answer to "would you let
   this loose on my systems?"

A fake portal we built ourselves is safer to automate and worth far less:
we would control both sides, and judges discount that on sight.

### The risk, and the hedge

Real retail sites have cookie walls, bot detection and shifting layouts.
Playwright on a big travel or marketplace site will probably get blocked.

So: **pick three small-to-middling retailers and test replay on them before
committing.** If a site fights back, drop it, do not fight it. Hotel booking is
the worst candidate for this reason; dull physical products are the best.

Keep `demo_01` working the whole day as the rehearsal rig, and record the
90-second fallback video straight after the first good run on the real sites.

---

## Which sites we can actually drive (probed 21 Nov, on venue wifi)

```
./pipeline/probe_sites.py         # re-run this if anything starts failing
```

| Site | Verdict | Detail |
|---|---|---|
| **Dunelm** | **use this** | 61 prices, 66 product links, name and price in one element |
| **IKEA** | second site | 74 prices, 132 links, but name and price are split across nodes |
| Amazon | avoid | loads, but only 1 usable product link and it blocks unpredictably |
| Argos | no | bot wall |
| Next | no | HTTP 403 |
| eBay | no | HTTP 403 |
| Wayfair | no | HTTP 429 |
| John Lewis | no | `ERR_HTTP2_PROTOCOL_ERROR` |

**Demo on Dunelm, with IKEA as the second site** so the pitch can say "across
retailers" truthfully. Do not touch the others.

Dunelm gives you this straight out of a single link's text:

```
20% Off Hotel Luxury Cotton Mattress Protector £18.20 - to £28.80
New Fogarty Soft Touch Waterproof Mattress Protector £16 - to £24
Special Buy Microfibre Mattress Protector £5 - to £10
```

### Two Playwright traps this probe already hit, so you do not have to

1. **One page for several sites does not work.** The previous site's pending
   timers fire a navigation that interrupts the next `goto`, and you get
   "interrupted by another navigation" on every site after the first. Use a
   fresh context per site. This cost six false negatives before it was spotted.
2. **`wait_until="domcontentloaded"` races the consent redirect.** Use
   `"commit"`, then wait for load state separately and tolerate the timeout.

### Caveat

This is one probe, on this wifi, at this hour. Retailers change their minds.
Re-run it before the final rehearsal, and keep `demo_01` as the rig that cannot
fail.

---

## Audimate (audimate.ai) — the closest competitor, and it helps us

Read their site properly. Audimate Labs Inc, $20/seat Plus, $100/seat Pro,
desktop app, agent called Arro. "Show Arro how you work and it follows your
steps on every run." You record a task once, it writes a **playbook** with
screenshots, works out which values change per run, and replays it in its own
browser. SOPs, triggers, schedules, plugin approvals.

So the record-and-replay half is taken, and taken well.

**What they do not do: discovery.** Nothing on their site scans activity, scores
loops or tells you what is worth automating. Their own copy says "Start with the
task your team repeats most" — they make the *user* choose. Same with Claude's
Record-a-Skill.

That is the whole wedge, and it is now a sharper line than before:

> Audimate and Claude replay the task you already picked.
> We tell you which task to pick, and prove it in measured minutes.

Do not pretend they do not exist. Name them in the pitch. "Audimate does the
replay half very well and charges $20 a seat for it. Nobody does the half that
comes first." That reads as market awareness, not weakness.

## The new UI flow

`./ui/server.py` then open <http://localhost:8799>. Five sections, one page:

1. **Recording** — drag an mp4 in. Buttons for each pipeline stage, with live
   log output underneath.
2. **Parts** — the recording split into scenes, as thumbnails. Click one to
   filter the steps below it.
3. **Steps** — the sequence, grouped by part, with your narration quoted inline
   next to the step it belongs to. Low-confidence steps are marked amber.
4. **Worth automating** — the ranked loops, measured minutes first.
5. **Automation** — the action list, then the live run and the verdict.

Tested in a browser end to end: upload, sample, segment, click an opportunity,
compile, run, "101x faster".

### New: `pipeline/segment.py`

Splits frames into scenes locally, no API call, free. `observe.py` now reads it
and sends **one segment per call** instead of arbitrary batches of six, so a
single look never straddles two different screens.

One trap found and fixed while building it: folding short segments by *frame
count* destroys real scene cuts, because one kept frame can stand for a long
still stretch. Fold by duration instead. The first version silently collapsed
three scenes into one and looked like it was working.

### New: `pipeline/label.py`

Names each part with one vision call on two frames. `seg_02, 0:12 to 0:24` means
nothing to a judge; **"Logging the price in Notes"** means everything. The label
shows on the thumbnail and as the heading above that part's steps.

```
MISTRAL_LABEL_MODEL=<a small vision model> ./pipeline/label.py
```

Cheapest pass in the pipeline, biggest effect on the demo. It writes after each
part, so a run that dies halfway keeps the parts it already named. Re-run with
`--force` to relabel.

---

## `pipeline/doctor.py` — run this before anything else

```bash
export MISTRAL_API_KEY=...
./pipeline/doctor.py
source out/env.sh
```

Five scripts here call the API and the model ids in them were guesses. The
doctor lists what is actually on the account, picks a model for each job
(reasoning, vision, labelling, voice), then **proves each call shape with a real
request**: a JSON-mode completion, a vision call on a generated image with the
word INVOICE in it (it checks the model reads it back), and an audio upload that
checks whether segment timestamps come back at all. Without those timestamps the
narration cannot be aligned to frames, and that is worth knowing in one second
rather than after a 15 minute recording.

It writes `out/env.sh` with the ids that exist. `source` it in every shell that
runs the pipeline. Tested against a missing key and a rejected key; both fail in
one line instead of a traceback.

**Everyone on the team runs this in their own shell.** A model id that works on
one account is not guaranteed on another.

---

## First contact with the real API: two bugs, one speed trap

The doctor is green. Getting there found two things that would have cost the
afternoon, both silent:

**1. `message.content` is not always a string.** On `mistral-large-latest` it
comes back as a list of blocks, `[{"type":"thinking",...},{"type":"text",...}]`,
and the JSON you asked for is in the last text block. `json.loads(content)`
throws on the list. `mistral-small-latest` still returns a plain string, so code
that works against small breaks against large and vice versa.

**2. `temperature: 0` is rejected**, error 3054: "top_p must be 1 when using
greedy sampling." Pin `top_p: 1` whenever temperature is 0.

Both are handled in `pipeline/_mistral.py`, which every script now calls. If you
write a new script, import `chat_json` from it rather than posting yourself, and
neither can come back.

Also: httpx's `raise_for_status()` **hides the response body**, which is where
Mistral puts the actual reason. Both bugs above showed as a bare `400 Bad
Request` until the body was printed. `_mistral.py` raises with the body.

### The speed trap: do not run `observe.py` on Large

Measured on the same 6 frames, just now:

| vision model | time | steps found |
|---|---|---|
| `mistral-large-latest` | **86s** | 1 |
| `mistral-small-latest` | **3.3s** | 6 |

26x. Large spends it on reasoning blocks we throw away. On a 15 minute
recording that is the difference between a 20 minute wait and under a minute.

```bash
export MISTRAL_VISION_MODEL=mistral-small-latest   # for observe.py and label.py
```

Keep `mistral-large-latest` for `rank.py` and `compile.py`, where the thinking
is the point and it runs once.

**Check this on the real recording before trusting it.** On test-pattern footage
small reported 6 steps where large reported 1, and on footage of nothing at all,
finding fewer steps is the more honest answer. Small may invent more. Compare
both on your first real clip, then decide.

### Proven working against the live API

- `doctor.py` — all four checks green
- `label.py` — named 3 parts, correctly, in 2.2s total
- `observe.py` — ran clean; reported almost nothing on test patterns rather
  than inventing work, which is what we want from the prompt
- `transcribe.py` — Voxtral returns segment timestamps, so narration can be
  aligned to frames

Still unexercised on real input: `rank.py` and `compile.py`. They need steps
from an actual recording to have anything to cluster.

---

## The Agents bonus: `pipeline/rank_agent.py`

Drop-in replacement for `rank.py`, same `out/opportunities.json`, built on the
Mistral Agents SDK with three real tools: `list_steps`, `measure_loop`,
`finish`. **Needs Python 3.12** (`uv` handles it from the shebang).

```bash
./pipeline/rank_agent.py
```

Tested against the live API: the agent calls the tools in a loop and finishes.

### Say this to the judges, it is the good bit

> The agent is good at seeing that steps 4 to 9 and 21 to 26 are the same loop.
> It is bad at arithmetic over timestamps, and arithmetic is where our whole
> pitch lives: measured minutes, not guesswork. So the agent proposes a loop and
> the `measure_loop` tool computes occurrences, median and observed minutes in
> Python. Every number on screen is arithmetic over real timestamps. The model
> never writes one.

That is a real reason to use tools, not a bonus-chasing wrapper, and the
`measured_by` field in the output says so.

**Workflows was the wrong target.** `mistralai-workflows` needs Python 3.12,
pulls in temporalio, and wants a worker process running against a dev server.
Half an hour of setup minimum, with three hours left. Agents needs none of it.
The bonus says "Agents and/or Workflows", so Agents alone qualifies.

## Guard added to both rankers

The agent run surfaced a real hazard: 0.1 min of loop inside a 0.4 min clip
extrapolates to **"40 hours a month"**. True to the formula, absurd to a judge,
and exactly the kind of number that destroys trust in everything else on screen.

Both `rank.py` and `rank_agent.py` now:

- **drop anything seen fewer than twice**, or marked `automatable: "no"`
- set `extrapolation_reliable: false` when under 5 minutes were observed
- print a warning saying the per-month figures are arithmetic but not evidence

The UI shows an amber note on any card with an unreliable projection. So if
somebody demos a two-minute clip, the page says so rather than claiming 40
hours. **Record at least 10 minutes.**

---

## Does it actually read a real screen? Measured, on real Dunelm pages.

Two real screenshots of the Dunelm search page, 6 seconds apart, fed through
`observe.py`. What the frames show: a results page, then the same page scrolled.
**Two steps.** Anything more is invention.

| model | time | steps | verdict |
|---|---|---|---|
| `mistral-small-latest` | 2.3s | **4** | invented "opened field", "typed", "clicked". Marked all of them **0.95 confident** |
| `magistral-small-latest` | 2.2s | **4** | same three invented steps, all at 0.95 |
| `mistral-large-latest` | **82s** | 2 | correct, and honestly graded (0.9 / 0.7) |
| `mistral-medium-latest` | **2.6s** | 2 | correct, honestly graded (0.9 / 0.8) |

The small models reasoned backwards: a results page *implies* someone typed and
clicked, so they reported watching it happen. Three steps that never occurred,
at 0.95 confidence. On a 15 minute recording that is hundreds of fabricated
steps feeding the ranker, and every measured minute downstream is then a lie.

This is the failure that would have cost the room, and it does not look like a
failure. It looks like a richer, more detailed output.

### Two prompt fixes, then re-measured

**Anti-inference rule**, now the first thing `observe.py` says:

> Report only what a frame SHOWS, never what must have happened to produce it.
> A results page proves a search ran; it does not prove you saw someone open
> the field, type and click. Missing a real step is fine. Inventing one is not.

After it, **small drops from 4 steps to 2**. The hallucination was the prompt's
fault as much as the model's.

**Confidence calibration.** Every model returned 1.0 for everything, which makes
the number worthless. The prompt now says 1.0 is almost never right, a clear
read of a labelled button is 0.8 to 0.9, a guess at a field's contents is 0.4.
Medium now returns 0.9 and 0.8 on those same two frames.

### What to run

```bash
export MISTRAL_VISION_MODEL=mistral-medium-latest
```

Already set in `out/env.sh`. Large's accuracy at 1/32 of the time. Use Large for
`rank`/`compile`, where thinking is the point and it runs once.

**Keep an eye on the confidence column during the demo.** A wall of identical
high numbers means the model has stopped discriminating, and the honest thing
is to say so rather than let the ranker build on it.

---

## Scope: the question a judge will definitely ask

"When does it stop?" A recording shows the loop. It almost never shows the
**stopping rule**, because there wasn't one: the person stopped when they got
bored. Julius checked 12-15 hotels last night, called two, and still ended up in
the wrong place at the wrong price. That is not a speed failure. It is a scope
failure, and speed would not have fixed it.

So `compile.py` now makes every skill declare a contract, shown in the UI above
the actions:

| field | means |
|---|---|
| `repeats_over` | what it iterates: search results, rows, emails |
| `budget` | how many times round, **set higher than what the human managed** |
| `budget_reason` | why that number |
| `bar` | what makes a result worth keeping, taken from the narration |
| `stop_when` | a real condition, never "when done" |
| `hands_back` | what lands in front of the person at the end |

The budget rule is the one that matters, and it is the pitch:

> You stopped at twelve because you were tired. It does not get tired, so it
> does forty and ranks them. The win is not that it is faster. It is that the
> answer changes.

If the narration never states the bar, the skill says "not stated in the
recording" rather than inventing a threshold. An invented bar is the same class
of error as an invented step.

`hands_back` should almost always be a ranked list the person chooses from.
"It decided for you" is only honest if the recording showed the person deciding
without deliberating, which it usually will not.
