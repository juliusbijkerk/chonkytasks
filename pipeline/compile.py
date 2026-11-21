#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""Opportunity -> skill.json, an auditable action list.

    ./pipeline/compile.py opp_01

The model writes DATA, never code. run.py is a fixed interpreter over that data.
That is the whole design: nothing the model emits is ever executed as Python, the
skill is diffable in review, and "it compiles to semantic selectors, not pixels"
is literally true when a judge asks.
"""
import argparse, json, os, sys
from pathlib import Path
import httpx

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mistral import chat_json  # noqa: E402

API = "https://api.mistral.ai/v1/chat/completions"
MODEL = os.environ.get("MISTRAL_MODEL", "mistral-large-latest")

SYSTEM = """You turn observed screen actions into a replayable skill.

Return ONLY a JSON object with this exact shape:

{
  "id": "<the opportunity id>",
  "name": "<short imperative name>",
  "start_url": "<the url the run begins at>",
  "params": [{"name": "company", "kind": "company", "example": "Acme Ltd"}],
  "scope": {
    "repeats_over": "<what the loop iterates: search results, rows, emails>",
    "budget": <how many, as a number>,
    "budget_reason": "<why that number>",
    "bar": "<what makes a result worth keeping, in the person's own words>",
    "stop_when": "<the condition that ends the run>",
    "hands_back": "<what the person gets at the end>"
  },
  "actions": [ ... ],
  "human_baseline_seconds": <median_seconds from the opportunity>
}

Every action is one of:

  {"op": "goto",        "url": "..."}
  {"op": "fill",        "locator": L, "param": "<param name>"}
  {"op": "fill",        "locator": L, "value": "<literal, non-sensitive only>"}
  {"op": "click",       "locator": L, "confirm": true|false}
  {"op": "select",      "locator": L, "value": "..."}
  {"op": "press",       "key": "Enter"}
  {"op": "wait_for",    "locator": L, "timeout_ms": 10000}
  {"op": "extract",     "locator": L, "as": "<name>"}
  {"op": "assert_text", "locator": L, "text": "..."}

A locator L is exactly one of:
  {"by": "role",        "role": "button", "name": "Save"}
  {"by": "label",       "value": "Company name"}
  {"by": "placeholder", "value": "Search"}
  {"by": "text",        "value": "Next"}
  {"by": "testid",      "value": "submit-btn"}
  {"by": "css",         "value": ".x"}          <- last resort only

Rules, in order of importance:
1. Prefer role and label locators. They survive a redesign; css does not.
2. Anything the person typed that varies per run is a PARAM, not a literal.
3. Set "confirm": true on any click that sends, submits, saves, deletes or pays.
   The runner will stop and ask before those.
4. Never emit a credential, card number or anything whose value_kind was
   "secret". If the loop needs a login, do not automate the login: start the
   skill after it and add the param "assumes an authenticated session".
5. End with an assert_text that proves the task actually worked. A run that
   cannot prove success is not a demo.
6. Do not invent steps you were not shown.

7. SCOPE. A recording shows the loop but almost never states when to stop.
   The person stopped because they got bored, not because they hit a rule.
   That is the gap you are filling, so be explicit about it:
   - budget: how many times to go round. Set it HIGHER than what you observed.
     A person checking 12 search results stopped at 12 out of fatigue. A
     machine has no fatigue, so 40 is reasonable where they managed 12, and
     say that in budget_reason. The point of automating this is not doing the
     same work faster, it is doing more of it than a person would sit through,
     because that is what changes the answer.
   - bar: take it from the narration wherever you can. "Under fifty reviews I
     ignore it" is a bar. If the narration gives you nothing, say
     "not stated in the recording" rather than inventing a threshold.
   - stop_when: a real condition, like "budget reached" or "5 results clear
     the bar", never "when done".
   - hands_back: what lands in front of the person at the end. A ranked list
     they choose from is a good answer. "It decided for you" is not, unless
     the recording showed the person acting without deliberating."""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("opportunity_id")
    ap.add_argument("--opportunities", default="out/opportunities.json")
    ap.add_argument("--steps", default="out/steps.json")
    ap.add_argument("--outdir", default="out/skills")
    args = ap.parse_args()

    key = os.environ.get("MISTRAL_API_KEY")
    if not key:
        sys.exit("MISTRAL_API_KEY is not set.")

    opps = json.loads(Path(args.opportunities).read_text())
    opp = next((o for o in opps if o.get("id") == args.opportunity_id), None)
    if not opp:
        sys.exit(f"No {args.opportunity_id}. Have: {', '.join(o.get('id','?') for o in opps)}")

    all_steps = json.loads(Path(args.steps).read_text())
    idx = opp.get("step_indices") or list(range(len(all_steps)))
    steps = [all_steps[i] for i in idx if 0 <= i < len(all_steps)]

    with httpx.Client(timeout=240) as client:
        skill = chat_json(client, key, MODEL, [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(
                {"opportunity": opp, "observed_steps": steps})},
        ])

    skill.setdefault("id", opp["id"])
    skill.setdefault("human_baseline_seconds", opp.get("median_seconds", 0))

    out = Path(args.outdir) / opp["id"]
    out.mkdir(parents=True, exist_ok=True)
    path = out / "skill.json"
    path.write_text(json.dumps(skill, indent=2))

    print(f"\n{skill.get('name')}")
    print(f"  start   {skill.get('start_url')}")
    print(f"  params  {', '.join(p['name'] for p in skill.get('params', [])) or '(none)'}")
    print(f"  actions {len(skill.get('actions', []))}"
          f"  ({sum(1 for a in skill.get('actions', []) if a.get('confirm'))} need confirming)")
    print(f"  human   {skill.get('human_baseline_seconds')}s per pass")
    print(f"\n-> {path}\n   review it, then: ./pipeline/run.py {opp['id']} --headed")


if __name__ == "__main__":
    main()
