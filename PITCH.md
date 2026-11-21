# chonkytasks — Pitch Slides

---

## Slide 1 — The problem

> You prompt an agent to automate what you *remember*.
> But what about the stuff you *forgot* you do every day?

- The average knowledge worker repeats the same 6-8 tasks daily
- Nobody writes these down — they're invisible
- That's where the hours go

---

## Slide 2 — The insight

> **Recording finds what you forgot — and proves it's worth automating.**

Watch someone work → find the loops → measure the time → automate the top one.

---

## Slide 3 — How it works

```
recording.mp4
  → frames + Voxtral narration   (what + why)
  → vision pass                  (what happened each frame)
  → rank loops                   (measured minutes, not guesses)
  → compile skill.json           (data, never code)
  → live replay in browser       (33s → 2.34s)
```

---

## Slide 4 — The honest number

Everything here is arithmetic over timestamps from one 5:53 recording.
The model proposed the loop; a Python tool measured it.

| | Value |
|---|---|
| Recording | **5:53**, 11 named parts, 54 real actions |
| The loop | seen **2x**, median **33.0s** per pass |
| Measured in recording | **1.1 min** of that loop |
| Live replay | **2.34s** (14x faster than the human median) |

Projected per month: **32.0 hours**. The basis, said out loud:
*1.1 min of loop inside 5.5 min observed, scaled to 8h x 20 days.*

> We show the measured number first, and we say what the projection assumes.
> 1.1 min inside the 5.5 min of measurable span is a fifth of it. If a judge
> divides it in their head, we would rather they heard it from us.

---

## Slide 5 — Safety by design

- Model writes **data, never code** — `skill.json` is a list of actions
- `--dry-run` prints every step before anything runs
- Anything that saves/sends/deletes/pays **stops and asks**
- Credentials never enter the pipeline
- **It refuses to guess.** Ask it to open "Applied AI Engineer" and five
  Paris postings match, so it stops rather than picking one

---

## Slide 6 — vs. the competition

> Audimate replays the task *you* already picked.
> **We tell you which task to pick — and prove it in measured minutes.**

---

## Slide 7 — The demo

1. Play 60s of real screen recording
2. Show the loops it found, ranked by measured time
3. Click **Run** — the agent drives the live public board
4. "33.0 seconds becomes 2.34"
5. Then ask it for an ambiguous title and watch it refuse

---

## Slide 8 — Close

> **Prompting automates what you remember.
> Recording finds what you forgot — and proves it's worth the money.**
