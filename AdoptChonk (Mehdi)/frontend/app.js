/* AutoPilot dashboard - vanilla JS, talks to the FastAPI backend. */
const $ = (sel) => document.querySelector(sel);
const api = {
  get: (p) => fetch(p).then((r) => r.json()),
  post: (p, body) => fetch(p, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}) }).then((r) => r.json()),
};

let chart = null;
let currentSession = null;
let recTimer = null;

/* ---------------- dashboard ---------------- */

async function loadDashboard() {
  const d = await api.get("/api/dashboard");
  renderSummary(d.summary);
  renderTasks(d.tasks);
  renderIdeas(d.other_ideas || []);
  renderChart(d.tasks);
  $("#keyNote").textContent = d.has_api_key
    ? `Mistral models active (${d.models.large} / ${d.models.small})`
    : "running in Demo Mode (no MISTRAL_API_KEY - add it to .env to enable Mistral analysis)";
  return d;
}

function renderSummary(s) {
  $("#stTasks").textContent = s.tasks_found;
  $("#stLost").textContent = s.hours_lost_week + " h";
  $("#stSaved").textContent = s.hours_saved_week + " h";
  $("#stMoney").textContent = "£" + s.monthly_saving_gbp;
}

function confBadge(c) {
  const cls = c >= 0.85 ? "high" : c >= 0.7 ? "mid" : "low";
  return `<span class="badge ${cls}">${Math.round(c * 100)}% confidence</span>`;
}

function renderTasks(tasks) {
  const list = $("#taskList");
  $("#emptyState").classList.toggle("hidden", tasks.length > 0);
  if (!tasks.length) { list.innerHTML = ""; return; }
  list.innerHTML = "";
  tasks.forEach((t) => list.appendChild(taskCard(t)));
}

function taskCard(t) {
  const p = t.proposal || {};
  const card = document.createElement("div");
  card.className = "task-card";
  const steps = (t.steps || []).slice(0, 8);
  const stepper = steps.map((s, i) =>
    `${i ? '<span class="step-arrow">→</span>' : ""}<span class="step">${esc(s)}</span>`).join("");
  const reps = t.repetitions || 1;
  // one bar per repetition (each ≈ avg minutes); capped so the card stays readable
  const shown = Math.min(reps, 12);
  const bars = Array.from({ length: shown }, () => `<div class="bar" style="height:26px"></div>`).join("")
    + (reps > shown ? `<span class="spark-label" style="margin-left:6px">+${reps - shown}</span>` : "");
  card.innerHTML = `
    <div>
      <div class="task-head">
        <span class="task-name">${esc(t.name)}</span>
        ${(t.apps || []).map((a) => `<span class="chip">${esc(a)}</span>`).join("")}
        ${confBadge(t.confidence)}
        <span class="badge status">${esc(t.status)}</span>
      </div>
      <div class="evidence">
        Repeated <b>${reps}×</b> in the recorded session · avg <b>${t.avg_minutes} min</b> each ·
        ≈ <b>${t.weekly_minutes} min/week</b> (${(t.weekly_minutes / 60).toFixed(1)} h)
      </div>
      <div class="stepper">${stepper}</div>
      <div class="spark">${bars}</div>
      <div class="spark-label">${reps} repetitions · ~${t.avg_minutes} min each</div>
      <button class="why-toggle">Why do we think this is repetitive? ▾</button>
      <div class="why-panel hidden">${esc((t.evidence || []).join("\n"))}</div>
    </div>
    <div class="agent-panel">
      <h3>🤖 ${esc(p.title || "Automation")}</h3>
      <div class="agent-desc">${esc(p.description || "")}</div>
      <ul class="agent-bullets">${(p.bullets || []).map((b) => `<li>${esc(b)}</li>`).join("")}</ul>
      <div class="agent-meta">
        <span>🛠 ${(p.tools_needed || []).join(", ")}</span>
        <span>⚡ ${esc(p.difficulty || "Easy")}</span>
        <span>⏱ setup ~${p.setup_minutes ?? 10} min</span>
        <span>💾 saves ~${Math.round(p.estimated_weekly_minutes_saved || 0)} min/week</span>
      </div>
      <div class="risk">🛡 ${esc(p.risk_note || "Runs in dry-run first; you approve every action.")}</div>
      <div class="level-row">Automation level:
        <select id="level-${t.id}">
          <option value="dry_run" ${p.default_level === "suggest" ? "selected" : ""}>Suggest only (dry run)</option>
          <option value="draft" ${p.default_level === "draft" ? "selected" : ""}>Draft and ask me</option>
          <option value="auto" ${p.default_level === "auto" ? "selected" : ""}>Fully automatic</option>
          ${(t.apps || []).some((a) => /gmail/i.test(a))
            ? `<option value="browser">🖥 Do it in my browser (real Gmail, drafts only)</option>` : ""}
        </select>
      </div>
      <div class="agent-actions">
        <button class="btn small ghost" data-act="preview">👁 Preview (dry run)</button>
        <button class="btn small primary" data-act="run">▶ Run agent now</button>
        <button class="btn small" data-act="export">📦 Export as Vibe skill</button>
        <button class="btn small" data-act="schedule">🗓 Schedule weekly</button>
        <button class="btn small ghost" data-act="dismiss">✖ Not interested</button>
      </div>
    </div>`;
  card.querySelector(".why-toggle").onclick = (e) => {
    const panel = card.querySelector(".why-panel");
    panel.classList.toggle("hidden");
    e.target.textContent = panel.classList.contains("hidden")
      ? "Why do we think this is repetitive? ▾" : "Why do we think this is repetitive? ▴";
  };
  card.querySelector('[data-act="preview"]').onclick = () => runAgent(t.id, "dry_run");
  card.querySelector('[data-act="run"]').onclick = () => runAgent(t.id, $("#level-" + t.id).value);
  card.querySelector('[data-act="export"]').onclick = () => exportSkill(t.id, card);
  card.querySelector('[data-act="schedule"]').onclick = () => scheduleTask(t.id);
  card.querySelector('[data-act="dismiss"]').onclick = () => dismissTask(t.id);
  return card;
}

