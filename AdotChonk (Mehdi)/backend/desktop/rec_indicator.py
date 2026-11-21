"""On-screen indicator while AutoPilot records the computer (red glow + dock).

    python -m backend.desktop.rec_indicator --session <id>

Started by the desktop observer when a recording session opens; closes itself
when the session is stopped (from here, the extension or the dashboard).
"""
import argparse
import json
import threading
import time
import urllib.request

from .. import config, db
from .computer_agent import _dpi_aware
from .overlay import GLOW_REC, Overlay


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--session", required=True)
    sid = p.parse_args().session
    _dpi_aware()
    stopping = threading.Event()

    def stop():
        if stopping.is_set():
            return
        stopping.set()
        ov.set_step("Stopping and analysing with Mistral…")

        def call():
            try:
                req = urllib.request.Request(f"{config.API_BASE}/api/sessions/analyze-latest", data=b"",
                                             method="POST")
                body = json.loads(urllib.request.urlopen(req, timeout=180).read())
                n = len((body.get("analysis") or {}).get("workflows", []))
                ov.set_step(f"✅ {n} repetitive task(s) found - see the dashboard")
            except Exception as e:
                ov.set_step(f"⚠ analysis failed: {e}")
            ov.close()
        threading.Thread(target=call, daemon=True).start()

    ov = Overlay(on_stop=stop, title="RECORDING", glow=GLOW_REC, stop_text="■  Stop & analyse",
                 hint="Records apps, windows & clicks - never what you type", logo_bg="#dc2626",
                 alpha_range=(0.25, 0.6))

    def watch():
        while True:
            s = db.get_session(sid)
            if not s or s.get("ended_at"):
                if not stopping.is_set():
                    ov.set_step("Recording stopped")
                    ov.close()
                return
            if not stopping.is_set():
                evs = db.list_events(sid)
                apps = []
                for e in evs:
                    if e.get("app") and e["app"] not in apps and e.get("type") != "idle":
                        apps.append(e["app"])
                live = "live" in (s.get("label") or "")
                ov.set_step(f"{'LIVE · ' if live else ''}{len(evs)} actions recorded\n"
                            f"Apps: {', '.join(apps[-4:]) or '-'}")
            time.sleep(2)

    threading.Thread(target=watch, daemon=True).start()
    ov.run()


if __name__ == "__main__":
    main()
