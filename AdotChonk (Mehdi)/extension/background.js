// AutoPilot Observer - background service worker.
// Owns the recording state, creates sessions on the backend, batches events
// from content scripts and POSTs them to /api/events. Shows a REC/LIVE badge.
// Timed mode: stops after N minutes, then analyses.
// Live mode: records until the user stops it, re-analysing every few minutes
// so the dashboard updates while they work.

const API = "http://localhost:8000";
const LIVE_ANALYSE_EVERY_MIN = 2;   // live mode: re-detect tasks this often...
const LIVE_MIN_NEW_EVENTS = 5;      // ...when at least this many new events arrived

let state = { recording: false, live: false, sessionId: null, startedAt: null, endsAt: null,
              queue: [], sent: 0, lastAnalysedSent: 0, lastError: null, analysing: false,
              lastAnalysis: null };
let lastFlush = 0;
const PERSIST = ["recording", "live", "sessionId", "startedAt", "endsAt", "sent", "lastAnalysedSent"];

async function api(path, body) {
  const res = await fetch(API + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(`${path} -> HTTP ${res.status}: ${JSON.stringify(data.detail || data).slice(0, 200)}`);
  return data;
}

function persist() {
  const o = {};
  PERSIST.forEach((k) => { o[k] = state[k]; });
  return chrome.storage.local.set(o);
}

async function setBadge() {
  if (state.recording) {
    await chrome.action.setBadgeText({ text: state.live ? "LIVE" : "REC" });
    await chrome.action.setBadgeBackgroundColor({ color: "#dc2626" });
  } else if (state.analysing) {
    await chrome.action.setBadgeText({ text: "…" });
    await chrome.action.setBadgeBackgroundColor({ color: "#4f46e5" });
  } else {
    await chrome.action.setBadgeText({ text: "" });
  }
}

function publicState() {
  return { recording: state.recording, live: state.live, sessionId: state.sessionId,
           startedAt: state.startedAt, endsAt: state.endsAt, sent: state.sent,
           queued: state.queue.length, lastError: state.lastError, analysing: state.analysing,
           lastAnalysis: state.lastAnalysis, agent: agent.running ? { ...agent } : null, now: Date.now() };
}

// ===========================================================================
// Browser agent: does the rejection-email routine in the user's own Gmail tab.
// Mistral (via the backend) picks the emails and writes the replies; this code
// clicks and types. It NEVER clicks Send - every reply is left as a draft.
// ===========================================================================

let agent = { running: false, runId: null, startedAt: null, step: "", i: 0, n: 0, abort: false, tabId: null };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function agentStep(step, extra = {}) {
  Object.assign(agent, { step }, extra);
  broadcast();
}

async function agentEvent(kind, message, data) {
  try { await api(`/api/browser-agent/${agent.runId}/event`, { kind, message, data: data || {} }); } catch (e) {}
}

async function waitTabComplete(tabId, timeoutMs = 30000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    const t = await chrome.tabs.get(tabId);
    if (t.status === "complete") return;
    await sleep(300);
  }
}

// run one command in the Gmail tab's content script (injecting it if needed)
async function tabCmd(tabId, cmd, args = {}, timeoutMs = 60000) {
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const res = await Promise.race([
        chrome.tabs.sendMessage(tabId, { type: "agentCmd", cmd, args }),
        sleep(timeoutMs).then(() => ({ ok: false, error: `${cmd} timed out` })),
      ]);
      if (res === undefined) throw new Error("no content script");
      return res;
    } catch (e) {
      await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] }).catch(() => {});
      await sleep(800);
    }
  }
  return { ok: false, error: `could not reach the Gmail tab for "${cmd}"` };
}