function renderIdeas(ideas) {
  const el = $("#ideaList");
  el.innerHTML = "";
  ideas.forEach((i) => {
    const d = document.createElement("div");
    d.className = "idea";
    d.innerHTML = `<h4>🤖 ${esc(i.name)}</h4><p>${esc(i.description || "")}</p>
      <div class="agent-meta"><span>🛠 ${(i.tools || []).join(", ")}</span>
      <span>💾 ~${i.estimated_weekly_minutes_saved ?? "?"} min/week</span></div>`;
    el.appendChild(d);
  });
}

function renderChart(tasks) {
  const ctx = $("#impactChart").getContext("2d");
  const labels = tasks.map((t) => t.name.length > 28 ? t.name.slice(0, 27) + "…" : t.name);
  const manual = tasks.map((t) => +(t.weekly_minutes / 60).toFixed(2));
  const remaining = tasks.map((t, i) => Math.max(0.05, +(manual[i] - ((t.proposal || {}).estimated_weekly_minutes_saved || 0) / 60).toFixed(2)));
  if (chart) chart.destroy();
  chart = new Chart(ctx, {
    type: "bar",
    data: { labels, datasets: [
      { label: "Manual time (h/week)", data: manual, backgroundColor: "#fca5a5", borderRadius: 6 },
      { label: "Time left with AutoPilot (h/week)", data: remaining, backgroundColor: "#86efac", borderRadius: 6 },
    ]},
    options: { responsive: true, plugins: { legend: { position: "bottom" } },
      scales: { y: { beginAtZero: true, title: { display: true, text: "hours / week" } } } },
  });
}

/* ---------------- sessions ---------------- */

// Recording itself happens in the Chrome extension (a web page cannot see other
// tabs). This page mirrors the extension's state by polling the backend.
let wasRecording = false;
let analysing = false;

function showNote(text, ms = 8000) {
  const bar = $("#voiceBar");
  $("#voiceText").textContent = text;
  bar.classList.remove("hidden");
  clearTimeout(showNote._t);
  if (ms) showNote._t = setTimeout(() => bar.classList.add("hidden"), ms);
}

async function pollRecording() {
  if (analysing) return;
  let s = null;
  try { s = (await api.get("/api/sessions/active")).session; } catch (e) { return; }
  const pill = $("#recPill");
  if (s) {
    currentSession = s.id;
    const live = (s.label || "").includes("live");
    pill.className = "pill rec";
    pill.textContent = `● ${live ? "LIVE" : "Recording"} · ${s.event_count} events`;
    $("#btnStart").disabled = true; $("#btnStop").disabled = false;
    $("#btnStop").textContent = live ? "■ Stop live & analyse" : "■ Stop & analyse";
    wasRecording = true;
    // live mode re-detects tasks every ~2 min: refresh the task list when it changes
    if (live && ++pollRecording.n % 5 === 0) {
      const d = await api.get("/api/dashboard");
      const key = JSON.stringify(d.tasks.map((t) => t.id));
      if (key !== pollRecording.key) { pollRecording.key = key; await loadDashboard(); }
    }
  } else {
    pill.className = "pill idle"; pill.textContent = "● Idle";
    $("#btnStart").disabled = false; $("#btnStop").disabled = true;
    if (wasRecording) {
      // stopped from the extension (button or timer): it runs the analysis itself
      wasRecording = false;
      waitForExtensionAnalysis();
    }
  }
}

