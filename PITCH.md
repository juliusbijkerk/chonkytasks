# chonkytasks — Pitch Slides

---

## Slide 1 — The problem

> You prompt an agent to automate what you *remember*.
> But what about the stuff you *forgot* you do every day?

- Nobody writes these down — they're invisible
- You cannot prompt for a task you cannot name
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
  → live replay in browser       (33s → 1-2s)
```

**On the narration, before anyone asks.** Voxtral transcribes the audio and
aligns it to the frames it belongs to. In this recording that produced
nothing usable: we recorded in a room full of people, so the transcript is
other teams talking about their own demos. The honest outcome is that **0 of
54 steps carry narration**, because the alignment refused to attach audio that
did not belong to the person recording. Earlier in the day it did attach it,
and a stranger's conversation showed up as the user's reasoning. We fixed that
by windowing the transcript per segment rather than passing the whole thing to
every call. Narrate into a headset and the "why" arrives; narrate in a
hackathon room and you get silence, which is the right answer.

---

## Slide 4 — The honest number

Everything here is arithmetic over timestamps from one 5:53 recording.
The model proposed the loop; a Python tool measured it.

| | Value |
|---|---|
| Recording | **5:53**, 11 named parts, 54 real actions |
| The loop | seen **2x**, median **33.0s** per pass |
| Measured in recording | **1.1 min** of that loop |
| Live replay | **1-2s** of actions, against a 33s human median |
| Wall clock | about **3s**, because launching a browser costs 2.5s |

**There is no monthly figure on this slide, and that is the point.**

The tool will not project a month from six minutes. It has a floor, and under
that floor it returns nothing and says why. Scaling 1.1 min of loop inside a
5.5 min clip to a whole working day would give you 32 hours a month. It is
correct arithmetic and it is not evidence, because a six minute clip of one
task is not a representative day.

> Give it an hour of recording and it will give you a month. We would rather
> show you a small number we can stand behind than a big one we cannot.

---

## Slide 5 — Safety by design

- Model writes **data, never code** — `skill.json` is a list of actions
- `--dry-run` prints every step before anything runs
- Anything that saves/sends/deletes/pays **stops and asks**
- Credentials never enter the pipeline
- **It refuses to guess.** Ask it to open "Applied AI Engineer" and five
  Paris postings match, so it stops rather than picking one

---

## Slide 6 — "This breaks the moment the page changes"

It does. That is the first thing anyone asks about automation like this, so
here is what happened today.

The compiler watched the recording and decided the Location filter was a
**button**. On the live board it is a `<select>`. The skill was wrong.

- It took **two minutes** to fix, because the skill is data, not code
- The diff is two lines of JSON, reviewable by someone who cannot program
- `skill.compiled.json` keeps what the model originally said, next to the fix
- Locators are **semantic** (role, label, text), not pixel coordinates, so
  a restyle does not break them. Only a rename does

> We are not claiming it never breaks. We are claiming that when it breaks you
> can see why, in a file you can read, and fix it before the meeting starts.

---

## Slide 7 — vs. the competition

> Audimate replays the task *you* already picked.
> **We tell you which task to pick — and prove it in measured minutes.**

---

## Slide 8 — The demo

1. Play 60s of real screen recording
2. Show the loops it found, ranked by measured time
3. Click **Run** — the agent drives the live public board
4. "33 seconds becomes one or two"
5. Then ask it for an ambiguous title and watch it refuse

**Say this at step 3, before anyone notices it:** the recording spans LinkedIn
and a careers page. The skill it compiled runs on the careers page, because
that is the half an agent can touch without someone's credentials. That is not
a gap in the demo, it is the reason we picked job boards over LinkedIn.

---

## Slide 9 — Close

> **Prompting automates what you remember.
> Recording finds what you forgot — and proves it's worth the money.**
