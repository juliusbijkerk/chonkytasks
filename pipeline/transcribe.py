#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""Voice -> timestamped narration, via Voxtral.

    ./pipeline/transcribe.py out/shopping.wav

Writes out/narration.txt, which observe.py reads and aligns to frames by
timestamp. The narration is where the WHY lives: vision sees the click, the
voice says why that option and not the other one.
"""
import argparse, os, sys
from pathlib import Path
import httpx

API = "https://api.mistral.ai/v1/audio/transcriptions"
MODEL = os.environ.get("VOXTRAL_MODEL", "voxtral-mini-latest")


def hhmmss(sec):
    sec = float(sec or 0)
    return f"{int(sec // 60):02d}:{sec % 60:05.2f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--out", default="out/narration.txt")
    ap.add_argument("--language", default="en")
    args = ap.parse_args()

    key = os.environ.get("MISTRAL_API_KEY")
    if not key:
        sys.exit("MISTRAL_API_KEY is not set.")

    path = Path(args.audio)
    if not path.exists():
        sys.exit(f"No such file: {path}")

    mb = path.stat().st_size / 1e6
    print(f"  sending {path.name} ({mb:.1f} MB) to {MODEL}")

    with httpx.Client(timeout=600) as client:
        r = client.post(
            API,
            headers={"Authorization": f"Bearer {key}"},
            files={"file": (path.name, path.read_bytes(), "audio/wav")},
            data={
                "model": MODEL,
                "language": args.language,
                # segment granularity gives us start times, which is the whole
                # point: without them we cannot line narration up with frames.
                "timestamp_granularities": "segment",
            },
        )
        if r.status_code >= 400:
            sys.exit(f"Voxtral said {r.status_code}: {r.text[:400]}")
        data = r.json()

    segments = data.get("segments") or []
    lines = []
    if segments:
        for s in segments:
            lines.append(f"[{hhmmss(s.get('start'))}] {s.get('text', '').strip()}")
    else:
        # Older or reduced responses only carry the flat text. Still usable,
        # just without alignment, so say so rather than failing.
        print("  no segment timestamps came back, writing flat text")
        lines.append(data.get("text", "").strip())

    Path(args.out).write_text("\n".join(lines) + "\n")
    print(f"  {len(lines)} lines -> {args.out}")
    for ln in lines[:5]:
        print(f"    {ln[:90]}")


if __name__ == "__main__":
    main()
