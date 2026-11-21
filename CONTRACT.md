# Shared data contract

Agree on this before anyone writes code. Diverge on implementation, converge here.
If every spike reads and writes these two shapes, merging at 14:00 is plumbing
instead of a rewrite.

## 1. ObservedStep

One thing the person did, as seen in the recording. Produced by the vision pass.

```json
{
  "t": 42.5,
  "app": "Chrome",
  "surface": "app.hubspot.com/contacts",
  "action": "typed into field",
  "target": "Company name",
  "value_kind": "company",
  "narration": "I always paste the company before the email or it dedupes wrong",
  "confidence": 0.78
}
```

- `t` seconds from start of recording
- `surface` URL or window title, whatever is legible in the frame
- `action` free text, but keep the verb set small: opened, clicked, typed into field,
  copied, pasted, switched app, scrolled, waited, submitted
- `value_kind` the KIND of data, never the value itself. No PII in the pipeline.
- `narration` the Voxtral line nearest this timestamp, or null
- `confidence` the model's own honesty score, 0 to 1. We show it in the UI.

## 2. Opportunity

A recurring loop worth automating. Produced by the ranking pass.

```json
{
  "id": "opp_01",
  "title": "Copy new leads from inbox into CRM",
  "steps": ["opp_01 step ids or indices into the ObservedStep list"],
  "occurrences": 7,
  "median_seconds": 95,
  "observed_minutes": 11.1,
  "projected_hours_per_month": 6.2,
  "automatable": "full",
  "blockers": ["needs CRM login"],
  "why_now": "same 6 fields every time, no judgment involved",
  "confidence": 0.81
}
```

- `occurrences` times we saw this loop in the recording
- `observed_minutes` real measured time inside the clip. Never extrapolated.
- `projected_hours_per_month` the extrapolation, kept in a SEPARATE field so the
  demo can show both. Judges will ask how you got the number. Show the honest one.
- `automatable` one of: full, partial, human_in_loop, no
- `blockers` what stops it being full

## Rule

The ranking pass may only read ObservedStep. The executor may only read Opportunity.
Nobody reads raw frames except the vision pass.

## 3. Process (added)

The same work, in the words a person would use. Produced by `pipeline/describe.py`
from `ObservedStep[]` only, written to `out/process.json`. The UI shows it as the
"Process" section; until describe has run, `ui/server.py` builds a plain outline
from the raw steps (`"source": "derived"`).

```json
{
  "summary": "Goes through job suggestions from a LinkedIn email and builds a comparison sheet",
  "source": "model",
  "steps": [
    {"n": 1, "title": "Opens the LinkedIn email with job suggestions",
     "detail": "Starts from the weekly job alert", "app": "Gmail",
     "actions": [0], "t_start": 0.0, "t_end": 0.0, "segment": "seg_01", "narration": null}
  ],
  "loops": [{"from": 2, "to": 7, "times": 3, "label": "for each interesting job"}]
}
```

- `actions` indices into the ObservedStep list. Every action belongs to one step.
- A repeated stretch is written once, with the actions from every pass, and a loop.
- `loops[].from/to` are 1-based step numbers.
