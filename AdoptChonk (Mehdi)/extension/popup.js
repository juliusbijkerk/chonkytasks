const pill = document.getElementById("pill");
const btn = document.getElementById("toggle");
const dur = document.getElementById("duration");
const status = document.getElementById("status");
let busy = false;
let note = "";   // one-off message shown until the next start/stop

const mmss = (ms) => {
  const s = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(sec).padStart(2, "0");
};
const hhmm = (t) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

function render(s) {
  const rec = s && s.recording;
  pill.className = "pill " + (rec ? "rec" : "idle");
  dur.disabled = !!rec;
  if (rec && s.live) {
    pill.textContent = `● LIVE ${mmss(Date.now() - (s.startedAt || Date.now()))}`;
    btn.textContent = "■ Stop live recording";
  } else if (rec) {
    pill.textContent = `● Recording ${mmss((s.endsAt || Date.now()) - Date.now())} left`;
    btn.textContent = "■ Stop & analyse";
  } else {
    pill.textContent = s && s.analysing ? "● Analysing with Mistral…" : "● Idle";
    btn.textContent = "▶ Start observation";
  }
  btn.disabled = busy || !!(s && s.analysing && !rec);

  let text = "";
  if (rec) {
    text = `${s.sent || 0} events sent${s.queued ? ` · ${s.queued} queued` : ""}`;
    if (s.live) text += "\nRecording the whole computer · tasks update on the dashboard every ~2 min";
  } else if (s && s.analysing) text = "Detecting repetitive tasks… (10-30 s)";
  else if (note) text = note;
  else if (s && s.lastAnalysis) text = `✅ Done: ${s.lastAnalysis.workflows} repetitive task(s) found.\nOpen the dashboard to see them.`;
  if (s && s.lastError) text = "⚠ " + s.lastError;
  status.textContent = text;
  status.className = "status" + (s && s.lastError ? " err" : "");
}

function refresh() {
  chrome.runtime.sendMessage({ type: "getState" }, (s) => {
    if (!chrome.runtime.lastError) render(s);
  });
}

btn.onclick = () => {
  busy = true;
  note = "";
  const type = btn.textContent.startsWith("▶") ? "start" : "stop";
  const live = dur.value === "live";
  chrome.runtime.sendMessage({ type, live, durationMin: live ? 0 : Number(dur.value) }, (r) => {
    busy = false;
    if (r && r.error) note = "⚠ Could not reach AutoPilot backend on localhost:8000\n" + r.error;
    else if (type === "stop" && r && !r.sent) note = "Stopped - no events were recorded, so nothing to analyse.";
    refresh();
  });
};

refresh();
setInterval(refresh, 1000);