async function waitForExtensionAnalysis() {
  const pill = $("#recPill");
  const before = JSON.stringify((await api.get("/api/dashboard")).tasks.map((t) => t.id));
  pill.textContent = "● Analysing…";
  for (let i = 0; i < 20; i++) {          // up to ~60 s
    await new Promise((r) => setTimeout(r, 3000));
    const d = await loadDashboard();
    if (JSON.stringify(d.tasks.map((t) => t.id)) !== before) break;
  }
  pill.textContent = "● Idle";
}

async function startSession() {
  // Live recording of the whole computer (server-side desktop recorder) + the browser
  // (the extension adopts the same session, if installed).
  const s = await api.post("/api/sessions/start", { label: "computer (live)", duration_min: 0 });
  window.postMessage({ source: "autopilot-dashboard", type: "recAdopt", sessionId: s.id }, "*");
  const ext = document.documentElement.getAttribute("data-autopilot-extension") === "1";
  showNote("🔴 Recording your computer live: apps, windows, clicks and typing bursts (never what you type)" +
           (ext ? " + browser detail via the extension." : ". Install the extension for browser detail.") +
           " Repeat your task 2-3 times, then press ■ Stop here or on the red panel.", 12000);
  pollRecording();
}

async function stopAndAnalyze() {
  const pill = $("#recPill");
  analysing = true;
  pill.className = "pill idle"; pill.textContent = "● Analysing with Mistral…";
  $("#btnStart").disabled = true; $("#btnStop").disabled = true;
  try {
    const r = await fetch("/api/sessions/analyze-latest", { method: "POST" });
    const body = await r.json();
    if (!r.ok) showNote("⚠ " + (body.detail || "analysis failed"));
    else showNote(`✅ Analysed ${body.event_count} recorded events: ` +
                  `${(body.analysis.workflows || []).length} repetitive task(s) found.`);
  } finally {
    analysing = false;
    wasRecording = false;
    await loadDashboard();
    await pollRecording();
  }
}

async function loadSample() {
  $("#taskList").innerHTML = '<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>';
  await api.post("/api/sessions/load-sample?analyze=true", {});
  await loadDashboard();
}

/* ---------------- agent runs ---------------- */

async function runAgent(taskId, mode) {
  const panel = $("#runPanel");
  panel.classList.remove("hidden");
  $("#runFeed").innerHTML = "";
  $("#runSummary").textContent = "";
  $("#runMode").textContent = `· mode: ${mode}`;
  let run_id;
  if (mode === "browser") {
    run_id = await startBrowserAgent(taskId);
    if (!run_id) return;
  } else {
    ({ run_id } = await api.post(`/api/agents/${taskId}/run`, { mode, demo_mode: true }));
  }
  panel.scrollIntoView({ behavior: "smooth" });
  const es = new EventSource(`/api/agents/runs/${run_id}/events`);
  es.onmessage = (msg) => {
    const ev = JSON.parse(msg.data);
    if (ev.kind === "stream_end") { es.close(); afterRun(run_id); return; }
    addFeed(ev);
  };
  es.onerror = () => { es.close(); afterRun(run_id); };
}

// The browser agent runs inside the Chrome extension (it acts in the user's real
// Gmail tab). This page asks the extension to start it via window.postMessage;
// the extension's content script on this page relays it.
async function startBrowserAgent(taskId) {
  if (document.documentElement.getAttribute("data-autopilot-extension") !== "1") {
    showNote("⚠ The AutoPilot Chrome extension isn't active on this page. Load/reload it in " +
             "chrome://extensions, then refresh this page.", 12000);
    $("#runPanel").classList.add("hidden");
    return null;
  }
  const r = await fetch("/api/browser-agent/start", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ task_id: taskId }) });
  const start = await r.json();
  if (!r.ok) { showNote("⚠ " + (start.detail || "could not start the agent")); return null; }
  const reply = new Promise((resolve) => {
    const onMsg = (e) => {
      if (e.source !== window || !e.data || e.data.source !== "autopilot-extension") return;
      window.removeEventListener("message", onMsg);
      resolve(e.data);
    };
    window.addEventListener("message", onMsg);
    setTimeout(() => resolve({ ok: false, error: "no answer from the extension" }), 5000);
  });
  window.postMessage({ source: "autopilot-dashboard", type: "agentStart", runId: start.run_id,
                       query: start.query, maxEmails: start.max_emails }, "*");
  const ack = await reply;
  if (!ack.ok) {
    showNote("⚠ Extension: " + (ack.error || "could not start"), 10000);
    await fetch(`/api/browser-agent/${start.run_id}/finish`, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ aborted: true, error: ack.error }) });
  } else {
    showNote("🖥 The agent is opening Gmail in a new tab - watch it work there. " +
             "It types replies but never sends them. Stop it anytime with ■ on the side panel.", 12000);
  }
  return start.run_id;
}

