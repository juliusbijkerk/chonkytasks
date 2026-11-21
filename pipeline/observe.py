#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""Frames (+ optional narration) -> ObservedStep[] via Mistral vision.

    export MISTRAL_API_KEY=...        # console.mistral.ai/api-keys
    ./pipeline/observe.py --batch 6

Sends frames in small batches so one bad batch does not lose the run, and so
you can watch it stream in during the demo.
"""
import argparse, json, base64, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import httpx

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mistral import chat_json  # noqa: E402

API = "https://api.mistral.ai/v1/chat/completions"
MODEL = os.environ.get("MISTRAL_VISION_MODEL", "mistral-large-latest")

SYSTEM = """You watch screen recordings of office work and report what the person did.

Return ONLY a JSON object: {"steps": [ ... ]}. One entry per distinct action you
can actually see.

THE RULE THAT MATTERS MOST: report only what a frame SHOWS, never what must
have happened to produce it. A results page proves a search ran; it does NOT
prove you saw someone open the field, type, and click. Those are three steps
you did not witness. If a frame simply shows a state, that is one step at most.
Describe the difference between consecutive frames, never the actions you
imagine in the gap between them. Frames are seconds apart and most of what
happened in between is invisible to you. Missing a real step is fine. Inventing
one is not: the numbers we compute downstream are only as honest as this list.

Each step:
  t             seconds, from the timestamp given with the frame
  app           application name as shown
  surface       URL or window title, "" if unreadable
  action        one of: opened, clicked, typed into field, copied, pasted,
                switched app, scrolled, waited, submitted
  target        what was acted on, e.g. a field or button label
  value_kind    the KIND of data only (email, company, amount, date, name, id).
                NEVER copy the actual value. If you see a password or card
                number, set value_kind to "secret" and target to "REDACTED".
  narration     the narration line nearest this timestamp, or null
  confidence    0 to 1, your real confidence that this happened

Be honest in confidence, and note that 1.0 is almost never right. It means you
would stake the demo on it. A clear read of a labelled button is 0.8 to 0.9. A
guess at what a field contained is 0.4. A blurry or cropped frame is lower
still. We show this number to users, so a wall of 1.0s is worse than useless."""


def b64(p):
    return base64.b64encode(Path(p).read_bytes()).decode()


TS = re.compile(r"^\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)$")


def parse_narration(text):
    """[mm:ss.xx] line -> [(seconds, line)]. Untimed text is ignored, because
    narration we cannot place is narration we cannot trust."""
    out = []
    for ln in (text or "").splitlines():
        m = TS.match(ln.strip())
        if m:
            out.append((int(m.group(1)) * 60 + float(m.group(2)), m.group(3).strip()))
    return out


def narration_window(lines, start, end, pad=1.5):
    """Only what was said while these frames were on screen.

    Handing the whole transcript to every call was the bug: the model then
    attaches any line to any step, so a sentence from minute two shows up as
    the reason for a click in minute one. Worse, when the mic picks up the
    room rather than the person, every single step gets decorated with a
    stranger's conversation and it reads like evidence.
    """
    got = [t for ts, t in lines if start - pad <= ts <= end + pad and t]
    return "\n".join(got)


def call(client, key, frames, narration, seg=None):
    head = ("What the person said while these exact frames were on screen:\n"
            + (narration or "(they said nothing here)")
            + "\n\nUse a line as a step's narration ONLY if it plainly explains "
              "that step. Background chatter, other people talking, and remarks "
              "about something else all get narration: null. A wrong narration "
              "is worse than none, because it reads as the person's reasoning.")
    if seg:
        head = (f"This is one segment of the recording: {seg['id']}, "
                f"{seg['start']}s to {seg['end']}s. The person stayed on one "
                f"screen or site for this stretch.\n\n") + head
    content = [{"type": "text", "text": head}]
    for f in frames:
        content.append({"type": "text", "text": f"--- frame at t={f['t']}s ---"})
        content.append({"type": "image_url",
                        "image_url": f"data:image/jpeg;base64,{b64(f['file'])}"})
    got = chat_json(client, key, MODEL,
                    [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": content}])
    return got.get("steps", [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default="out/frames/manifest.json")
    ap.add_argument("--narration", default="out/narration.txt",
                    help="Voxtral transcript, optional")
    ap.add_argument("--batch", type=int, default=6)
    ap.add_argument("--segments", default="out/segments.json",
                    help="if present, observe one segment at a time instead of "
                         "fixed batches, so a call never straddles two screens")
    ap.add_argument("--out", default="out/steps.json")
    args = ap.parse_args()

    key = os.environ.get("MISTRAL_API_KEY")
    if not key:
        sys.exit("MISTRAL_API_KEY is not set. Make one at console.mistral.ai/api-keys")

    frames = json.loads(Path(args.manifest).read_text())
    narration_lines = parse_narration(
        Path(args.narration).read_text() if Path(args.narration).exists() else "")
    if narration_lines:
        print(f"  narration: {len(narration_lines)} timed lines")

    by_file = {f["file"]: f for f in frames}
    segs = []
    if Path(args.segments).exists():
        segs = json.loads(Path(args.segments).read_text())

    # Build the work list: one unit per segment when we have segments, else
    # fixed batches. A segment longer than --batch is split, but never merged
    # with its neighbour.
    units = []
    if segs:
        for sg in segs:
            fr = [by_file[f] for f in sg["frames"] if f in by_file]
            for i in range(0, len(fr), args.batch):
                units.append((sg, fr[i:i + args.batch]))
    else:
        for i in range(0, len(frames), args.batch):
            units.append((None, frames[i:i + args.batch]))

    # Units are independent, so several go to the API at once. Results are
    # collected here on the main thread, so the file writes never race.
    def work(n, sg, chunk):
        t0 = time.time()
        lo = min(f["t"] for f in chunk)
        hi = max(f["t"] for f in chunk)
        window = narration_window(narration_lines, lo, hi)
        return call(client, key, chunk, window, sg), time.time() - t0

    steps = []
    workers = max(1, int(os.environ.get("MISTRAL_CONCURRENCY", "4")))
    print(f"  {len(units)} calls, {workers} at a time")
    with httpx.Client(timeout=180) as client, ThreadPoolExecutor(workers) as pool:
        futs = {pool.submit(work, n, sg, chunk): (n, sg) for n, (sg, chunk) in enumerate(units)}
        for fut in as_completed(futs):
            n, sg = futs[fut]
            tag = sg["id"] if sg else f"batch_{n}"
            try:
                got, dt = fut.result()
            except Exception as e:
                print(f"  {tag} failed, skipping: {e}", file=sys.stderr)
                continue
            for st in got:
                st["segment"] = sg["id"] if sg else None
            steps.extend(got)
            print(f"  {tag}: {len(got)} steps ({dt:.1f}s)")
            # save as we go, so the UI can show steps while the rest are read
            Path(args.out).write_text(json.dumps(
                sorted(steps, key=lambda s: s.get("t", 0)), indent=2))

    steps.sort(key=lambda s: s.get("t", 0))
    Path(args.out).write_text(json.dumps(steps, indent=2))
    print(f"\n{len(steps)} steps -> {args.out}")
    if units and not steps:
        sys.exit("Every call failed, so there are no steps. See the errors above.")


if __name__ == "__main__":
    main()
