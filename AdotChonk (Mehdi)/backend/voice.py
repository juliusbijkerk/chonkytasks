"""Voxtral voice input: transcribe a mic recording and route simple commands.
Optional - never breaks the main flow. POST /api/voice with multipart 'file'.
"""
import json
import tempfile
from pathlib import Path
from typing import Optional

from . import config, db

ROUTE_SYSTEM = """You route voice commands for the AutoPilot app. Return STRICT JSON:
{"intent": "load_sample"|"run_rejections_dry"|"run_rejections_draft"|"show_dashboard"|"start_observe"|"stop_observe"|"none",
 "reply": "short spoken-style confirmation"}
Examples: "automate the rejection emails and show me the drafts" -> run_rejections_draft."""


def transcribe(audio_bytes: bytes, filename: str = "audio.webm") -> Optional[str]:
    if not config.has_api_key():
        return None
    from .mistral_util import transcribe as _tr
    for attempt in range(2):
        try:
            resp = _tr(audio_bytes, filename=filename, model=config.MISTRAL_VOICE_MODEL)
            text = resp.get("text") if isinstance(resp, dict) else None
            if text:
                return text.strip()
        except Exception as e:
            print(f"[voice] transcription attempt {attempt + 1} failed: {e}")
    return None


def route_command(transcript: str) -> dict:
    """Map a transcript to an app intent (Mistral small, keyword fallback)."""
    t = transcript.lower()
    if config.has_api_key():
        try:
            from .mistral_util import chat_complete, parse_json_response
            resp = chat_complete(
                messages=[{"role": "user", "content": transcript}],
                system=ROUTE_SYSTEM,
                model=config.MISTRAL_SMALL_MODEL,
                response_format={"type": "json_object"}, temperature=0.0, max_tokens=120)
            data = parse_json_response(resp["choices"][0]["message"]["content"])
            if data:
                return data
        except Exception as e:
            print(f"[voice] routing failed: {e}")
    if any(k in t for k in ("rejection", "automate", "draft")):
        return {"intent": "run_rejections_draft", "reply": "Running the rejection handler agent - check the drafts panel."}
    if "sample" in t or "demo" in t:
        return {"intent": "load_sample", "reply": "Loading the sample session."}
    if "start" in t and ("record" in t or "observ" in t):
        return {"intent": "start_observe", "reply": "Starting observation."}
    if "stop" in t:
        return {"intent": "stop_observe", "reply": "Stopping observation."}
    if any(k in t for k in ("dashboard", "show", "tasks")):
        return {"intent": "show_dashboard", "reply": "Showing your repetitive tasks."}
    return {"intent": "none", "reply": "Sorry, I didn't catch a command."}