/* ---------------- laptop agent ---------------- */

let laptopRunId = null;

async function runLaptopAgent() {
  const goal = $("#laptopGoal").value.trim();
  if (!goal) { $("#laptopGoal").focus(); return; }
  const panel = $("#runPanel");
  const r = await fetch("/api/desktop-agent/run", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ goal }) });
  const body = await r.json();
  if (!r.ok) { showNote("⚠ " + (body.detail || "could not start the laptop agent")); return; }
  laptopRunId = body.run_id;
  panel.classList.remove("hidden");
  $("#runFeed").innerHTML = "";
  $("#runSummary").textContent = "";
  $("#runMode").innerHTML = `· laptop agent <button class="btn small ghost" id="btnLaptopStop">■ Stop</button>`;
  $("#btnLaptopStop").onclick = () => api.post(`/api/desktop-agent/${laptopRunId}/stop`);
  showNote("💻 The laptop agent is taking control - hands off the mouse. Press Esc to stop it.", 8000);
  const es = new EventSource(`/api/agents/runs/${body.run_id}/events`);
  es.onmessage = async (msg) => {
    const ev = JSON.parse(msg.data);
    if (ev.kind === "stream_end") {
      es.close();
      const run = await api.get(`/api/agents/runs/${laptopRunId}`);
      const rep = run.report || {};
      $("#runSummary").textContent = `${rep.status === "done" ? "✅" : rep.status === "stopped" ? "■" : "⚠"} ` +
        `${rep.summary || rep.status || "finished"} · ${rep.steps ?? "?"} steps · ${rep.agent_seconds ?? "?"}s`;
      return;
    }
    addFeed(ev);
  };
}

function answerQuestion(runId, approve, box) {
  box.querySelectorAll("button").forEach((b) => (b.disabled = true));
  box.append(approve ? " → approved" : " → denied");
  api.post(`/api/desktop-agent/${runId}/answer`, { approve });
}

function addFeed(ev) {
  const feed = $("#runFeed");
  const div = document.createElement("div");
  div.className = "ev";
  if (ev.kind === "question") {
    div.className = "ev ask";
    div.innerHTML = `<span class="k question">ASKS YOU</span>${esc(ev.message)}<br>
      <button class="yes">✔ Approve</button><button class="no">✖ Deny</button>`;
    const runId = ev.run_id;
    div.querySelector(".yes").onclick = () => answerQuestion(runId, true, div);
    div.querySelector(".no").onclick = () => answerQuestion(runId, false, div);
    feed.appendChild(div);
    feed.scrollTop = feed.scrollHeight;
    return;
  }
  div.innerHTML = `<span class="k ${ev.kind}">${ev.kind.toUpperCase()}</span>${esc(ev.message)}`;
  feed.appendChild(div);
  feed.scrollTop = feed.scrollHeight;
}

async function afterRun(runId) {
  const run = await api.get(`/api/agents/runs/${runId}`);
  const r = run.report || {};
  if (r.weekly_hours_saved !== undefined) {
    $("#runSummary").textContent =
      `✅ ${r.rejections ?? "?"} rejections handled · ${r.drafts_created ?? 0} drafts · ` +
      `${r.rows_updated ?? 0} tracker rows updated · ${(r.ghosted || []).length} ghosted\n` +
      `⏱ agent took ${r.agent_seconds}s vs ~${r.manual_minutes_equivalent} min manual\n` +
      `💰 ~${r.weekly_hours_saved} h/week saved ≈ £${r.monthly_saving_gbp}/month`;
  } else if (r.error) {
    $("#runSummary").textContent = "❌ " + r.error;
  }
  await loadDrafts();
  await loadDashboard();
}

