#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""ObservedStep[] -> Opportunity[], ranked by time actually observed.

    ./pipeline/rank.py --workdays-per-month 20

The extrapolation is kept in its own field. Show the measured number first when
you demo; a judge will ask where the big number came from.
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

# A projection from a short clip is arithmetic, but it is not evidence.
# 0.1 min of loop inside a 0.4 min span scales to "40 hours a month", which is
# true to the formula and absurd to a judge. Below this many minutes observed
# we still show the measured number and mark the projection unreliable.
MIN_SPAN_MIN = 5.0


SYSTEM = """You find repeated loops in a log of observed screen actions.

Return ONLY {"opportunities": [ ... ]}. A loop is a sequence of steps that
happens more than once with the same shape and different data.

Each opportunity:
  id                     opp_01, opp_02, ...
  title                  plain language, what a manager would call it
  step_indices           indices into the input array
  occurrences            how many times you saw the whole loop
  median_seconds         median wall time of one pass, from the timestamps
  automatable            full | partial | human_in_loop | no
  blockers               what stops it being full, [] if nothing
  why_now                one sentence: why this one is worth doing first
  confidence             0 to 1

Rules:
- Only count loops you saw at least twice. One occurrence is not a pattern.
- Judgment-heavy work is human_in_loop even if the clicking is repetitive.
- Do not invent time. Derive seconds from the t values you were given."""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", default="out/steps.json")
    ap.add_argument("--out", default="out/opportunities.json")
    ap.add_argument("--workdays-per-month", type=float, default=20.0)
    args = ap.parse_args()

    key = os.environ.get("MISTRAL_API_KEY")
    if not key:
        sys.exit("MISTRAL_API_KEY is not set.")

    steps = json.loads(Path(args.steps).read_text())
    if not steps:
        sys.exit("No steps. Run observe.py first.")

    span_min = (max(s.get("t", 0) for s in steps) - min(s.get("t", 0) for s in steps)) / 60 or 1

    with httpx.Client(timeout=240) as client:
        opps = chat_json(client, key, MODEL, [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(steps)},
        ])["opportunities"]

    opps = [o for o in opps
            if o.get("occurrences", 0) >= 2 and o.get("automatable") != "no"]
    for o in opps:
        observed = o.get("occurrences", 0) * o.get("median_seconds", 0) / 60
        o["observed_minutes"] = round(observed, 1)
        # honest extrapolation: rate per observed minute, scaled to a workday
        per_min = observed / span_min
        o["projected_hours_per_month"] = round(
            per_min * 60 * 8 * args.workdays_per_month / 60, 1)
        o["extrapolation_basis"] = (
            f"{observed:.1f} min of loop inside {span_min:.1f} min observed, "
            f"scaled to 8h x {args.workdays_per_month:.0f} days")
        o["extrapolation_reliable"] = span_min >= MIN_SPAN_MIN

    opps.sort(key=lambda o: o.get("observed_minutes", 0), reverse=True)
    Path(args.out).write_text(json.dumps(opps, indent=2))

    print(f"\nObserved {span_min:.1f} minutes. {len(opps)} loops found.\n")
    if span_min < MIN_SPAN_MIN:
        print(f"  NOTE: only {span_min:.1f} min observed. The per-month figures")
        print(f"  are arithmetic but not evidence. Record at least"
              f" {MIN_SPAN_MIN:.0f} minutes before showing them.\n")
    for o in opps:
        print(f"  {o['id']}  {o['title']}")
        print(f"       seen {o.get('occurrences')}x, {o['observed_minutes']} min measured"
              f"  ->  ~{o['projected_hours_per_month']} h/month")
        print(f"       {o.get('automatable')}  conf {o.get('confidence')}"
              f"  {'blockers: ' + ', '.join(o['blockers']) if o.get('blockers') else ''}")
    print(f"\n-> {args.out}")


if __name__ == "__main__":
    main()
