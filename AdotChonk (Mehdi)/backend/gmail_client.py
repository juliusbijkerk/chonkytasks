"""Gmail API client: OAuth, search, read, create DRAFTS only (never sends).

Setup once: put OAuth client credentials (credentials.json) from Google Cloud
Console into the project root, then run:
    python -m backend.gmail_client auth
Scopes requested: gmail.readonly + gmail.compose (drafts). Sending is possible
in principle with gmail.compose but AutoPilot never sends without an explicit,
separate user action in the UI.
"""
import base64
import json
import os
from pathlib import Path
from typing import Optional

from . import config

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly",
          "https://www.googleapis.com/auth/gmail.compose"]
ROOT = config.ROOT
TOKEN_PATH = ROOT / "token.json"
CREDS_PATH = ROOT / "credentials.json"


class GmailNotConfigured(Exception):
    pass


def get_service():
    """Return an authorised Gmail service or raise GmailNotConfigured."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError as e:
        raise GmailNotConfigured(f"google client libs missing: {e}")

    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json())
    if not creds or not creds.valid:
        raise GmailNotConfigured(
            "Gmail not authorised. Put credentials.json in the project root and run "
            "`python -m backend.gmail_client auth`. Demo Mode does not need Gmail.")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _decode_payload(payload: dict) -> dict:
    """Extract headers + plain text body from a Gmail message payload."""
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    body_text = ""

    def walk(part):
        nonlocal body_text
        mime = part.get("mimeType", "")
        if mime == "text/plain" and part.get("body", {}).get("data"):
            body_text += base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", "replace")
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    return {
        "id": payload.get("id"),
        "thread_id": payload.get("threadId"),
        "from_addr": headers.get("from", ""),
        "from_name": headers.get("from", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "snippet": payload.get("snippet", ""),
        "body": body_text,
    }


def _from_addr(addr: str) -> tuple[str, str]:
    # "Jane Doe <jane@x.com>" -> ("jane@x.com", "Jane Doe")
    if "<" in addr and ">" in addr:
        name, email = addr.split("<", 1)
        return email.rstrip(">").strip(), name.strip().strip('"') or email
    return addr, addr


def search_messages(query: str, max_results: int = 20) -> list[dict]:
    service = get_service()
    resp = service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    out = []
    for m in resp.get("messages", []) or []:
        full = service.users().messages().get(userId="me", id=m["id"], format="full").execute()
        d = _decode_payload(full.get("payload", {}))
        d["from_addr"], d["from_name"] = _from_addr(d["from_addr"])
        out.append(d)
    return out


def create_draft(to: str, subject: str, body: str, thread_id: Optional[str] = None) -> dict:
    """Create a Gmail draft. NEVER sends."""
    service = get_service()
    from email.mime.text import MIMEText
    msg = MIMEText(body, "utf-8")
    msg["To"] = to
    msg["Subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    payload = {"message": {"raw": raw}}
    if thread_id:
        payload["message"]["threadId"] = thread_id
    return service.users().drafts().create(userId="me", body=payload).execute()


def auth_console() -> None:
    """One-time OAuth flow: python -m backend.gmail_client auth"""
    if not CREDS_PATH.exists():
        raise SystemExit("Missing credentials.json in project root (Google Cloud OAuth client).")
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_PATH), SCOPES)
    creds = flow.run_local_server(port=8765)
    TOKEN_PATH.write_text(creds.to_json())
    print(f"Gmail authorised. Token written to {TOKEN_PATH}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        auth_console()
    else:
        print("usage: python -m backend.gmail_client auth")
