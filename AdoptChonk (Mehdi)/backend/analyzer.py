"""Analyzer: turn a raw event log into detected repetitive workflows.

Numbers (repetitions, minutes, weekly estimates) are always computed in plain
Python from real timestamps. Mistral Large is used for wording, step phrasing,
agent proposals and extra ideas (JSON mode), with a deterministic local
fallback when no API key is configured so Demo Mode always works.
"""
import json
import time
from datetime import datetime
from typing import Optional

from rapidfuzz import fuzz

from . import config, db
from .mistral_util import chat_complete, parse_json_response
from .models import AnalysisResult, AutomationProposal, Workflow

# ---------------------------------------------------------------------------
# Event -> steps preprocessing
# ---------------------------------------------------------------------------

def _human_step(e: dict) -> str:
    t, app = e.get("type", ""), (e.get("app") or "other").title()
    d = e.get("detail", {}) or {}
    label = str(d.get("label", ""))[:60]
    if t == "navigation":
        return f"Open {d.get('title') or d.get('url') or 'page'}"
    if t == "click":
        return f"Click \u201c{label}\u201d" if label else f"Click in {app}"
    if t == "input":
        return f"Fill {d.get('label') or 'field'} in {app}"
    if t == "copy_paste":
        return f"Copy/paste in {app}"
    if t == "tab_switch":
        return f"Switch tab to {d.get('to', 'other tab')}"
    if t == "app_switch":
        title = d.get("title") or app
        return f"Switch to {app}" if title.lower() == app.lower() else f"Work in {title} ({app})"
    if t == "idle":
        return f"Idle {d.get('seconds', '?')}s"
    return f"{t} in {app}"


def _mmss(ms: float) -> str:
    s = int(ms / 1000)
    return f"{s // 60}:{s % 60:02d}"


MOVE_TYPES = {"app_switch", "tab_switch", "navigation"}


def _is_noise_event(e: dict) -> bool:
    """Events that are never part of the user's work: AutoPilot's own dashboard,
    blank/system pages."""
    d = e.get("detail") or {}
    url = str(d.get("url", ""))
    if "localhost:8000" in url or "127.0.0.1:8000" in url or url.startswith(("chrome://", "edge://", "about:")):
        return True
    return "autopilot" in str(d.get("title", "")).lower()


def _drop_detours(events: list[dict]) -> list[dict]:
    """Remove one-off interruptions (a glance at music/chat/notifications in the middle
    of a task) so they don't break the repetition they interrupt: a visit to an app
    that happens only once in the session, lasts < 30 s and has <= 3 actions."""
    segs: list[list[dict]] = []
    for e in events:
        if segs and segs[-1][0].get("app") == e.get("app"):
            segs[-1].append(e)
        else:
            segs.append([e])
    visits: dict = {}
    for s in segs:
        visits[s[0].get("app")] = visits.get(s[0].get("app"), 0) + 1
    out = []
    for i, s in enumerate(segs):
        end = segs[i + 1][0]["timestamp_ms"] if i + 1 < len(segs) else s[-1]["timestamp_ms"]
        brief = (end - s[0]["timestamp_ms"]) < 30_000 and len(s) <= 3
        if brief and visits[s[0].get("app")] < 2 and 0 < i < len(segs) - 1:
            continue
        out.extend(s)
    return out


