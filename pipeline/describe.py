#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""ObservedStep[] -> the process, in words a colleague would use.

    ./pipeline/describe.py            # reads out/steps.json, writes out/process.json

observe.py reports raw actions ("clicked", "typed into field"). This pass groups
them into the steps a person would say they did ("Opens the LinkedIn email with
job suggestions") and marks the stretches they repeated, so the UI can show
the whole process at a glance. Reads ObservedStep only, per CONTRACT.md.

Output shape (also produced by ui/server.py as a fallback when this has not run):

    {"summary": "...", "source": "model",
     "steps": [{"n": 1, "title": "...", "detail": "...", "app": "...",
                "actions": [0, 1], "t_start": 0.0, "t_end": 9.0,
                "segment": "seg_01", "narration": "..." | null}],
     "loops": [{"from": 2, "to": 9, "times": 3, "label": "For each job"}]}
"""
import argparse, json, os, sys
from pathlib import Path
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mistral import chat_json  # noqa: E402

MODEL = os.environ.get("MISTRAL_MODEL", "mistral-large-latest")

SYSTEM = """You turn a log of screen actions into the process the person was following.

You get a numbered list of observed actions (index, time, app, page, action,
target, and sometimes what the person said out loud). Write the process the way
a colleague would explain it to a new hire: a short list of meaningful steps,
not a replay of every click.

Return ONLY a JSON object:
{"summary": "one sentence: what the person was doing overall",
 "steps": [{"title": "...", "detail": "...", "app": "...", "actions": [0, 1, 2]}],
 "loops": [{"from": 2, "to": 6, "times": 3, "label": "For each interesting job"}]}

Rules:
- title: starts with a verb, present tense, no subject. "Opens the LinkedIn
  email with job suggestions", "Copies the salary into the comparison sheet".
  Name the real things you can see (sites, documents, fields). Max ~10 words.
- detail: one short sentence on what or why. Use the narration when there is
  one; it is the only record of WHY. Otherwise describe what changed. Never
  invent reasons.
- actions: indices from the log. Every action belongs to exactly one step.
  Keep steps in the order they first happen.
- Repeats: when the person does the same sequence more than once (the same
  kind of work on a different item), write that sequence ONCE, put the actions
  from every pass under the matching step, and add a loop covering those steps
  (step numbers are 1-based, "times" is how many passes you saw). Only claim a
  loop you can see at least twice.
- 3 to 15 steps. Fewer, clearer steps beat a long list.
- Never copy values (emails, names, amounts). Say what kind of thing it was."""


def fmt(i, s):
    line = (f"[{i}] t={s.get('t')}s app={s.get('app') or '?'} page={s.get('surface') or '?'} "
            f"action={s.get('action')} target={s.get('target') or ''}")
    if s.get("value_kind"):
        line += f" ({s['value_kind']})"
    if s.get("narration"):
        line += f'  said: "{s["narration"]}"'
    return line


def enrich(steps, raw):
    """Fill in times, segment and narration from the actions, and renumber."""
    out = []
    for st in steps:
        idx = sorted({int(i) for i in st.get("actions", []) if 0 <= int(i) < len(raw)})
        if not idx:
            continue
        acts = [raw[i] for i in idx]
        out.append({
            "title": str(st.get("title") or "").strip() or acts[0].get("action", "step"),
            "detail": str(st.get("detail") or "").strip(),
            "app": st.get("app") or acts[0].get("app") or "",
            "actions": idx,
            "t_start": min(a.get("t", 0) for a in acts),
            "t_end": max(a.get("t", 0) for a in acts),
            "segment": acts[0].get("segment"),
            "narration": next((a["narration"] for a in acts if a.get("narration")), None),
        })
    out.sort(key=lambda s: s["actions"][0])
    for n, s in enumerate(out, 1):
        s["n"] = n
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", default="out/steps.json")
    ap.add_argument("--out", default="out/process.json")
    args = ap.parse_args()

    key = os.environ.get("MISTRAL_API_KEY")
    if not key:
        sys.exit("MISTRAL_API_KEY is not set. Make one at console.mistral.ai/api-keys")
    raw = json.loads(Path(args.steps).read_text())
    if not raw:
        sys.exit("No steps yet. Run observe.py first.")

    log = "\n".join(fmt(i, s) for i, s in enumerate(raw))
    with httpx.Client(timeout=180) as client:
        got = chat_json(client, key, MODEL,
                        [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": f"{len(raw)} observed actions:\n\n{log}"}])

    steps = enrich(got.get("steps", []), raw)
    n = len(steps)
    loops = []
    for lp in got.get("loops", []) or []:
        try:
            a, b, times = int(lp["from"]), int(lp["to"]), int(lp.get("times", 2))
        except (KeyError, TypeError, ValueError):
            continue
        if 1 <= a <= b <= n and times >= 2:
            loops.append({"from": a, "to": b, "times": times, "label": str(lp.get("label") or "")})

    Path(args.out).write_text(json.dumps(
        {"summary": got.get("summary", ""), "source": "model", "steps": steps, "loops": loops}, indent=2))
    print(f"{len(raw)} actions -> {n} process steps, {len(loops)} loop(s) -> {args.out}")
    for s in steps:
        print(f"  {s['n']:>2}. {s['title']}")


if __name__ == "__main__":
    main()
