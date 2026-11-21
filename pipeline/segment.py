#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow"]
# ///
"""Frames -> segments. Break the recording into the parts it is actually made of.

    ./pipeline/segment.py

A segment is a stretch where the person stayed in one place: one site, one app,
one form. Boundaries are big visual jumps, which in practice are tab switches
and navigations.

This runs locally and costs nothing. It matters for two reasons:
  - observe.py gets a coherent, bounded chunk per call instead of an arbitrary
    batch of 6 frames that may straddle three different screens
  - steps come back belonging to a part, so the sequence reads as a story
    rather than a flat list
"""
import argparse, json
from pathlib import Path
from PIL import Image


def ahash(path, size=12):
    img = Image.open(path).convert("L").resize((size, size), Image.LANCZOS)
    px = list(img.tobytes())
    avg = sum(px) / len(px)
    return sum(1 << i for i, p in enumerate(px) if p > avg)


def dist(a, b):
    return bin(a ^ b).count("1")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default="out/frames/manifest.json")
    ap.add_argument("--out", default="out/segments.json")
    ap.add_argument("--cut", type=int, default=34,
                    help="hamming distance that counts as a scene change")
    ap.add_argument("--min-seconds", type=float, default=2.0,
                    help="segments shorter than this get folded into the previous "
                         "one. Folding by FRAME COUNT instead looks reasonable and "
                         "is wrong: one kept frame can stand for a long still "
                         "stretch, and folding it destroys a real scene cut.")
    args = ap.parse_args()

    frames = json.loads(Path(args.manifest).read_text())
    if not frames:
        raise SystemExit("No frames. Run sample_frames.py first.")

    hashes = [ahash(f["file"]) for f in frames]

    groups, cur = [], [0]
    for i in range(1, len(frames)):
        if dist(hashes[i], hashes[i - 1]) >= args.cut:
            groups.append(cur)
            cur = [i]
        else:
            cur.append(i)
    groups.append(cur)

    # A scene runs until the next one starts, not until its own last kept
    # frame. Those differ a lot once near-duplicates have been dropped.
    tail = frames[-1]["t"] - frames[-2]["t"] if len(frames) > 1 else 1.0

    def span(g, nxt):
        start = frames[g[0]]["t"]
        end = frames[nxt[0]]["t"] if nxt else frames[g[-1]]["t"] + tail
        return start, end

    # Fold away flicker (loading screens, a modal opening) by DURATION.
    merged = []
    for i, g in enumerate(groups):
        s, e = span(g, groups[i + 1] if i + 1 < len(groups) else None)
        if merged and (e - s) < args.min_seconds:
            merged[-1].extend(g)
        else:
            merged.append(g)

    segments = []
    for n, g in enumerate(merged, 1):
        s, e = span(g, merged[n] if n < len(merged) else None)
        segments.append({
            "id": f"seg_{n:02d}",
            "start": round(s, 1),
            "end": round(e, 1),
            "seconds": round(e - s, 1),
            "frames": [frames[i]["file"] for i in g],
            "frame_count": len(g),
            "thumb": frames[g[0]]["file"],
            "label": None,
        })

    Path(args.out).write_text(json.dumps(segments, indent=2))
    print(f"\n  {len(frames)} frames -> {len(segments)} segments\n")
    for s in segments:
        print(f"  {s['id']}  {s['start']:>6.1f}s - {s['end']:>6.1f}s"
              f"  ({s['frame_count']} frames, {s['seconds']}s)")
    print(f"\n  -> {args.out}")


if __name__ == "__main__":
    main()