def detect_workflows(events: list[dict], max_workflows: int = 3) -> list[dict]:
    """Find repeated (app, type) n-grams in the event sequence. Pure Python."""
    events = _drop_detours([e for e in events if e.get("type") not in ("idle",) and not _is_noise_event(e)])
    evs = []
    for e in events:
        # collapse bursts of the same (app, type) - e.g. typing produces many input
        # events and is one step, not many repetitions
        if evs and (evs[-1].get("app"), evs[-1].get("type")) == (e.get("app"), e.get("type")):
            continue
        evs.append(e)
    if len(evs) < 6:
        return []
    seq = [((e.get("app") or "other"), e.get("type", "?")) for e in evs]
    ts = [e.get("timestamp_ms", 0) for e in evs]

    claimed: set[int] = set()
    found: list[dict] = []
    n_max = min(14, len(seq))
    while len(found) < max_workflows:
        # each round: across ALL lengths, pick the pattern covering the most events
        # (repetitions x length) - so a 12-step cycle seen 6x beats a 14-step one seen 3x
        best = None
        for n in range(n_max, 3, -1):
            counts: dict[tuple, list[int]] = {}
            for i in range(len(seq) - n + 1):
                if any(j in claimed for j in range(i, i + n)):
                    continue
                idxs_g = counts.setdefault(tuple(seq[i:i + n]), [])
                if not idxs_g or i >= idxs_g[-1] + n:   # non-overlapping occurrences only
                    idxs_g.append(i)
            for gram, idxs in counts.items():
                if len(idxs) < 2:
                    continue
                # skip a gram that is just a shorter cycle repeated (count the cycle instead)
                if any(n % p == 0 and gram == gram[:p] * (n // p) for p in range(1, n // 2 + 1)):
                    continue
                # noise: only moving around (switching apps/tabs, opening pages) without doing
                # anything there, or everything inside one app with no real action
                if all(t in MOVE_TYPES for _, t in gram):
                    continue
                if sum(1 for _, t in gram if t not in MOVE_TYPES) < 2:
                    continue
                # a real workflow takes time: ignore patterns averaging < 10 s per repetition
                if (ts[idxs[-1] + n - 1] - ts[idxs[0]]) / len(idxs) < 10_000:
                    continue
                score = (len(idxs) * n, n)
                if best is None or score > best[0]:
                    best = (score, gram, idxs)
        if not best:
            break
        _, gram, idxs = best
        n = len(gram)
        for i in idxs:
            for j in range(i, i + n):
                claimed.add(j)
        # human-readable steps from the first occurrence, collapsing consecutive dupes
        steps, last = [], None
        for i in range(idxs[0], idxs[0] + n):
            s = _human_step(evs[i])
            if s != last:
                steps.append(s)
                last = s
        reps = len(idxs)
        span_ms = ts[idxs[-1] + n - 1] - ts[idxs[0]]
        avg_min = (span_ms / reps / 60000.0) if reps else 0.0
        total_min = avg_min * reps
        weekly = total_min * config.WORKDAYS_PER_WEEK
        apps = []
        for a, _ in gram:
            if a not in apps:
                apps.append(a)
        times = [datetime.fromtimestamp(ts[i] / 1000).strftime("%H:%M:%S") for i in idxs]
        found.append({
            "name": " + ".join(apps[:2]) + " routine",
            "apps": apps,
            "steps": steps,
            "times_repeated_in_session": reps,
            "avg_minutes_per_repetition": round(avg_min, 2),
            "total_minutes_in_session": round(total_min, 2),
            "estimated_weekly_minutes": round(weekly, 1),
            "is_repetitive": True,
            "confidence": round(min(0.97, 0.55 + 0.09 * reps), 2),
            "evidence": [f"Repeated {reps}x at " + ", ".join(times),
                         f"Session span for this pattern: {_mmss(span_ms)} "
                         f"({round(avg_min, 1)} min per repetition)"],
            "_gram_types": [t for _, t in gram],
        })
    # rank by weekly cost
    found.sort(key=lambda w: w["estimated_weekly_minutes"], reverse=True)
    return found


# ---------------------------------------------------------------------------
# Local fallback proposals (used when Mistral is unavailable or fails)
# ---------------------------------------------------------------------------

def _local_name(wf: dict) -> str:
    steps_text = " ".join(wf["steps"]).lower()
    apps = [a.lower() for a in wf["apps"]]
    if "gmail" in apps and "search" in steps_text:
        return "Handle job rejection emails"
    if "docs" in apps or "drive" in apps:
        return "Assemble weekly report"
    if "copy/paste" in steps_text and len(apps) >= 2:
        return "Copy data between spreadsheet and web form"
    return " + ".join(wf["apps"][:2]) + " routine"


def _local_proposal(wf: dict) -> AutomationProposal:
    steps_text = " ".join(wf["steps"]).lower()
    apps = [a.lower() for a in wf["apps"]]
    if "gmail" in apps and "search" in steps_text:
        return AutomationProposal(
            title="Rejection Handler Agent",
            description="Finds rejection emails, drafts a polite feedback request for each, "
                        "and updates your application tracker automatically.",
            bullets=[
                "Searches Gmail for rejection phrases (unfortunately, not moving forward...)",
                "Classifies each email and drafts a personalised feedback request (draft only, never auto-sent)",
                "Matches the company/role to your applications.xlsx tracker and sets Status to Declined",
                "Marks old 'Applied' rows with no reply as Ghosted after 21 days",
            ],
            tools_needed=["Gmail", "Excel"],
            difficulty="Easy",
            setup_minutes=5,
            estimated_weekly_minutes_saved=wf["estimated_weekly_minutes"],
            risk_note="Replies are created as Gmail drafts only - nothing is sent without your approval. "
                      "The tracker is backed up before every write.",
            default_level="draft",
        )
    if ("docs" in apps or "drive" in apps) and "copy/paste" in steps_text:
        return AutomationProposal(
            title="Report Assembly Agent",
            description="Assembles your periodic report from Drive/Sheets into a document and shares it.",
            bullets=["Pulls the latest metrics file", "Pastes them into the report template",
                     "Shares the finished doc with your team"],
            tools_needed=["Drive", "Docs"],
            difficulty="Medium", setup_minutes=10,
            estimated_weekly_minutes_saved=wf["estimated_weekly_minutes"],
            risk_note="Preview the generated document before sharing.",
            default_level="suggest",
        )
    if "copy/paste" in steps_text and len(apps) >= 2:
        return AutomationProposal(
            title="Form Filler Agent",
            description="Copies rows from your spreadsheet into web forms and submits them for you.",
            bullets=["Reads new rows from the source spreadsheet",
                    "Fills the matching fields in the web form",
                    "Submits and logs the result back into the sheet"],
            tools_needed=["Sheets", "Web form"],
            difficulty="Medium", setup_minutes=15,
            estimated_weekly_minutes_saved=wf["estimated_weekly_minutes"],
            risk_note="Dry-run first: you approve each submission before it goes live.",
            default_level="suggest",
        )
    return AutomationProposal(
        title="Report Assembly Agent",
        description="Assembles your periodic report from Drive/Sheets into a document and shares it.",
        bullets=["Pulls the latest metrics file", "Pastes them into the report template",
                "Shares the finished doc with your team"],
        tools_needed=["Drive", "Docs"],
        difficulty="Medium", setup_minutes=10,
        estimated_weekly_minutes_saved=wf["estimated_weekly_minutes"],
        risk_note="Preview the generated document before sharing.",
        default_level="suggest",
    )


def _local_other_ideas(events: list[dict]) -> list[dict]:
    apps = {e.get("app", "other") for e in events}
    ideas = []
    if "Gmail" in apps:
        ideas.append({"name": "Inbox Zero triage agent", "description": "Labels, archives and drafts replies to routine emails (newsletters, receipts, acks).", "tools": ["Gmail"], "estimated_weekly_minutes_saved": 25})
    if any(a in apps for a in ("Sheets", "Excel Online")):
        ideas.append({"name": "Spreadsheet hygiene agent", "description": "Finds duplicate rows, fixes date formats and flags stale entries every week.", "tools": ["Sheets", "Excel"], "estimated_weekly_minutes_saved": 20})
    if "Docs" in apps or "Drive" in apps:
        ideas.append({"name": "Meeting notes summariser", "description": "Turns raw notes into a summary with action items and emails it to attendees.", "tools": ["Docs", "Gmail"], "estimated_weekly_minutes_saved": 30})
    ideas.append({"name": "Follow-up reminder agent", "description": "Detects applications with no reply after N days and drafts a polite follow-up.", "tools": ["Gmail", "Excel"], "estimated_weekly_minutes_saved": 15})
    return ideas[:5]


# ---------------------------------------------------------------------------
# Mistral analysis
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are AutoPilot's workflow analyst. You receive a compact summary of a recorded
work session covering the whole computer: desktop apps (which app/document was in use, button clicks,
typing bursts, copy/paste, save) and browser pages - never typed content.
Detect the repetitive WORK workflows and propose automations a non-technical user could adopt.

Separate real work from noise. NOISE = things that are not a task worth automating: glancing at
chat/music/social apps, checking notifications, idle app switching, the user navigating around
without completing anything, reading without acting. Each item of detected_repetitions_hint has an
index (0-based): list the indexes that are noise in "noise" - they will be discarded.

Return STRICT JSON with this schema:
{
  "noise": [<indexes of detected_repetitions_hint that are noise>],
  "workflows": [
    {
      "name": "short task name",
      "steps": ["human readable step 1", "step 2", ...],
      "apps": ["Gmail", "Excel"],
      "automation_proposal": {
        "title": "Agent name",
        "description": "one sentence",
        "bullets": ["what it does 1", "2", "3"],
        "tools_needed": ["Gmail"],
        "difficulty": "Easy|Medium|Hard",
        "setup_minutes": 5,
        "estimated_weekly_minutes_saved": 80,
        "risk_note": "safety note",
        "default_level": "suggest|draft|auto"
      }
    }
  ],
  "other_ideas": [{"name": "...", "description": "...", "tools": ["..."], "estimated_weekly_minutes_saved": 10}]
}
Numbers like repetitions/minutes are already computed for you in the hints - reuse those values
in estimated_weekly_minutes_saved. Keep wording plain and non-technical."""


def _compact_summary(events: list[dict], workflows: list[dict]) -> dict:
    evs = [e for e in events if e.get("type") != "idle" and not _is_noise_event(e)]
    duration_min = round((evs[-1]["timestamp_ms"] - evs[0]["timestamp_ms"]) / 60000, 1) if evs else 0
    # compact timeline: collapse consecutive identical (app,type) into one line with count
    timeline, last, count = [], None, 0
    for e in evs:
        key = (e.get("app"), e.get("type"))
        if key == last:
            count += 1
        else:
            if last:
                timeline.append({"app": last[0], "action": last[1], "x": count,
                                 "example": _human_step(last_e)})
            last, count, last_e = key, 1, e
    if last:
        timeline.append({"app": last[0], "action": last[1], "x": count, "example": _human_step(last_e)})
    hints = [{"index": i, **{k: v for k, v in w.items() if not k.startswith("_")}}
             for i, w in enumerate(workflows)]
    return {
        "session_duration_min": duration_min,
        "n_events": len(events),
        "timeline": timeline[:60],
        "detected_repetitions_hint": hints,
    }


def _call_mistral(summary: dict) -> Optional[dict]:
    if not config.has_api_key():
        return None
    user = "Recorded session summary (JSON):\n" + json.dumps(summary, ensure_ascii=False)
    for attempt in range(2):  # one retry on error / malformed JSON
        try:
            resp = chat_complete(
                messages=[{"role": "user", "content": user}],
                system=SYSTEM_PROMPT,
                model=config.MISTRAL_LARGE_MODEL,
                response_format={"type": "json_object"},
                temperature=0.2,
                max_tokens=4000,
                reasoning_effort="low",  # large-4 burns tokens on thinking otherwise
            )
            parsed = parse_json_response(resp["choices"][0]["message"]["content"])
            if parsed is None:
                raise ValueError("empty or non-JSON response")
            return parsed
        except Exception as e:
            print(f"[analyzer] mistral call attempt {attempt + 1} failed: {e}")
    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def analyze_session(session_id: str) -> AnalysisResult:
    events = db.list_events(session_id)
    detected = detect_workflows(events)
    summary = _compact_summary(events, detected)
    model_out = _call_mistral(summary)

    # Mistral flags detected patterns that are noise (not real work) -> drop them
    noise = set()
    for i in (model_out or {}).get("noise", []) or []:
        try:
            noise.add(int(i))
        except (TypeError, ValueError):
            pass
    if noise:
        dropped = [detected[i]["steps"][:3] for i in sorted(noise) if 0 <= i < len(detected)]
        if dropped:
            print(f"[analyzer] Mistral marked as noise: {dropped}")
        detected = [w for i, w in enumerate(detected) if i not in noise]

    model_wfs = (model_out or {}).get("workflows", [])
    # Pair model wording with grounded numbers (both sorted by weekly cost)
    model_wfs_sorted = sorted(model_wfs, key=lambda w: w.get("automation_proposal", {})
                              .get("estimated_weekly_minutes_saved", 0), reverse=True)

    def _sig(w: dict) -> str:
        # compare on name + apps + steps: the detector's local names are generic
        return " ".join([w.get("name", ""), " ".join(w.get("apps", []) or []),
                         " ".join(w.get("steps", []) or [])]).lower()

    remaining = list(model_wfs_sorted)
    workflows: list[Workflow] = []
    for det in detected:
        mw = None
        if remaining:
            # best match among the model workflows not yet used (each used at most once)
            mw = max(remaining, key=lambda w: fuzz.token_set_ratio(_sig(w), _sig(det)))
            remaining.remove(mw)
        proposal_data = None
        if mw and mw.get("automation_proposal"):
            proposal_data = dict(mw["automation_proposal"])
            proposal_data["estimated_weekly_minutes_saved"] = det["estimated_weekly_minutes"]
        proposal = AutomationProposal(**(proposal_data or _local_proposal(det).model_dump()))
        name = (mw or {}).get("name") or _local_name(det)
        steps = (mw or {}).get("steps") or det["steps"]
        apps = (mw or {}).get("apps") or det["apps"]
        workflows.append(Workflow(
            name=name, apps=apps, steps=steps,
            times_repeated_in_session=det["times_repeated_in_session"],
            avg_minutes_per_repetition=det["avg_minutes_per_repetition"],
            total_minutes_in_session=det["total_minutes_in_session"],
            estimated_weekly_minutes=det["estimated_weekly_minutes"],
            is_repetitive=True,
            confidence=det["confidence"],
            evidence=det["evidence"],
            automation_proposal=proposal,
        ))

    other_ideas = (model_out or {}).get("other_ideas") or _local_other_ideas(events)

    result = AnalysisResult(workflows=workflows, other_ideas=other_ideas)
    stored = {"session_id": session_id, "created_at": time.time(),
              "used_mistral": model_out is not None,
              "workflows": [w.model_dump() for w in workflows],
              "other_ideas": other_ideas}
    db.save_analysis(session_id, stored)
    db.save_tasks(session_id, stored["workflows"])
    return result
