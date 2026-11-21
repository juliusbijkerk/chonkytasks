#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12,<3.15"
# dependencies = ["mistralai-agents==1.1.3"]
# ///
"""Find the loops with a Mistral Agent, but let a tool do the arithmetic.

    ./pipeline/rank_agent.py

Same output as rank.py, same out/opportunities.json, but built on the Agents
SDK with real tools.

Why this split matters, and it is the thing to say to the judges:

    The agent is good at seeing that steps 4-9 and 21-26 are the same loop.
    It is bad at arithmetic over timestamps, and that is exactly where our
    credibility lives: the whole pitch is "measured minutes, not guesswork".

    So the agent proposes a loop as a list of step indices, and `measure_loop`
    computes occurrences, median and observed minutes in Python. Every number
    on screen is arithmetic over real timestamps. The model never writes one.

Needs Python 3.12. Falls back cleanly: if this errors, rank.py still works.
"""
import argparse, asyncio, json, statistics, sys
from pathlib import Path
from mistralai import agents

STEPS: list = []
WORKDAYS = 20.0

# A projection from a short clip is arithmetic, but it is not evidence.
# 0.1 min of loop inside a 0.4 min span scales to "40 hours a month", which is
# true to the formula and absurd to a judge. Below this many minutes observed
# we still show the measured number and mark the projection unreliable.
MIN_SPAN_MIN = 5.0


INSTRUCTIONS = """You find repeated loops in a log of observed screen actions.

A loop is a sequence of steps that happens more than once with the same shape
and different data. Filling the same form twice is a loop. Doing something once
is not.

Work like this:
1. Call list_steps to see the log.
2. For each loop you think you see, call measure_loop with ONE pass of it:
   the step indices of a single occurrence, in order. It will find the other
   occurrences itself and give you the real numbers.
3. If measure_loop says a pattern occurs once, drop it. It is not a loop.
4. When you are done, call finish with the loops worth automating.

Never state a duration or a count yourself. measure_loop is the only source of
numbers. You decide what is a loop and whether it is worth automating; it does
the arithmetic."""

FOUND: list = []


@agents.tool
async def list_steps() -> list[dict]:
    """The observed screen actions, in order. Index is position in this list."""
    return [{"i": i, "t": s.get("t"), "action": s.get("action"),
             "target": s.get("target"), "surface": s.get("surface"),
             "app": s.get("app"), "segment": s.get("segment")}
            for i, s in enumerate(STEPS)]


@agents.tool
async def measure_loop(step_indices: list[int]) -> dict:
    """Measure one pass of a loop. Finds its other occurrences and times them.

    Pass the indices of a SINGLE occurrence, in order. Returns occurrences,
    median_seconds, observed_minutes and the other matches it found.
    """
    if not step_indices:
        return {"error": "no steps given"}
    try:
        pattern = [(STEPS[i].get("action"), STEPS[i].get("target"))
                   for i in step_indices]
    except IndexError:
        return {"error": f"index out of range, there are {len(STEPS)} steps"}

    n = len(pattern)
    shape = [a for a, _ in pattern]
    matches, i = [], 0
    while i <= len(STEPS) - n:
        window = [STEPS[j].get("action") for j in range(i, i + n)]
        if window == shape:
            matches.append(list(range(i, i + n)))
            i += n
        else:
            i += 1

    durations = []
    for m in matches:
        ts = [STEPS[j].get("t") for j in m if STEPS[j].get("t") is not None]
        if len(ts) >= 2:
            durations.append(max(ts) - min(ts))
    if not durations:
        return {"occurrences": len(matches), "median_seconds": 0,
                "observed_minutes": 0.0,
                "note": "no usable timestamps, cannot measure this one"}

    median = statistics.median(durations)
    observed = sum(durations) / 60
    return {
        "occurrences": len(matches),
        "median_seconds": round(median, 1),
        "observed_minutes": round(observed, 2),
        "occurrence_indices": matches,
        "note": ("seen once, this is not a loop" if len(matches) < 2 else
                 f"measured across {len(matches)} occurrences"),
    }


@agents.tool
async def finish(opportunities: list[dict]) -> str:
    """Record the final loops. Each needs: title, step_indices, automatable
    (full|partial|human_in_loop|no), blockers (list), why_now, confidence."""
    FOUND.clear()
    FOUND.extend(opportunities)
    return f"recorded {len(opportunities)}"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", default="out/steps.json")
    ap.add_argument("--out", default="out/opportunities.json")
    ap.add_argument("--model", default="mistral-large-latest")
    ap.add_argument("--workdays-per-month", type=float, default=20.0)
    args = ap.parse_args()

    global WORKDAYS
    WORKDAYS = args.workdays_per_month
    STEPS.extend(json.loads(Path(args.steps).read_text()))
    if not STEPS:
        sys.exit("No steps. Run observe.py first.")

    print(f"  {len(STEPS)} steps, agent on {args.model}\n")

    agent = agents.Agent(
        model=args.model,
        name="loop-finder",
        instructions=INSTRUCTIONS,
        tools=[list_steps, measure_loop, finish],
    )
    result = await agent.run(
        "Find every loop in this recording that is worth automating. "
        "Measure each one before you judge it, then call finish.")

    if not FOUND:
        print("  the agent never called finish. what it said:\n")
        print("   ", getattr(result, "text", result))
        sys.exit(1)

    span = (max(s.get("t", 0) for s in STEPS)
            - min(s.get("t", 0) for s in STEPS)) / 60 or 1

    opps = []
    for n, o in enumerate(FOUND, 1):
        m = await measure_loop(o.get("step_indices") or [])
        observed = m.get("observed_minutes", 0)
        if m.get("occurrences", 0) < 2 or o.get("automatable") == "no":
            print(f"  dropping {o.get('title')!r}: "
                  f"{'seen once' if m.get('occurrences',0) < 2 else 'not automatable'}")
            continue
        per_min = observed / span
        opps.append({
            "id": f"opp_{n:02d}",
            "title": o.get("title"),
            "step_indices": o.get("step_indices"),
            "occurrences": m.get("occurrences", 0),
            "median_seconds": m.get("median_seconds", 0),
            "observed_minutes": observed,
            "projected_hours_per_month": round(per_min * 8 * WORKDAYS, 1),
            "automatable": o.get("automatable"),
            "blockers": o.get("blockers") or [],
            "why_now": o.get("why_now"),
            "confidence": o.get("confidence"),
            "extrapolation_basis": (
                f"{observed} min of loop inside {span:.1f} min observed, "
                f"scaled to 8h x {WORKDAYS:.0f} days"),
            "extrapolation_reliable": span >= MIN_SPAN_MIN,
            "measured_by": "measure_loop tool, not the model",
        })

    opps.sort(key=lambda o: o["observed_minutes"], reverse=True)
    Path(args.out).write_text(json.dumps(opps, indent=2))

    print(f"\n  Observed {span:.1f} minutes. {len(opps)} loops.\n")
    if span < MIN_SPAN_MIN:
        print(f"  NOTE: only {span:.1f} min observed. The per-month figures are")
        print(f"  arithmetic but not evidence. Record at least {MIN_SPAN_MIN:.0f}"
              f" minutes before showing them to anyone.\n")
    for o in opps:
        print(f"  {o['id']}  {o['title']}")
        print(f"       seen {o['occurrences']}x, {o['observed_minutes']} min measured"
              f"  ->  ~{o['projected_hours_per_month']} h/month  [{o['automatable']}]")
    print(f"\n  -> {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
