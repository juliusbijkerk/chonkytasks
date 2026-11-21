"""Central configuration: loads .env and exposes typed settings."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent  # autopilot/
load_dotenv(ROOT / ".env")

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "").strip()
MISTRAL_LARGE_MODEL = os.getenv("MISTRAL_LARGE_MODEL", "mistral-large-latest")
MISTRAL_SMALL_MODEL = os.getenv("MISTRAL_SMALL_MODEL", "mistral-small-latest")
MISTRAL_VOICE_MODEL = os.getenv("MISTRAL_VOICE_MODEL", "voxtral-mini-latest")

HOURLY_RATE = float(os.getenv("HOURLY_RATE_GBP", "15"))
MINUTES_PER_EMAIL = float(os.getenv("MINUTES_PER_EMAIL", "3.5"))
GHOSTED_DAYS = int(os.getenv("GHOSTED_DAYS", "21"))
WORKDAYS_PER_WEEK = int(os.getenv("WORKDAYS_PER_WEEK", "5"))
USE_MISTRAL_AGENT = os.getenv("USE_MISTRAL_AGENT", "1") == "1"
USER_NAME = os.getenv("USER_NAME", "Alex")

TRACKER_PATH = Path(os.getenv("TRACKER_PATH", ROOT / "backend/demo_data/applications.xlsx")).resolve()
DB_PATH = Path(os.getenv("DB_PATH", ROOT / "backend/demo_data/autopilot.db")).resolve()
DEMO_DATA = ROOT / "backend" / "demo_data"

# Browser agent: update the tracker visibly in the Excel desktop app (1) or silently with openpyxl (0)
EXCEL_DESKTOP = os.getenv("EXCEL_DESKTOP", "1") == "1"

# Folders watched (non-recursively) for desktop Excel open/save while recording.
# WATCH_DIRS in .env: ";"-separated list. Default: tracker folder + Desktop/Documents/Downloads.
_home = Path.home()
_default_watch = [TRACKER_PATH.parent, _home / "Desktop", _home / "OneDrive" / "Bureau",
                  _home / "OneDrive" / "Desktop", _home / "Documents", _home / "OneDrive" / "Documents",
                  _home / "Downloads"]
WATCH_DIRS = [Path(p).expanduser() for p in os.getenv("WATCH_DIRS", "").split(";") if p.strip()] \
    or _default_watch
FRONTEND = ROOT / "frontend"
EXPORTED_SKILLS = ROOT / "exported_skills"
API_BASE = os.getenv("API_BASE", "http://localhost:8000")


def mistral_client():
    """Return a Mistral SDK client, or None when no API key is configured."""
    if not MISTRAL_API_KEY:
        return None
    from mistralai.client import Mistral
    return Mistral(api_key=MISTRAL_API_KEY)


def has_api_key() -> bool:
    return bool(MISTRAL_API_KEY)