async function runBrowserAgent({ runId, query, maxEmails }) {
  if (agent.running) throw new Error("the agent is already running");
  agent = { running: true, runId, startedAt: Date.now(), step: "Opening Gmail…", i: 0, n: 0, abort: false, tabId: null };
  broadcast();
  let error = null;
  try {
    const searchHash = "#search/" + encodeURIComponent(query);
    const tab = await chrome.tabs.create({ url: "https://mail.google.com/mail/u/0/" + searchHash, active: true });
    agent.tabId = tab.id;
    await waitTabComplete(tab.id);
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] }).catch(() => {});
    await agentEvent("info", "Opened Gmail and searched for rejection emails");

    agentStep("Reading search results…");
    const list = await tabCmd(tab.id, "listResults", { max: maxEmails || 8 });
    if (!list.ok) throw new Error(list.error || "could not read Gmail results");
    const emails = list.emails || [];
    await agentEvent("info", `Found ${emails.length} matching email(s) in Gmail`, { count: emails.length });
    if (!emails.length) return;
    if (agent.abort) return;

    agentStep(`Mistral is reading ${emails.length} email(s)…`);
    const { results } = await api(`/api/browser-agent/${runId}/classify`, { emails });
    const todo = results.filter((r) => r.is_rejection && r.draft_body);
    const byId = Object.fromEntries(emails.map((e) => [e.id, e]));
    agent.n = todo.length;

    for (let k = 0; k < todo.length && !agent.abort; k++) {
      const r = todo[k], email = byId[r.id];
      agent.i = k + 1;
      const who = r.company || email.from_name || "email";
      agentStep(`Opening email ${k + 1}/${todo.length} · ${who}`);
      let res = await tabCmd(tab.id, "openThread", { threadId: r.id, searchHash });
      if (!res.ok) { await agentEvent("warning", `Could not open the ${who} email: ${res.error}`); continue; }
      if (agent.abort) break;

      agentStep(`Replying to ${who}…`);
      res = await tabCmd(tab.id, "reply");
      if (!res.ok) {
        await agentEvent(res.skip ? "info" : "warning", res.skip ? `Skipped ${who}: ${res.error}`
                                                                 : `Could not open Reply for ${who}: ${res.error}`);
        await tabCmd(tab.id, "backToResults", { searchHash });
        continue;
      }
      if (agent.abort) break;

      agentStep(`Typing feedback request to ${who}…`);
      res = await tabCmd(tab.id, "typeText", { text: r.draft_body }, 120000);
      if (!res.ok) { await agentEvent("warning", `Could not type the reply for ${who}: ${res.error}`); continue; }

      await sleep(1800); // let Gmail autosave the draft
      await api(`/api/browser-agent/${runId}/drafted`, {
        email_id: r.id, to: email.from_addr, subject: r.draft_subject || ("Re: " + email.subject),
        body: r.draft_body, company: r.company || "", role: r.role || "",
      });
      agentStep(`Draft saved for ${who} - not sent`);
      await tabCmd(tab.id, "backToResults", { searchHash });
      await sleep(600);
    }
  } catch (e) {
    error = String(e.message || e);
  } finally {
    const aborted = agent.abort;
    agentStep(aborted ? "Stopped" : "Done - review your drafts in Gmail");
    try { await api(`/api/browser-agent/${runId}/finish`, { aborted, error }); } catch (e) {}
    await sleep(1500);
    agent = { running: false, runId: null, startedAt: null, step: "", i: 0, n: 0, abort: false, tabId: null };
    broadcast();
  }
}

// Tell every tab's content script (instant overlay show/hide)
async function broadcast() {
  const tabs = await chrome.tabs.query({});
  const msg = { type: "state", state: publicState() };
  for (const t of tabs) chrome.tabs.sendMessage(t.id, msg).catch(() => {});
}

// Tabs opened before the extension was (re)loaded have no content script yet
async function injectIntoOpenTabs() {
  const tabs = await chrome.tabs.query({ url: ["http://*/*", "https://*/*"] });
  for (const t of tabs) {
    chrome.scripting.executeScript({ target: { tabId: t.id }, files: ["content.js"] }).catch(() => {});
  }
}

// MV3 service workers are killed when idle; restore state before handling anything.
const ready = new Promise((resolve) => {
  chrome.storage.local.get(PERSIST, async (s) => {
    if (s.recording && s.sessionId) {
      PERSIST.forEach((k) => { if (s[k] !== undefined) state[k] = s[k]; });
      await setBadge();
    }
    resolve();
  });
});

async function startRecording(durationMin, live) {
  const s = await api("/api/sessions/start", {
    label: live ? "chrome extension (live)" : "chrome extension",
    duration_min: live ? 0 : durationMin,
  });
  Object.assign(state, {
    recording: true, live: !!live, sessionId: s.id, startedAt: Date.now(),
    endsAt: live ? null : Date.now() + durationMin * 60 * 1000,
    queue: [], sent: 0, lastAnalysedSent: 0, lastError: null, lastAnalysis: null,
  });
  await persist();
  await setBadge();
  if (live) chrome.alarms.create("autopilot-live", { periodInMinutes: LIVE_ANALYSE_EVERY_MIN });
  else chrome.alarms.create("autopilot-stop", { when: state.endsAt });
  await injectIntoOpenTabs();
  broadcast();
  return s;
}

