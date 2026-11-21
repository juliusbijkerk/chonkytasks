#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx", "pillow"]
# ///
"""Run this the moment you have an API key. Before anything else.

    export MISTRAL_API_KEY=...
    ./pipeline/doctor.py

Five scripts in this pipeline call the API and none of them has ever run. The
model ids in them are guesses. This finds out what is actually available on
your account, picks the right model for each job, proves each call shape works
with a real request, and writes out/env.sh with the answers.

    source out/env.sh

Then every other script uses the ids that actually exist instead of my guesses.
"""
import base64, io, json, os, subprocess, sys, tempfile
from pathlib import Path
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mistral import text_of  # noqa: E402

BASE = "https://api.mistral.ai/v1"
OUT = Path("out")

G = "\033[32m"; R = "\033[31m"; Y = "\033[33m"; D = "\033[2m"; X = "\033[0m"
OK, BAD, WARN = f"{G}ok{X}", f"{R}FAIL{X}", f"{Y}warn{X}"


def say(status, what, detail=""):
    print(f"  {status:<14} {what}" + (f"\n                 {D}{detail}{X}" if detail else ""))


def tiny_png():
    """A 64x64 image with a word on it, so a vision reply is checkable."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (160, 80), (250, 250, 250))
    d = ImageDraw.Draw(img)
    d.rectangle([10, 10, 150, 70], outline=(10, 10, 10), width=3)
    d.text((28, 32), "INVOICE", fill=(10, 10, 10))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode()


def tiny_wav():
    """One second of tone. We only care that the endpoint accepts the upload."""
    p = Path(tempfile.gettempdir()) / "_doctor_tone.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-ar", "16000", "-ac", "1", str(p)], check=True)
    return p


def main():
    key = os.environ.get("MISTRAL_API_KEY")
    print()
    if not key:
        say(BAD, "MISTRAL_API_KEY is not set")
        print(f"\n  Make one at {D}https://console.mistral.ai/api-keys{X}, then:")
        print("    export MISTRAL_API_KEY=...\n")
        sys.exit(1)
    say(OK, "MISTRAL_API_KEY is set", f"{key[:6]}…{key[-4:]}")

    h = {"Authorization": f"Bearer {key}"}
    client = httpx.Client(timeout=120, headers=h)

    # ---------------------------------------------------------- models
    try:
        r = client.get(f"{BASE}/models")
    except Exception as e:
        say(BAD, "cannot reach the API", str(e)[:140])
        sys.exit(1)
    if r.status_code == 401:
        say(BAD, "the key was rejected (401)", "wrong key, or not activated yet")
        sys.exit(1)
    if r.status_code >= 400:
        say(BAD, f"/models returned {r.status_code}", r.text[:200])
        sys.exit(1)

    models = r.json().get("data", [])
    ids = sorted(m["id"] for m in models)
    say(OK, f"{len(ids)} models on this account")

    caps = {m["id"]: (m.get("capabilities") or {}) for m in models}

    def has_vision(mid):
        c = caps.get(mid, {})
        if "vision" in c:
            return bool(c["vision"])
        return any(k in mid for k in ("pixtral", "large", "medium"))

    def pick(cands, where):
        for c in cands:
            if c in ids:
                return c
        for i in ids:                      # fall back to a prefix match
            if any(i.startswith(c.split("-latest")[0]) for c in cands):
                return i
        say(WARN, f"no obvious model for {where}", f"choose from: {', '.join(ids[:8])}…")
        return None

    big = pick(["mistral-large-latest", "mistral-large-4", "mistral-medium-latest"], "reasoning")
    vision = next((m for m in [big] + ids if m and has_vision(m)), None)
    small = pick(["mistral-small-latest", "pixtral-12b-latest", "ministral-8b-latest"], "labelling")
    small_v = small if small and has_vision(small) else vision
    audio = pick(["voxtral-mini-latest", "voxtral-small-latest", "voxtral-mini-2507"], "voice")

    print()
    say(OK if big else WARN, f"reasoning   {big}")
    say(OK if vision else WARN, f"vision      {vision}")
    say(OK if small_v else WARN, f"labelling   {small_v}")
    say(OK if audio else WARN, f"voice       {audio}")
    print()

    passed = True

    # ------------------------------------------------- structured output
    if big:
        try:
            r = client.post(f"{BASE}/chat/completions", json={
                "model": big,
                "messages": [{"role": "user",
                              "content": 'Reply with {"ok": true} and nothing else.'}],
                "response_format": {"type": "json_object"},
                "temperature": 0, "top_p": 1,
            })
            if r.status_code >= 400:
                raise RuntimeError(r.text[:220])
            json.loads(text_of(r.json()))
            say(OK, "json response_format", "rank.py and compile.py will work")
        except Exception as e:
            passed = False
            say(BAD, "json response_format", str(e)[:200])

    # ------------------------------------------------------------ vision
    if vision:
        try:
            r = client.post(f"{BASE}/chat/completions", json={
                "model": vision,
                "messages": [{"role": "user", "content": [
                    {"type": "text",
                     "text": 'What single word is in the box? Reply {"word": "..."}'},
                    {"type": "image_url",
                     "image_url": f"data:image/jpeg;base64,{tiny_png()}"}]}],
                "response_format": {"type": "json_object"},
                "temperature": 0, "top_p": 1,
            })
            if r.status_code >= 400:
                raise RuntimeError(r.text[:220])
            got = json.loads(text_of(r.json()))
            word = str(got.get("word", "")).strip().lower()
            if "invoice" in word:
                say(OK, "vision with inline images", "observe.py and label.py will work")
            else:
                say(WARN, "vision call worked but misread the image",
                    f"said {word!r}, expected 'invoice'. Model may be weak at OCR.")
        except Exception as e:
            passed = False
            say(BAD, "vision with inline images", str(e)[:200])

    # ------------------------------------------------------------- audio
    if audio:
        try:
            wav = tiny_wav()
            r = client.post(f"{BASE}/audio/transcriptions",
                            files={"file": (wav.name, wav.read_bytes(), "audio/wav")},
                            data={"model": audio,
                                  "timestamp_granularities": "segment"})
            if r.status_code >= 400:
                raise RuntimeError(f"{r.status_code}: {r.text[:160]}")
            body = r.json()
            seg = "segments" in body and body["segments"] is not None
            say(OK, "audio transcription",
                "with segment timestamps" if seg
                else "but NO segment timestamps, so narration cannot be aligned to frames")
        except Exception as e:
            passed = False
            say(BAD, "audio transcription", str(e)[:200])
            say("", "", "if the endpoint is wrong, check docs.mistral.ai for Voxtral")

    # --------------------------------------------------------------- env
    OUT.mkdir(exist_ok=True)
    env = OUT / "env.sh"
    env.write_text("\n".join([
        "# written by pipeline/doctor.py from the models actually on this account",
        f'export MISTRAL_MODEL="{big or ""}"',
        f'export MISTRAL_VISION_MODEL="{vision or ""}"',
        f'export MISTRAL_LABEL_MODEL="{small_v or ""}"',
        f'export VOXTRAL_MODEL="{audio or ""}"',
        "",
    ]))

    print()
    if passed:
        say(OK, "every call shape works")
    else:
        say(BAD, "something above is broken. fix it before recording.")
    print(f"\n  wrote {env}. Use it in every shell that runs the pipeline:")
    print(f"    {D}source out/env.sh{X}\n")
    print(f"  {D}all models: {', '.join(ids)}{X}\n")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
