"""Process entry point for one laptop-agent run (started by the API).

    python -m backend.desktop.runner --run-id <id> --goal "<what to do>"

Runs the overlay (tkinter, main thread) and the agent (worker thread), then
writes the final report to the run. A separate process keeps GUI + global
mouse/keyboard control out of the web server and lets Stop kill it cleanly.
"""
import argparse
import threading

from .. import db
from .computer_agent import ComputerAgent, _dpi_aware
from .overlay import Overlay


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--run-id", required=True)
    p.add_argument("--goal", required=True)
    a = p.parse_args()

    _dpi_aware()                       # before any window / coordinate work
    holder: dict = {}
    overlay = Overlay(on_stop=lambda: holder["agent"].stop())
    agent = ComputerAgent(a.run_id, a.goal, step_cb=overlay.set_step)
    holder["agent"] = agent

    def work():
        report = {"mode": "desktop", "status": "failed"}
        try:
            report = agent.run()
        finally:
            db.finish_run(a.run_id, report)
            overlay.set_step({"done": "✅ Done", "stopped": "■ Stopped"}.get(report.get("status"), "Finished"))
            overlay.close()

    threading.Thread(target=work, daemon=True).start()
    overlay.run()


if __name__ == "__main__":
    main()