// The dashboard started a (live) session itself: record the browser into it too.
async function adoptSession(sessionId) {
  if (state.recording && state.sessionId === sessionId) return { ok: true };
  if (state.recording) await stopRecording();
  Object.assign(state, {
    recording: true, live: true, sessionId, startedAt: Date.now(), endsAt: null,
    queue: [], sent: 0, lastAnalysedSent: 0, lastError: null, lastAnalysis: null,
  });
  await persist();
  await setBadge();
  chrome.alarms.create("autopilot-live", { periodInMinutes: LIVE_ANALYSE_EVERY_MIN });
  await injectIntoOpenTabs();
  broadcast();
  return { ok: true };
}

async function stopRecording() {
  if (!state.recording) return null;
  state.recording = false;
  await flush(); // send remaining events
  const sid = state.sessionId;
  let out = null;
  if (sid) {
    out = await api(`/api/sessions/${sid}/stop`, {}).catch((e) => ({ error: String(e) }));
  }
  chrome.alarms.clear("autopilot-stop");
  chrome.alarms.clear("autopilot-live");
  state.sessionId = null;
  state.live = false;
  await persist();
  await setBadge();
  broadcast();
  // final analysis in the background (popup shows progress) - unless the dashboard already did
  if (sid && state.sent > 0 && out && !out.error && !out.already_stopped) analyse(sid);
  return { stopped: out, sessionId: sid, sent: state.sent };
}

async function analyse(sid) {
  if (state.analysing) return;
  state.analysing = true;
  const sentAtStart = state.sent;
  await setBadge();
  try {
    const r = await api(`/api/sessions/${sid}/analyze`, {});
    state.lastAnalysis = { workflows: (r.workflows || []).length, at: Date.now() };
    state.lastAnalysedSent = sentAtStart;
    state.lastError = null;
    persist();
  } catch (e) {
    state.lastError = String(e);
  } finally {
    state.analysing = false;
    await setBadge();
    broadcast();
  }
}

function stopLocally() {
  // session was stopped (and analysed) from the dashboard: no re-analysis
  state.recording = false;
  state.live = false;
  state.sessionId = null;
  state.queue = [];
  chrome.alarms.clear("autopilot-stop");
  chrome.alarms.clear("autopilot-live");
  persist();
  setBadge();
  broadcast();
}

async function flush() {
  if (!state.queue.length || !state.sessionId) return;
  lastFlush = Date.now();
  const sid = state.sessionId;
  const events = state.queue.splice(0, state.queue.length).map((e) => ({ ...e, session_id: sid }));
  try {
    const r = await api("/api/events", { session_id: sid, events });
    state.sent += r.received || 0;
    state.lastError = null;
    chrome.storage.local.set({ sent: state.sent });
  } catch (e) {
    if (String(e).includes("HTTP 409")) return stopLocally();
    state.lastError = String(e);
    state.queue.unshift(...events); // retry later
  }
}

// flush every 4s while the worker is alive; also flushed on incoming events
// (setInterval dies with the worker, so we never rely on it alone)
setInterval(() => { if (state.recording) flush(); }, 4000);

chrome.alarms.onAlarm.addListener(async (alarm) => {
  await ready;
  if (alarm.name === "autopilot-stop") await stopRecording();
  // live re-detection now runs on the server (it sees desktop + browser events);
  // the extension just makes sure its events are delivered
  if (alarm.name === "autopilot-live" && state.recording && state.live && state.sessionId) await flush();
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  ready.then(async () => {
    try {
      if (msg.type === "events") {
        if (state.recording) {
          state.queue.push(...msg.events);
          if (state.queue.length > 40 || Date.now() - lastFlush > 4000) await flush();
        }
        sendResponse({ ok: true, queued: state.queue.length });
      } else if (msg.type === "getState") {
        sendResponse(publicState());
      } else if (msg.type === "start") {
        sendResponse(await startRecording(msg.durationMin, msg.live));
      } else if (msg.type === "stop") {
        sendResponse(await stopRecording());
      } else if (msg.type === "adopt") {
        sendResponse(await adoptSession(msg.sessionId));
      } else if (msg.type === "agentStart") {
        if (agent.running) sendResponse({ error: "the agent is already running" });
        else { runBrowserAgent(msg); sendResponse({ ok: true }); }   // runs in the background
      } else if (msg.type === "agentStop") {
        agent.abort = true;
        agentStep("Stopping…");
        sendResponse({ ok: true });
      } else {
        sendResponse(null);
      }
    } catch (e) {
      sendResponse({ error: String(e) });
    }
  });
  return true; // async response
});
