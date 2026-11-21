"""Mistral API helpers.

Uses plain REST (httpx) instead of the SDK: the installed mistralai 3.8 SDK
has a bug with plain-dict kwargs ("process() takes no keyword arguments"),
while the REST endpoints work fine. Also robust content/JSON extraction for
models (mistral-large-4) that return content as a list of blocks.
"""
import json
from typing import Any, Optional

import httpx

from . import config

BASE = "https://api.mistral.ai/v1"


def _headers() -> dict:
    return {"Authorization": f"Bearer {config.MISTRAL_API_KEY}",
            "Content-Type": "application/json"}


def chat_complete(messages: list[dict], model: Optional[str] = None, *,
                  system: Optional[str] = None,
                  response_format: Optional[dict] = None,
                  tools: Optional[list] = None,
                  tool_choice: Optional[str] = None,
                  max_tokens: Optional[int] = None,
                  temperature: Optional[float] = None,
                  reasoning_effort: Optional[str] = None,
                  timeout: float = 120.0) -> dict:
    """POST /v1/chat/completions. Returns the raw response dict."""
    payload: dict[str, Any] = {"model": model or config.MISTRAL_LARGE_MODEL,
                               "messages": list(messages)}
    if system:
        payload["messages"] = [{"role": "system", "content": system}] + payload["messages"]
    if response_format:
        payload["response_format"] = response_format
    if tools:
        payload["tools"] = tools
    if tool_choice:
        payload["tool_choice"] = tool_choice
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if temperature is not None:
        payload["temperature"] = temperature
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    r = httpx.post(f"{BASE}/chat/completions", headers=_headers(), json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


def transcribe(audio_bytes: bytes, filename: str = "audio.webm",
               model: Optional[str] = None, timeout: float = 120.0) -> dict:
    """POST /v1/audio/transcriptions (Voxtral). Returns the raw response dict."""
    r = httpx.post(
        f"{BASE}/audio/transcriptions",
        headers={"Authorization": f"Bearer {config.MISTRAL_API_KEY}"},
        data={"model": model or config.MISTRAL_VOICE_MODEL},
        files={"file": (filename, audio_bytes, "application/octet-stream")},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def content_text(content: Any) -> str:
    """Extract plain text from a message content field (str or list of blocks)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text" and block.get("text"):
                    parts.append(block["text"])
                # thinking blocks are skipped on purpose
        return "".join(parts)
    return str(content)


def parse_json_response(content: Any) -> Optional[dict]:
    """Parse a JSON-mode model response; tolerate ```json fences. None on failure."""
    text = content_text(content).strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except Exception:
                return None
    return None