async function loadDrafts() {
  const { drafts } = await api.get("/api/drafts");
  const el = $("#draftList");
  el.innerHTML = drafts.length ? "" : '<p class="muted">No drafts yet.</p>';
  drafts.forEach((d) => el.appendChild(draftCard(d)));
}

function draftCard(d) {
  const el = document.createElement("div");
  el.className = "draft";
  el.innerHTML = `
    <div class="to">${esc(d.to_addr)} <span class="badge status">${esc(d.status)}</span></div>
    <div class="subj">${esc(d.subject)}</div>
    <div class="body">${esc(d.body)}</div>
    <div class="row">
      <button class="btn small primary" data-a="approve">Approve</button>
      <button class="btn small" data-a="edit">Edit</button>
      <button class="btn small ghost" data-a="skip">Skip</button>
    </div>
    <textarea class="hidden" rows="4">${esc(d.body)}</textarea>`;
  const ta = el.querySelector("textarea");
  el.querySelector('[data-a="approve"]').onclick = async () => { await api.post(`/api/drafts/${d.id}/approve`); loadDrafts(); };
  el.querySelector('[data-a="skip"]').onclick = async () => { await api.post(`/api/drafts/${d.id}/skip`); loadDrafts(); };
  el.querySelector('[data-a="edit"]').onclick = async () => {
    ta.classList.toggle("hidden");
    if (!ta.classList.contains("hidden")) {
      el.querySelector('[data-a="edit"]').textContent = "Save";
      el.querySelector('[data-a="edit"]').onclick = async () => {
        await api.post(`/api/drafts/${d.id}/edit`, { body: ta.value }); loadDrafts();
      };
    }
  };
  return el;
}

async function exportSkill(taskId, card) {
  const r = await api.post(`/api/agents/${taskId}/export-vibe-skill`, {});
  alert(`Exported to:\n${r.folder}\n\nFiles: ${r.files.join(", ")}\nSchedule: ${r.schedule.cron}`);
  loadDashboard();
}

async function scheduleTask(taskId) {
  const r = await api.post(`/api/agents/${taskId}/schedule`, {});
  alert(`Scheduled: ${r.cron} — ${r.note}`);
}

async function dismissTask(taskId) {
  await api.post(`/api/tasks/${taskId}/dismiss`, {});
  loadDashboard();
}

/* ---------------- voice (Voxtral) ---------------- */

let mediaRecorder = null;
let audioChunks = [];

async function toggleMic() {
  const bar = $("#voiceBar");
  if (mediaRecorder && mediaRecorder.state === "recording") {
    mediaRecorder.stop();
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    audioChunks = [];
    mediaRecorder = new MediaRecorder(stream);
    mediaRecorder.ondataavailable = (e) => audioChunks.push(e.data);
    mediaRecorder.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      bar.classList.remove("hidden");
      $("#voiceText").textContent = "Transcribing (Voxtral)…";
      const blob = new Blob(audioChunks, { type: "audio/webm" });
      const fd = new FormData();
      fd.append("file", blob, "audio.webm");
      const r = await fetch("/api/voice", { method: "POST", body: fd }).then((x) => x.json());
      $("#voiceText").textContent = r.transcript
        ? `🗣 “${r.transcript}” → ${r.intent?.reply || r.intent?.intent || ""}`
        : (r.error || "voice unavailable");
      if (r.session_id || r.run_id) setTimeout(loadDashboard, 800);
      setTimeout(() => bar.classList.add("hidden"), 6000);
    };
    mediaRecorder.start();
    bar.classList.remove("hidden");
    $("#voiceText").textContent = "Listening… press 🎙️ again to stop";
  } catch (e) {
    alert("Microphone unavailable: " + e.message);
  }
}

/* ---------------- utils ---------------- */

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------------- wire up ---------------- */

$("#btnStart").onclick = startSession;
$("#btnStop").onclick = stopAndAnalyze;
$("#btnSample").onclick = loadSample;
$("#btnMic").onclick = toggleMic;
$("#btnLaptop").onclick = runLaptopAgent;
api.get("/api/health").then((h) => {
  document.querySelectorAll(".chip-btn").forEach((b) => {
    b.onclick = () => { $("#laptopGoal").value = b.dataset.goal.replace("{TRACKER}", h.tracker || "applications.xlsx"); };
  });
});
loadDashboard();
pollRecording.n = 0;
pollRecording.key = null;
pollRecording();
setInterval(pollRecording, 2000);
setInterval(() => { if (!$("#runPanel").classList.contains("hidden")) loadDrafts(); }, 5000);
