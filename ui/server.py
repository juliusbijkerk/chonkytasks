#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""chonkytasks demo UI. Stdlib only, no build step, works with the wifi off.

    ./ui/server.py            then open http://localhost:8799

Upload a recording, watch it break into parts, see the steps, see what is worth
automating, then run the automation.

Agents: start at /llms.txt. /api lists every endpoint, /api/report.md is the
current state as Markdown, /api/state is the same as JSON.
"""
import json, mimetypes, os, re, shutil, subprocess, sys, threading, time
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"

# One stage at a time. The log is what the page shows while it waits.
JOBS = {}      # stage name -> {"proc", "started", "ended", "exit"}
LOG = []       # merged output of every stage, each line tagged with its stage
PIPE = {"active": False, "plan": [], "pending": [], "skipped": set(), "cancelled": set(),
        "failed": None, "started": 0, "ended": 0}
LOCK = threading.Lock()

# What each stage needs before it can start. Anything not on the same path runs
# side by side: transcribe alongside segment/label, describe alongside rank.
DEPS = {"frames": [], "segment": ["frames"], "transcribe": ["frames"],
        "label": ["segment"], "observe": ["segment", "transcribe"],
        "describe": ["observe"], "rank": ["observe"]}
SOFT = {"transcribe", "label"}   # nice to have: a failure here does not stop the run


def running_names():
    return [n for n, j in JOBS.items() if j["proc"].poll() is None]


def busy():
    return PIPE["active"] or bool(running_names())
RUN = {"proc": None, "id": None}

VIDEO_EXT = (".mp4", ".webm", ".mov", ".mkv", ".m4v")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".heic")
REC = {"file": None, "started": 0, "bytes": 0}   # live browser recording


def find_video():
    for ext in VIDEO_EXT:
        if (OUT / f"recording{ext}").exists():
            return OUT / f"recording{ext}"
    return None


def source():
    return jload(OUT / "source.json", None) or (
        {"kind": "video", "file": find_video().name} if find_video() else None)


def reset_outputs():
    """A new recording invalidates everything downstream of it."""
    for stale in ("segments.json", "steps.json", "process.json", "opportunities.json", "narration.txt",
                  "run.jsonl", "recording.wav", "source.json"):
        (OUT / stale).unlink(missing_ok=True)
    for ext in VIDEO_EXT:
        (OUT / f"recording{ext}").unlink(missing_ok=True)
    shutil.rmtree(OUT / "frames", ignore_errors=True)
    JOBS.clear()
    LOG.clear()


def frame_every(path, seconds=None):
    """Seconds between sampled frames. Dense for a 20 second demo clip, sparse
    for a recording that runs all week."""
    if not seconds:
        try:
            seconds = float(subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(path)], capture_output=True, text=True,
                timeout=20).stdout.strip())
        except Exception:
            seconds = None
    if not seconds:
        return 2
    return 1 if seconds <= 180 else 3 if seconds <= 2 * 3600 else 10


def frames_cmd(v):
    v = str(v or find_video() or "out/recording.mp4")
    every = (source() or {}).get("every") or frame_every(v)
    # Pull the audio out first so 'transcribe' has something to send to Voxtral.
    return ["bash", "-c",
            f'ffmpeg -nostdin -y -loglevel error -i "{v}" -vn -ac 1 -ar 16000 out/recording.wav '
            f'&& echo "audio -> out/recording.wav" || {{ rm -f out/recording.wav; echo "no audio track, skipping voice"; }}; '
            f'exec "{ROOT}/pipeline/sample_frames.py" "{v}" --every {every}']


PIPELINE = ["frames", "segment", "label", "transcribe", "observe", "describe", "rank"]

STAGES = {
    "frames":        lambda v: frames_cmd(v),
    "segment":       lambda v: ["pipeline/segment.py"],
    "label":         lambda v: ["pipeline/label.py"],
    "transcribe":    lambda v: ["pipeline/transcribe.py", "out/recording.wav"],
    "observe":       lambda v: ["pipeline/observe.py"],
    "describe":      lambda v: ["pipeline/describe.py"],
    # The agent ranker has the honest-loop fixes (no non-event loops, no monthly
    # figure from a too-short recording). rank.py stays as the fallback.
    "rank":          lambda v: ["bash", "-c", f'"{ROOT}/pipeline/rank_agent.py" || '
                                f'{{ echo "agent ranker failed, falling back to rank.py"; exec "{ROOT}/pipeline/rank.py"; }}'],
}


NAME = "chonkytasks"
TAGLINE = "Show it once. It finds the chonky tasks you repeat, then takes them over."

STAGE_INFO = [
    ("frames",     "Sample frames",    "mp4 -> out/frames/*.jpg (only frames that differ)"),
    ("segment",    "Find the parts",   "frames -> segments.json (split where the screen changes wholesale)"),
    ("label",      "Name the parts",   "segments.json -> a label per part"),
    ("transcribe", "Transcribe voice", "recording.wav -> narration.txt (Voxtral)"),
    ("observe",    "Read the steps",   "frames -> steps.json (ObservedStep[], vision pass)"),
    ("describe",   "Write up the process", "steps.json -> process.json (steps in plain words, repeats marked)"),
    ("rank",       "Find the loops",   "steps.json -> opportunities.json (Opportunity[])"),
]

API = [
    {"method": "GET",  "path": "/api/state", "returns": "JSON: job, stage status, segments, steps, opportunities, narration"},
    {"method": "GET",  "path": "/api/report.md", "returns": "Markdown summary of everything above. Best place for an agent to start."},
    {"method": "GET",  "path": "/api/process", "returns": "JSON: the process in plain steps, with repeated stretches marked as loops"},
    {"method": "GET",  "path": "/api/skill?id=<opportunity id>", "returns": "JSON: compiled skill (list of actions), or null if not compiled"},
    {"method": "GET",  "path": "/api/events?since=<n>", "returns": "JSON: run events after line n, plus cursor and running flag"},
    {"method": "GET",  "path": "/frames/<file>.jpg", "returns": "a sampled frame"},
    {"method": "POST", "path": "/api/upload", "body": "multipart/form-data, one video file", "does": "replaces the recording, clears downstream output, starts 'frames'"},
    {"method": "POST", "path": "/api/upload-images", "body": "multipart/form-data, one or more screenshots", "does": "replaces the input with screenshots (one frame each, in filename order)"},
    {"method": "POST", "path": "/api/record/start", "body": '{"mime": "video/webm"}', "does": "starts a streamed browser recording; clears downstream output"},
    {"method": "POST", "path": "/api/record/chunk", "body": "raw bytes", "does": "appends the next recorder chunk, in order"},
    {"method": "POST", "path": "/api/record/stop", "does": "finishes the recording and starts 'frames'"},
    {"method": "POST", "path": "/api/pipeline", "body": '{"stages": [...]} (optional)', "does": "runs those stages (default: everything not done), in parallel where they do not depend on each other"},
    {"method": "POST", "path": "/api/stage", "body": '{"name": "<stage>"}', "does": "runs one pipeline stage; 409 if one is already running"},
    {"method": "POST", "path": "/api/compile", "body": '{"id": "<opportunity id>"}', "does": "compiles an opportunity into a skill (blocking, up to 300s)"},
    {"method": "POST", "path": "/api/run", "body": '{"id": "<skill id>", "params": {...}, "headed": false}', "does": "replays the skill; poll /api/events"},
]


def stage_status():
    """done / running / failed / todo per stage, plus a short count."""
    segs = jload(OUT / "segments.json", [])
    facts = {
        "frames":     len(list((OUT / "frames").glob("*.jpg"))) if (OUT / "frames").exists() else 0,
        "segment":    len(segs),
        "label":      sum(1 for x in segs if x.get("label")),
        "transcribe": len((OUT / "narration.txt").read_text().split())
                      if (OUT / "narration.txt").exists() else 0,
        "observe":    len(jload(OUT / "steps.json", [])),
        "describe":   len((jload(OUT / "process.json", {}) or {}).get("steps", [])),
        "rank":       len(jload(OUT / "opportunities.json", [])),
    }
    unit = {"frames": "frames", "segment": "parts", "label": "named",
            "transcribe": "words", "observe": "actions", "describe": "steps", "rank": "loops"}
    out = []
    now = time.time()
    for name, label, what in STAGE_INFO:
        st = "done" if facts[name] else "todo"
        j = JOBS.get(name)
        secs = None
        if j:
            secs = round((j["ended"] or now) - j["started"], 1)
            if j["ended"] == 0:
                st = "running"
            elif j["exit"]:
                st = "failed"
        if name in PIPE["skipped"]:
            st = "skipped"
        elif name in PIPE["pending"]:
            st = "queued" if st != "done" else st
        out.append({"name": name, "label": label, "does": what, "status": st,
                    "count": facts[name], "unit": unit[name], "seconds": secs})
    return out


VERBS = {"typed into field": "Types into", "clicked": "Clicks", "pasted": "Pastes into",
         "copied": "Copies", "scrolled": "Scrolls to", "opened": "Opens", "submitted": "Submits",
         "switched app": "Switches to", "waited": "Waits for"}


def _site(surface):
    s = str(surface or "")
    return s.split("/")[0] if "." in s.split("/")[0] else s


def process():
    """The process in plain steps. describe.py writes the good version; until it
    has run (or while observe.py is still streaming) this builds a plain one
    from the raw actions, so there is always something to look at."""
    got = jload(OUT / "process.json", None)
    if got and got.get("steps"):
        return got
    raw = jload(OUT / "steps.json", [])
    if not raw:
        return None
    groups = []
    for i, a in enumerate(raw):
        key = (a.get("segment"), _site(a.get("surface")) or a.get("app"))
        if groups and groups[-1]["key"] == key:
            groups[-1]["actions"].append(i)
        else:
            groups.append({"key": key, "actions": [i]})
    labels = {s.get("id"): s.get("label") for s in jload(OUT / "segments.json", [])}
    steps = []
    for n, g in enumerate(groups, 1):
        acts = [raw[i] for i in g["actions"]]
        phr = [f"{VERBS.get(a.get('action'), str(a.get('action') or '').capitalize())} "
               f"{a.get('target') or ''}".strip() for a in acts[:2]]
        title = phr[0] + (", then " + phr[1][0].lower() + phr[1][1:] if len(phr) > 1 else "")
        where = _site(acts[0].get("surface")) or acts[0].get("app") or ""
        steps.append({
            "n": n, "title": title,
            "detail": (labels.get(g["key"][0]) or "") + (f" · on {where}" if where else ""),
            "app": acts[0].get("app") or "", "actions": g["actions"],
            "t_start": min(a.get("t", 0) for a in acts), "t_end": max(a.get("t", 0) for a in acts),
            "segment": g["key"][0],
            "narration": next((a["narration"] for a in acts if a.get("narration")), None)})
    loops = []
    opps = sorted(jload(OUT / "opportunities.json", []),
                  key=lambda o: -(o.get("projected_hours_per_month") or 0))
    if opps and opps[0].get("occurrences", 0) >= 2:
        idx = set(opps[0].get("step_indices") or [])
        hit = [s["n"] for s in steps if idx & set(s["actions"])]
        if len(hit) >= 1:
            loops.append({"from": min(hit), "to": max(hit), "times": opps[0]["occurrences"],
                          "label": opps[0].get("title") or ""})
    return {"summary": "", "source": "derived", "steps": steps, "loops": loops}


def report_md():
    st = stage_status()
    opps = sorted(jload(OUT / "opportunities.json", []),
                  key=lambda o: -(o.get("projected_hours_per_month") or 0))
    steps = jload(OUT / "steps.json", [])
    segs = jload(OUT / "segments.json", [])
    L = [f"# {NAME}: current state", "", TAGLINE, "",
         f"Input: {json.dumps(source()) if source() else 'none yet'}", "",
         "## Pipeline", "", "| stage | status | output |", "|---|---|---|"]
    L += [f"| {s['name']} | {s['status']} | {s['count']} {s['unit']} |" for s in st]
    L += ["", f"## Loops worth automating ({len(opps)})", ""]
    if not opps:
        L.append("None yet. Run the `rank` stage.")
    for i, o in enumerate(opps, 1):
        compiled = (OUT / "skills" / o.get("id", "") / "skill.json").exists()
        L += [f"### {i}. {o.get('title') or o.get('id')}  (`{o.get('id')}`)", "",
              f"- projected: **{o.get('projected_hours_per_month')} h/month** (extrapolated)",
              f"- measured: {o.get('observed_minutes')} min in the recording, "
              f"seen {o.get('occurrences')}x, median {o.get('median_seconds')} s each",
              f"- automatable: {o.get('automatable')}, confidence {o.get('confidence')}",
              f"- blockers: {', '.join(o.get('blockers') or []) or 'none'}",
              f"- why: {o.get('why_now') or ''}",
              f"- basis: {o.get('extrapolation_basis') or 'n/a'}",
              f"- skill compiled: {'yes, GET /api/skill?id=' + o['id'] if compiled else 'no, POST /api/compile'}",
              ""]
    pr = process()
    if pr:
        L += [f"## The process ({len(pr['steps'])} steps, {pr.get('source')})", ""]
        if pr.get("summary"):
            L += [pr["summary"], ""]
        for st in pr["steps"]:
            loop = next((lp for lp in pr.get("loops", []) if lp["from"] <= st["n"] <= lp["to"]), None)
            L.append(f"{st['n']}. {st['title']}" + (f" — {st['detail']}" if st.get("detail") else "")
                     + (f"  _(in loop ×{loop['times']})_" if loop else ""))
        for lp in pr.get("loops", []):
            L.append(f"- loop: steps {lp['from']}–{lp['to']} repeated {lp['times']}× {lp.get('label') or ''}")
        L.append("")
    L += [f"## Parts ({len(segs)})", ""]
    L += [f"- `{s.get('id')}` {s.get('label') or '(unlabelled)'}, "
          f"{s.get('start')}s to {s.get('end')}s, {s.get('frame_count')} frames" for s in segs]
    L += ["", f"## Steps ({len(steps)})", ""]
    for s in steps[:200]:
        line = (f"- {s.get('t')}s [{s.get('segment', '')}] {s.get('action')} "
                f"**{s.get('target')}** on {s.get('surface')} (conf {s.get('confidence')})")
        if s.get("narration"):
            line += f'\n  > "{s["narration"]}"'
        L.append(line)
    L += ["", "---", "Endpoints: GET /api  ·  docs: /llms.txt", ""]
    return "\n".join(L)


def llms_txt():
    L = [f"# {NAME}", "", f"> {TAGLINE}", "",
         "Local demo server. Record the screen in the browser (seconds for a demo, up to a work "
         "week for real discovery), or upload a video or screenshots. The pipeline "
         "splits it into parts, reads each step, finds repeated loops, ranks them by "
         "measured time, then compiles one into a replayable skill (a list of browser "
         "actions, never generated code) and runs it.", "",
         "## Start here", "",
         "- [Current state as Markdown](/api/report.md)",
         "- [Current state as JSON](/api/state)",
         "- [Endpoint manifest](/api)", "",
         "## Pipeline stages (run in order with POST /api/stage)", ""]
    L += [f"- `{n}`: {lbl}. {what}" for n, lbl, what in STAGE_INFO]
    L += ["", "## Endpoints", ""]
    for a in API:
        L.append(f"- `{a['method']} {a['path']}`" + (f" body `{a['body']}`" if a.get("body") else "")
                 + f": {a.get('does') or a.get('returns')}")
    L += ["", "## Rules", "",
          "- Only one stage runs at a time. Poll /api/state until job.running is false.",
          "- `observed_minutes` is measured; `projected_hours_per_month` is an extrapolation. Report both.",
          "- Skill actions marked `confirm` save, send or pay. The UI runs with --yes; ask a human before you do.",
          "", "## In the browser", "",
          "The page exposes `window.chonkytasks` (state(), runStage(name), runAll(), select(id), run({params, headed}), startRecording(), stopRecording()) "
          "and mirrors its state into `<script type=application/json id=agent-state>`. "
          "Every control carries a `data-action` attribute.", ""]
    return "\n".join(L)


def jload(p, default):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def pump(name, job):
    proc = job["proc"]
    for line in iter(proc.stdout.readline, b""):
        with LOCK:
            LOG.append(f"[{name}] {line.decode(errors='replace').rstrip()}")
            del LOG[:-600]
    job["exit"] = proc.wait()
    job["ended"] = time.time()
    with LOCK:
        LOG.append(f"[{name}] finished in {job['ended'] - job['started']:.1f}s"
                   + (f" (exit {job['exit']})" if job["exit"] else ""))


def stage_env():
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    # the Agents SDK (rank_agent.py) reads MISTRAL_CLIENT_API_KEY first
    if env.get("MISTRAL_API_KEY"):
        env.setdefault("MISTRAL_CLIENT_API_KEY", env["MISTRAL_API_KEY"])
    return env


def start_job(name, video=None):
    if name in running_names():
        return False, f"{name} is already running"
    cmd = [str(ROOT / c) if c.startswith("pipeline/") else c
           for c in STAGES[name](video)]
    with LOCK:
        LOG.append(f"[{name}] $ {' '.join(Path(c).name for c in cmd)}")
    job = {"started": time.time(), "ended": 0, "exit": None,
           "proc": subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT,
                                    env=stage_env())}
    JOBS[name] = job
    threading.Thread(target=pump, args=(name, job), daemon=True).start()
    return True, None


def run_pipeline(plan, video=None):
    """Run the stages in `plan`, each as soon as the ones it needs are done."""
    PIPE.update(active=True, plan=list(plan), pending=list(plan), skipped=set(),
                cancelled=set(), failed=None, started=time.time(), ended=0)
    finished = set(n for n in STAGES if n not in plan)
    try:
        while PIPE["pending"] or any(n in running_names() for n in plan):
            for n in plan:
                j = JOBS.get(n)
                if n in finished or n in PIPE["pending"] or not j or j["ended"] == 0:
                    continue
                finished.add(n)
                if j["exit"] and n not in SOFT:
                    PIPE["failed"] = n
                    PIPE["cancelled"] = set(PIPE["pending"])
                    PIPE["pending"] = []
            for n in list(PIPE["pending"]):
                if all(d in finished for d in DEPS[n]):
                    PIPE["pending"].remove(n)
                    if n == "transcribe" and not (OUT / "recording.wav").exists():
                        PIPE["skipped"].add(n)
                        finished.add(n)
                        continue
                    start_job(n, video if n == "frames" else None)
            time.sleep(0.3)
    finally:
        PIPE.update(active=False, ended=time.time())


def start_pipeline(plan, video=None):
    if busy():
        return False, "something is still running"
    JOBS.clear()
    threading.Thread(target=run_pipeline, args=(plan, video), daemon=True).start()
    return True, None


class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(ROOT / "ui"), **k)

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, text, ctype="text/markdown; charset=utf-8"):
        body = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path):
        if not path.exists() or not path.is_file():
            return self._json({"error": "not found"}, 404)
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---------------------------------------------------------------- GET

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path in ("/llms.txt", "/.well-known/llms.txt"):
            return self._text(llms_txt(), "text/plain; charset=utf-8")

        if u.path == "/api/report.md":
            return self._text(report_md())

        if u.path in ("/api", "/api/"):
            return self._json({"name": NAME, "about": TAGLINE, "docs": "/llms.txt",
                               "stages": [{"name": n, "label": l, "does": w}
                                          for n, l, w in STAGE_INFO],
                               "endpoints": API})

        if u.path == "/api/state":
            names = running_names()
            # a soft stage (voice, names) failing is shown on its line, not as a stopped run
            failed = PIPE["failed"] or next((n for n, j in JOBS.items() if j["exit"] and j["ended"]
                                             and (n not in SOFT or len(PIPE["plan"]) == 1)), None)
            total = ((time.time() if PIPE["active"] else PIPE["ended"]) - PIPE["started"]
                     if PIPE["started"] else 0)
            return self._json({
                "app": NAME,
                "source": source(),
                "has_input": bool(find_video() or list(OUT.glob("frames/*.jpg"))),
                "has_audio": (OUT / "recording.wav").exists(),
                "recording": {"live": REC["file"] is not None,
                              "seconds": round(time.time() - REC["started"], 1) if REC["file"] else 0,
                              "mb": round(REC["bytes"] / 1e6, 1)},
                "stages": stage_status(),
                "job": {"name": ", ".join(names) or failed, "running": busy(),
                        "running_stages": names,
                        "exit": (JOBS[failed]["exit"] if failed and not busy() else None),
                        "failed": failed if not busy() else None,
                        "log": LOG[-60:],
                        "elapsed": round(total, 1)},
                "pipeline": {"active": PIPE["active"], "plan": PIPE["plan"],
                             "seconds": round(total, 1)},
                "has_video": find_video() is not None,
                "segments": jload(OUT / "segments.json", []),
                "steps": jload(OUT / "steps.json", []),
                "process": process(),
                "input_mb": round(find_video().stat().st_size / 1e6, 1) if find_video() else None,
                "opportunities": jload(OUT / "opportunities.json", []),
                "narration": (OUT / "narration.txt").read_text()[:4000]
                             if (OUT / "narration.txt").exists() else "",
            })

        if u.path == "/api/process":
            return self._json(process())

        if u.path == "/api/skill":
            return self._json(jload(OUT / "skills" / q.get("id", [""])[0] / "skill.json", None))

        if u.path.startswith("/frames/"):
            return self._file(OUT / "frames" / Path(u.path).name)

        if u.path == "/api/events":
            since = int(q.get("since", ["0"])[0])
            p = OUT / "run.jsonl"
            lines = p.read_text().splitlines() if p.exists() else []
            evs = []
            for ln in lines[since:]:
                try:
                    evs.append(json.loads(ln))
                except Exception:
                    pass
            pr = RUN["proc"]
            return self._json({"events": evs, "cursor": len(lines),
                               "running": pr is not None and pr.poll() is None})

        return super().do_GET()

    # --------------------------------------------------------------- POST

    def do_POST(self):
        u = urlparse(self.path)

        if u.path == "/api/upload":
            ctype = self.headers.get("Content-Type", "")
            m = re.search(r"boundary=(?:\"([^\"]+)\"|([^;]+))", ctype)
            if "multipart/form-data" not in ctype or not m:
                return self._json({"error": "expected multipart"}, 400)
            # Hand-rolled, because cgi.FieldStorage is gone in Python 3.13 and
            # we do not control which interpreter uv picks. One file part only.
            boundary = ("--" + (m.group(1) or m.group(2)).strip()).encode()
            remaining = int(self.headers.get("Content-Length", 0))

            def readline():
                nonlocal remaining
                ln = self.rfile.readline()
                remaining -= len(ln)
                return ln

            if not readline().startswith(boundary):
                return self._json({"error": "malformed body"}, 400)
            if busy():
                return self._json({"error": "something is still running"}, 409)
            filename = None
            while True:                       # part headers, ends on a blank line
                ln = readline()
                if ln in (b"\r\n", b"\n", b""):
                    break
                fm = re.search(rb'filename="([^"]*)"', ln)
                if fm:
                    filename = fm.group(1).decode(errors="replace")
            if not filename:
                return self._json({"error": "no video"}, 400)

            ext = Path(filename).suffix.lower()
            if ext not in VIDEO_EXT:
                return self._json({"error": f"{ext or 'that file'} is not a video; "
                                            "send screenshots to /api/upload-images"}, 400)
            OUT.mkdir(exist_ok=True)
            reset_outputs()
            dest = OUT / f"recording{ext}"
            tail = len(boundary) + 6          # CRLF + boundary + "--" + CRLF
            with dest.open("wb") as f:
                while remaining > tail:
                    chunk = self.rfile.read(min(1 << 20, remaining - tail))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    f.write(chunk)
            self.rfile.read(max(0, remaining))
            # the writer above stops a few bytes early; trim the trailing CRLF
            with dest.open("r+b") as f:
                f.seek(0, 2)
                end = f.tell()
                f.seek(max(0, end - 64))
                blob = f.read()
                cut = blob.rfind(b"\r\n" + boundary[:20])
                if cut >= 0:
                    f.truncate(max(0, end - 64) + cut)
            (OUT / "source.json").write_text(json.dumps(
                {"kind": "video", "file": dest.name, "name": filename}))
            ok, err = start_pipeline(PIPELINE, dest)
            return self._json({"ok": ok, "error": err,
                               "mb": round(dest.stat().st_size / 1e6, 1)})

        if u.path == "/api/upload-images":
            # Screenshots skip the video step: each one becomes a frame.
            n = int(self.headers.get("Content-Length", 0))
            if n > 300e6:
                return self._json({"error": "too large, send fewer screenshots"}, 413)
            ctype = self.headers.get("Content-Type", "")
            m = re.search(r"boundary=(?:\"([^\"]+)\"|([^;]+))", ctype)
            if not m:
                return self._json({"error": "expected multipart"}, 400)
            if busy():
                return self._json({"error": "something is still running"}, 409)
            body = self.rfile.read(n)
            parts = []
            for chunk in body.split(b"--" + (m.group(1) or m.group(2)).strip().encode()):
                head, _, data = chunk.partition(b"\r\n\r\n")
                fm = re.search(rb'filename="([^"]*)"', head)
                if fm and data:
                    name = fm.group(1).decode(errors="replace")
                    if Path(name).suffix.lower() in IMAGE_EXT:
                        parts.append((name, data[:-2] if data.endswith(b"\r\n") else data))
            if not parts:
                return self._json({"error": "no images (png, jpg, webp, heic)"}, 400)
            reset_outputs()
            frames = OUT / "frames"
            frames.mkdir(parents=True)
            manifest = []
            for i, (name, data) in enumerate(sorted(parts, key=lambda x: x[0])):
                tmp = frames / f"in_{i:04d}{Path(name).suffix.lower()}"
                tmp.write_bytes(data)
                dst = frames / f"frame_{i:04d}.jpg"
                subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(tmp),
                                "-vf", "scale='min(1280,iw)':-2", "-q:v", "4", str(dst)],
                               capture_output=True, timeout=60)
                tmp.unlink(missing_ok=True)
                if dst.exists():
                    manifest.append({"file": f"out/frames/{dst.name}", "t": float(i * 5)})
            (frames / "manifest.json").write_text(json.dumps(manifest, indent=2))
            (OUT / "source.json").write_text(json.dumps(
                {"kind": "screenshots", "count": len(manifest)}))
            start_pipeline([n for n in PIPELINE if n not in ("frames", "transcribe")])
            return self._json({"ok": True, "frames": len(manifest)})

        # Browser recording, streamed in chunks so an hour (or a week) never
        # has to fit in the tab's memory.
        if u.path == "/api/record/start":
            if REC["file"] is not None:
                return self._json({"error": "already recording"}, 409)
            if busy():
                return self._json({"error": "something is still running"}, 409)
            n = int(self.headers.get("Content-Length", 0))
            mime = json.loads(self.rfile.read(n) or b"{}").get("mime", "")
            OUT.mkdir(exist_ok=True)

            # Archive whatever is already there before clearing anything.
            # Starting a recording used to truncate the previous one to zero
            # bytes, so a stray second press (or a curl you fired to see if the
            # route existed) destroyed the take you had just spent fifteen
            # minutes making, with no copy anywhere. Never again: old takes
            # move aside, they do not disappear.
            keep = OUT / "takes"
            for old in list(OUT.glob("recording.*")):
                if old.suffix in (".webm", ".mp4", ".wav") and old.stat().st_size > 0:
                    keep.mkdir(exist_ok=True)
                    stamp = time.strftime("%H%M%S", time.localtime(old.stat().st_mtime))
                    old.rename(keep / f"{stamp}-{old.name}")

            reset_outputs()
            REC.update(file=OUT / ("recording.mp4" if "mp4" in mime else "recording.webm"),
                       started=time.time(), bytes=0)
            REC["file"].write_bytes(b"")
            return self._json({"ok": True})

        if u.path == "/api/record/chunk":
            n = int(self.headers.get("Content-Length", 0))
            data = self.rfile.read(n)
            if REC["file"] is None:
                return self._json({"error": "not recording"}, 409)
            with REC["file"].open("ab") as f:
                f.write(data)
            REC["bytes"] += len(data)
            return self._json({"ok": True, "mb": round(REC["bytes"] / 1e6, 2)})

        if u.path == "/api/record/stop":
            if REC["file"] is None:
                return self._json({"error": "not recording"}, 409)
            dest, secs = REC["file"], time.time() - REC["started"]
            REC.update(file=None, started=0)
            if dest.stat().st_size == 0:
                dest.unlink()
                return self._json({"error": "nothing was recorded"}, 400)
            (OUT / "source.json").write_text(json.dumps(
                {"kind": "recording", "file": dest.name, "seconds": round(secs, 1),
                 "every": frame_every(dest, secs)}))
            ok, err = start_pipeline(PIPELINE, dest)
            return self._json({"ok": ok, "error": err, "seconds": round(secs, 1),
                               "mb": round(dest.stat().st_size / 1e6, 1)})

        if u.path == "/api/stage":
            n = int(self.headers.get("Content-Length", 0))
            name = json.loads(self.rfile.read(n) or b"{}").get("name")
            if name not in STAGES:
                return self._json({"error": f"unknown stage {name}"}, 400)
            if busy():
                return self._json({"error": "something is still running"}, 409)
            ok, err = start_pipeline([name])
            return self._json({"ok": ok, "error": err}, 200 if ok else 409)

        if u.path == "/api/pipeline":
            # Run the given stages, or everything not done yet, in dependency order.
            n = int(self.headers.get("Content-Length", 0))
            want = json.loads(self.rfile.read(n) or b"{}").get("stages")
            if not want:
                st = {x["name"]: x["status"] for x in stage_status()}
                want = [k for k in PIPELINE if st.get(k) != "done"]
            want = [k for k in PIPELINE if k in want]
            ok, err = start_pipeline(want)
            return self._json({"ok": ok, "error": err, "stages": want}, 200 if ok else 409)

        if u.path == "/api/run":
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            pr = RUN["proc"]
            if pr is not None and pr.poll() is None:
                return self._json({"error": "a run is already going"}, 409)
            (OUT / "run.jsonl").write_text("")
            cmd = [str(ROOT / "pipeline" / "run.py"), body["id"], "--yes"]
            if body.get("headed"):
                cmd += ["--headed", "--slow", "250"]
            for k, v in (body.get("params") or {}).items():
                cmd += ["--param", f"{k}={v}"]
            RUN["proc"] = subprocess.Popen(cmd, cwd=str(ROOT),
                                           stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL)
            RUN["id"] = body["id"]
            return self._json({"ok": True})

        if u.path == "/api/compile":
            n = int(self.headers.get("Content-Length", 0))
            oid = json.loads(self.rfile.read(n) or b"{}").get("id")
            p = subprocess.run([str(ROOT / "pipeline" / "compile.py"), oid],
                               cwd=str(ROOT), capture_output=True, timeout=300)
            return self._json({
                "ok": p.returncode == 0,
                "log": (p.stdout + p.stderr).decode(errors="replace")[-2000:],
            })

        return self._json({"error": "not found"}, 404)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    OUT.mkdir(exist_ok=True)
    print(f"\n  {NAME}  ->  http://localhost:{port}   (agents: /llms.txt)\n")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
