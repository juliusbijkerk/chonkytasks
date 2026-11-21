#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow"]
# ///
"""Sample frames from a screen recording and throw away the boring ones.

    ./pipeline/sample_frames.py recording.mp4 --every 3 --max-width 1280

Why dedupe: 10 minutes at 1 fps is 600 frames. Most are identical because the
person was reading. Perceptual hashing drops those before you pay for vision.
Typical result is 600 frames down to 40 or so.
"""
import argparse, json, subprocess, sys
from pathlib import Path
from PIL import Image


def ahash(path, size=24):
    """Average hash. Small, fast, good enough to spot 'nothing changed'."""
    img = Image.open(path).convert("L").resize((size, size), Image.LANCZOS)
    px = list(img.tobytes())
    avg = sum(px) / len(px)
    return sum(1 << i for i, p in enumerate(px) if p > avg)


def dist(a, b):
    return bin(a ^ b).count("1")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--every", type=float, default=3.0, help="seconds between frames")
    ap.add_argument("--max-width", type=int, default=1280)
    ap.add_argument("--threshold", type=int, default=10,
                    help="hamming distance below which a frame counts as unchanged. "
                         "Over a 24x24 hash this is ~1.7%% of the image. A coarse "
                         "hash with a loose threshold throws away real work: typing "
                         "into one field changes a tiny fraction of a 1440p screen, "
                         "and at 12x12/8 that read as 'nothing happened'. A 2:26 "
                         "recording went 72 frames -> 12 kept, and the vision pass "
                         "duly reported nine 'waited' steps.")
    ap.add_argument("--out", default="out/frames")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.jpg"):
        old.unlink()

    subprocess.run([
        "ffmpeg", "-loglevel", "error", "-i", args.video,
        "-vf", f"fps=1/{args.every},scale={args.max_width}:-2",
        "-q:v", "4", str(out / "raw_%05d.jpg"),
    ], check=True)

    raw = sorted(out.glob("raw_*.jpg"))
    if not raw:
        sys.exit("ffmpeg produced no frames. Is the path right?")

    kept, last = [], None
    for f in raw:
        h = ahash(f)
        if last is None or dist(h, last) >= args.threshold:
            kept.append(f)
            last = h
        else:
            f.unlink()

    manifest = []
    for i, f in enumerate(kept):
        idx = int(f.stem.split("_")[1])
        new = out / f"frame_{i:04d}.jpg"
        f.rename(new)
        manifest.append({"file": str(new), "t": round((idx - 1) * args.every, 1)})

    Path(args.out).joinpath("manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"{len(raw)} sampled -> {len(kept)} kept ({len(raw) - len(kept)} near-duplicates dropped)")
    print(f"manifest: {out / 'manifest.json'}")


if __name__ == "__main__":
    main()
