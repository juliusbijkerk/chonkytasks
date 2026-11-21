"""Shared Mistral call helper. Stdlib only, imported by the pipeline scripts.

Two things bit us on first contact with the real API, both silent until you
look. Everything goes through here so neither can come back.

1. `message.content` is NOT always a string.
   On reasoning models (mistral-large-4 and friends) it comes back as a list of
   blocks, `[{"type":"thinking",...},{"type":"text","text":"..."}]`. The JSON
   you asked for is inside the LAST text block. `json.loads(content)` throws
   TypeError on the list, and if you str() it you get the model's private
   reasoning glued to your payload. Smaller models still return a plain string,
   so code that works against mistral-small breaks against large.

2. `temperature: 0` is rejected with
   "top_p must be 1 when using greedy sampling" (error 3054).
   Greedy sampling needs top_p pinned. We send top_p=1 whenever temperature
   is 0, which is the thing you actually wanted.
"""
import json

API = "https://api.mistral.ai/v1/chat/completions"


def text_of(data):
    """Pull the assistant's text out of a completion response."""
    content = data["choices"][0]["message"]["content"]
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        if parts:
            return parts[-1]
        raise ValueError(
            "no text block in the reply, only "
            f"{[b.get('type') for b in content if isinstance(b, dict)]}")
    raise TypeError(f"unexpected content type: {type(content).__name__}")


def chat(client, key, model, messages, json_mode=True, temperature=0.1,
         timeout=None, **extra):
    """POST a completion and return the assistant's text.

    Raises with the server's own message on an error, because httpx's
    raise_for_status() hides the body and Mistral puts the real reason there.
    """
    payload = {"model": model, "messages": messages,
               "temperature": temperature, **extra}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if temperature == 0:
        payload.setdefault("top_p", 1)

    kw = {"json": payload}
    if timeout is not None:
        kw["timeout"] = timeout
    r = client.post(API, headers={"Authorization": f"Bearer {key}"}, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"{r.status_code} from {model}: {r.text[:300]}")
    return text_of(r.json())


def chat_json(client, key, model, messages, **kw):
    """chat(), parsed. Keeps the raw text in the error when it is not JSON."""
    txt = chat(client, key, model, messages, json_mode=True, **kw)
    try:
        return json.loads(txt)
    except json.JSONDecodeError as e:
        raise ValueError(f"{model} did not return JSON ({e}): {txt[:200]}") from None
