#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""Give every segment a name. One vision call per part, two frames each.

    ./pipeline/label.py

Writes `label`, `app` and `summary` back into out/segments.json.

This is the cheapest pass in the pipeline and the one that does the most for
the demo: "seg_02, 0:12 to 0:24" means nothing to a judge, "Logging the price
in Notes" means everything. Set MISTRAL_LABEL_MODEL to a small vision model;
naming a screen does not need the big one.
"""
import argparse, base64, json, os, sys, time
from pathlib import Path
import httpx

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mistral import chat_json  # noqa: E402

API = "https://api.mistral.ai/v1/chat/completions"
MODEL = (os.environ.get("MISTRAL_LABEL_MODEL")
         or os.environ.get("MISTRAL_VISION_MODEL")
         or "mistral-large-latest")

SYSTEM = """You name one part of a screen recording of someone working.

You get one or two frames from the same stretch. Return ONLY:

{"label": "...", "app": "...", "summary": "..."}

  label    3 to 6 words, what the person is DOING here, in plain language.
           "Searching Dunelm for protectors", "Logging the price in Notes".
           Not "Browser window" and not "Step 2". A verb, always.
  app      the application or site, as a person would say it: "Dunelm",
           "Apple Notes", "Gmail". "" if you genuinely cannot tell.
  summary  one sentence on what this part is for, in the flow of the work.

Describe only what is visible. No guessing at what came before or after, and
never repeat a value you can read on screen: no names, emails, amounts or
numbers, just the kind of thing it is."""


def b64(p):
    return base64.b64encode(Path(p).read_bytes()).decode()


def label_one(client, key, seg):
    frames = seg.get("frames") or []
    # First and middle: the start of a scene is often still loading.
    picks = [frames[0]] if len(frames) == 1 else [frames[0], frames[len(frames) // 2]]
    content = [{"type": "text",
                "text": f"Part {seg['id']}, {seg['start']}s to {seg['end']}s."}]
    for f in picks:
        content.append({"type": "image_url",
                        "image_url": f"data:image/jpeg;base64,{b64(f)}"})
    return chat_json(client, key, MODEL,
                     [{"role": "system", "content": SYSTEM},
                      {"role": "user", "content": content}],
                     temperature=0.2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--segments", default="out/segments.json")
    ap.add_argument("--force", action="store_true",
                    help="relabel segments that already have a label")
    args = ap.parse_args()

    key = os.environ.get("MISTRAL_API_KEY")
    if not key:
        sys.exit("MISTRAL_API_KEY is not set.")

    path = Path(args.segments)
    if not path.exists():
        sys.exit("No segments.json. Run segment.py first.")
    segs = json.loads(path.read_text())

    print(f"  labelling {len(segs)} parts with {MODEL}\n")
    with httpx.Client(timeout=120) as client:
        for s in segs:
            if s.get("label") and not args.force:
                print(f"  {s['id']}  (already labelled, skipping)")
                continue
            t0 = time.time()
            try:
                got = label_one(client, key, s)
            except Exception as e:
                print(f"  {s['id']}  failed: {e}", file=sys.stderr)
                continue
            s["label"] = got.get("label")
            s["app"] = got.get("app")
            s["summary"] = got.get("summary")
            # Write after each one: a run that dies halfway still leaves the
            # parts it already named, which matters when you are on the clock.
            path.write_text(json.dumps(segs, indent=2))
            print(f"  {s['id']}  {s['label']}"
                  f"{'  [' + s['app'] + ']' if s.get('app') else ''}"
                  f"  ({time.time() - t0:.1f}s)")

    print(f"\n  -> {path}")


if __name__ == "__main__":
    main()
