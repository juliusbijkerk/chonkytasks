// AutoPilot Observer - content script.
// PRIVACY BY DESIGN: never captures typed values, passwords, or page text.
// Logs only: navigations (url+title), clicks (element tag + visible label, 60 chars),
// input events (field label/type ONLY, one per editing burst), tab switches, copy/paste
// (no content), idle. Click labels come only from buttons/links (aria-label or short text).

(() => {
  // injected both by the manifest and programmatically on start: run once per page
  if (window.__autopilotObserver) return;
  window.__autopilotObserver = true;

  let recording = false;
  let buffer = [];
  let lastUrl = location.href;
  let lastActivity = Date.now();
  let idleSent = false;

  const appFromDomain = (url) => {
    try {
      const h = new URL(url).hostname;
      if (h.includes("mail.google.com")) return "Gmail";
      if (h.includes("docs.google.com")) return h.includes("spreadsheets") ? "Sheets" : "Docs";
      if (h.includes("drive.google.com")) return "Drive";
      const u = url.toLowerCase();
      if (h.includes("excel.officeapps.live.com") || h.includes("excel.cloud.microsoft") ||
          ((h.includes("onedrive.live.com") || h.includes("sharepoint.com") || h.includes("1drv.ms")) &&
           (u.includes(".xlsx") || u.includes("xlsx") || u.includes("excel") || u.includes("doc.aspx"))))
        return "Excel Online";
      if (h.includes("linkedin.com")) return "LinkedIn";
      return "Other";
    } catch { return "Other"; }
  };

  const mask = (s) =>
    String(s || "")
      .replace(/[\w.+-]+@[\w-]+\.[\w.]+/g, "[email]")   // mask email addresses
      .replace(/\b\d[\d\s().-]{6,}\d\b/g, "[number]")   // mask phone-like numbers
      .slice(0, 60);

  const fieldLabel = (el) => {
    if (!el) return "field";
    return mask(
      el.getAttribute("aria-label") ||
      el.getAttribute("placeholder") ||
      el.name || el.id ||
      (el.labels && el.labels[0] ? el.labels[0].innerText : "") ||
      el.type || "field"
    );
  };

  const push = (type, detail) => {
    if (!recording) return;
    if (uiState && uiState.agent) return;   // the agent is acting: don't record its actions as the user's
    buffer.push({
      session_id: null, // filled by background relay
      timestamp_ms: Date.now(),
      type,
      app: appFromDomain(location.href),
      detail,
    });
    lastActivity = Date.now();
    idleSent = false;
    if (buffer.length >= 20) flush();
  };

  const flush = () => {
    if (!buffer.length) return;
    const events = buffer.splice(0, buffer.length);
    chrome.runtime.sendMessage({ type: "events", events }, () => void chrome.runtime.lastError);
  };

  // ask background whether we are recording; re-check periodically (SPA-safe)
  // ---------------- on-page recording indicator ----------------
  // Glowing, breathing frame around the viewport + a side dock with the logo,
  // a timer and a Stop button. Lives in a shadow root so page CSS can't touch it
  // (and ours can't leak). The frame ignores the mouse; only the dock is clickable.
  let host = null, ui = null, uiState = null, uiTimer = null;

  const OVERLAY_CSS = `
    :host { all: initial; }
    .frame { position: fixed; inset: 0; pointer-events: none; z-index: 2147483646;
      box-shadow: inset 0 0 0 2px rgba(239,68,68,.55), inset 0 0 26px 4px rgba(239,68,68,.30),
                  inset 0 0 90px 18px rgba(99,102,241,.22);
      animation: ap-breathe 2.6s ease-in-out infinite; }
    @keyframes ap-breathe { 0%,100% { opacity: .55 } 50% { opacity: 1 } }
    .dock { position: fixed; right: 14px; top: 50%; transform: translateY(-50%); z-index: 2147483647;
      display: flex; flex-direction: column; align-items: center; gap: 8px; padding: 10px 8px;
      width: 58px; box-sizing: border-box; border-radius: 18px;
      background: rgba(17,20,40,.86); backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
      border: 1px solid rgba(255,255,255,.12); box-shadow: 0 10px 30px rgba(0,0,0,.35);
      font: 600 10px/1.2 "Segoe UI", system-ui, -apple-system, sans-serif; color: #e8eaff;
      user-select: none; transition: opacity .2s; }
    .dock.dim { opacity: .35; }
    .dock:hover { opacity: 1; }
    .logo { position: relative; width: 34px; height: 34px; border-radius: 11px; display: grid; place-items: center;
      background: linear-gradient(140deg, #6366f1, #a855f7); font-size: 18px; }
    .dot { position: absolute; top: -3px; right: -3px; width: 10px; height: 10px; border-radius: 50%;
      background: #ef4444; border: 2px solid rgba(17,20,40,.9); animation: ap-pulse 1.4s ease-in-out infinite; }
    @keyframes ap-pulse { 0%,100% { transform: scale(1); opacity: 1 } 50% { transform: scale(.7); opacity: .6 } }
    .mode { letter-spacing: .08em; color: #fca5a5; font-size: 9.5px; }
    .time { font-variant-numeric: tabular-nums; font-size: 11px; color: #fff; }
    .stop { all: unset; cursor: pointer; width: 34px; height: 34px; border-radius: 10px; display: grid; place-items: center;
      background: rgba(239,68,68,.16); border: 1px solid rgba(239,68,68,.55); transition: background .15s; }
    .stop:hover { background: rgba(239,68,68,.35); }
    .stop:focus-visible { outline: 2px solid #fca5a5; outline-offset: 2px; }
    .stop i { width: 11px; height: 11px; border-radius: 2px; background: #f87171; display: block; }
    .hide { all: unset; cursor: pointer; font-size: 9px; color: #9aa0c8; }
    .hide:hover { color: #fff; }
    /* agent mode: indigo/violet instead of red - "AutoPilot is acting", not just watching */
    .agent .frame { box-shadow: inset 0 0 0 2px rgba(129,140,248,.7), inset 0 0 30px 6px rgba(129,140,248,.38),
                    inset 0 0 100px 22px rgba(168,85,247,.22); }
    .agent .dot { background: #818cf8; }
    .agent .mode { color: #c7d2fe; }
    .bubble { position: fixed; right: 84px; top: 50%; transform: translateY(-50%); z-index: 2147483647;
      max-width: 280px; padding: 9px 12px; border-radius: 12px; background: rgba(17,20,40,.9);
      border: 1px solid rgba(129,140,248,.5); color: #eef0ff; box-shadow: 0 10px 30px rgba(0,0,0,.3);
      font: 500 12px/1.4 "Segoe UI", system-ui, -apple-system, sans-serif; }
    .bubble b { color: #c7d2fe; font-weight: 700; display: block; font-size: 10px; letter-spacing: .06em; margin-bottom: 2px; }
    .bubble:empty, .bubble.off { display: none; }
    @media (prefers-reduced-motion: reduce) { .frame, .dot { animation: none; } }
  `;

  const fmt = (ms) => {
    const s = Math.max(0, Math.round(ms / 1000));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(sec).padStart(2, "0");
  };

  const tick = () => {
    if (!ui || !uiState) return;
    const s = uiState;
    const ag = s.agent;
    ui.wrap.classList.toggle("agent", !!ag);
    if (ag) {
      ui.mode.textContent = "AGENT";
      ui.time.textContent = fmt(Date.now() - (ag.startedAt || Date.now()));
      ui.bubble.classList.remove("off");
      ui.bubble.innerHTML = "";
      const b = document.createElement("b");
      b.textContent = ag.n ? `AUTOPILOT AGENT · ${ag.i}/${ag.n}` : "AUTOPILOT AGENT";
      ui.bubble.append(b, document.createTextNode(ag.step || "Working…"));
      ui.stop.title = "Stop the agent";
      return;
    }
    ui.bubble.classList.add("off");
    ui.stop.title = "Stop recording";
    ui.mode.textContent = s.analysing ? "CHECK" : s.live ? "LIVE" : "REC";
    ui.time.textContent = s.live ? fmt(Date.now() - (s.startedAt || Date.now()))
                                 : fmt((s.endsAt || Date.now()) - Date.now());
  };

  const showOverlay = () => {
    if (window.top !== window || host) return;   // top frame only, once
    host = document.createElement("div");
    host.id = "autopilot-recording-indicator";
    const root = host.attachShadow({ mode: "open" });
    root.innerHTML = `<style>${OVERLAY_CSS}</style><div class="wrap">
      <div class="frame"></div>
      <div class="bubble off" role="status" aria-live="polite"></div>
      <div class="dock" role="status" aria-label="AutoPilot is active in this browser">
        <div class="logo" title="AutoPilot is observing your work (never what you type)">✈️<span class="dot"></span></div>
        <div class="mode">REC</div>
        <div class="time">0:00</div>
        <button class="stop" title="Stop recording"><i></i></button>
        <button class="hide" title="Fade the panel (keeps running)">fade</button>
      </div></div>`;
    ui = { wrap: root.querySelector(".wrap"), mode: root.querySelector(".mode"), time: root.querySelector(".time"),
           dock: root.querySelector(".dock"), stop: root.querySelector(".stop"),
           hide: root.querySelector(".hide"), bubble: root.querySelector(".bubble") };
    ui.stop.addEventListener("click", () => {
      if (uiState && uiState.agent) {   // agent running: stop the agent (recording, if any, continues)
        chrome.runtime.sendMessage({ type: "agentStop" }, () => void chrome.runtime.lastError);
        return;
      }
      ui.stop.disabled = true;
      chrome.runtime.sendMessage({ type: "stop" }, () => void chrome.runtime.lastError);
      hideOverlay();
    });
    ui.hide.addEventListener("click", () => {
      ui.dock.classList.toggle("dim");
      ui.hide.textContent = ui.dock.classList.contains("dim") ? "show" : "fade";
    });
    (document.documentElement || document.body).appendChild(host);
    uiTimer = setInterval(tick, 1000);
    tick();
  };

  const hideOverlay = () => {
    clearInterval(uiTimer);
    if (host) host.remove();
    host = null; ui = null;
  };

  const applyState = (s) => {
    const was = recording;
    recording = !!(s && s.recording);
    uiState = s;
    // On Windows the server shows a whole-screen RECORDING indicator, so the in-page one is
    // only needed while the browser agent acts (or on other OSes).
    const desktopIndicator = /Windows/i.test(navigator.userAgent);
    if ((recording && !desktopIndicator) || (s && s.agent)) { showOverlay(); tick(); } else hideOverlay();
    if (was && !recording) flush();
  };

  // instant updates pushed by the background on start/stop/analysis
  chrome.runtime.onMessage.addListener((msg) => {
    if (msg && msg.type === "state") applyState(msg.state);
  });

  // ask background whether we are recording; re-check periodically (SPA-safe)
  const sync = () => {
    chrome.runtime.sendMessage({ type: "getState" }, (s) => {
      if (chrome.runtime.lastError) return;
      applyState(s);
    });
  };
  sync();
  setInterval(sync, 3000);
  setInterval(flush, 3000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) sync(); });

  // never record clicks/typing on our own indicator, nor the agent's synthetic events
  const isOurs = (e) => !e.isTrusted || (host && e.composedPath && e.composedPath().includes(host));

  // ---------------- browser agent: Gmail actions ----------------
  // Commands come from background.js. Gmail's DOM is obfuscated, so each lookup
  // tries a stable class first, then aria-label / tooltip text (English + French).
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const visible = (el) => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden";
  const $all = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const waitFor = async (fn, timeoutMs = 15000, every = 250) => {
    const t0 = Date.now();
    while (Date.now() - t0 < timeoutMs) {
      const v = fn();
      if (v) return v;
      await sleep(every);
    }
    return null;
  };
  const realClick = (el) => {
    el.scrollIntoView({ block: "center" });
    const r = el.getBoundingClientRect();
    const o = { bubbles: true, cancelable: true, view: window, clientX: r.x + r.width / 2, clientY: r.y + r.height / 2 };
    ["pointerdown", "mousedown", "pointerup", "mouseup", "click"].forEach((t) =>
      el.dispatchEvent(new (t.startsWith("pointer") ? PointerEvent : MouseEvent)(t, o)));
  };
  const resultRows = () => $all("tr.zA").filter(visible);
  const threadIdOf = (row) => {
    const el = row.querySelector("[data-legacy-thread-id]") || row.querySelector("[data-thread-id]");
    return el ? (el.getAttribute("data-legacy-thread-id") || el.getAttribute("data-thread-id")) : null;
  };
  const NO_RESULTS = /no messages matched|aucun message ne correspond|aucun résultat/i;

  const agentCmds = {
    async listResults({ max = 8 }) {
      const rows = await waitFor(() => {
        const r = resultRows();
        if (r.length) return r;
        const main = document.querySelector('[role="main"]');
        return main && NO_RESULTS.test(main.innerText) ? [] : null;
      }, 20000);
      if (rows === null) return { ok: false, error: "Gmail results did not load (are you signed in?)" };
      const emails = rows.slice(0, max).map((row) => {
        const sender = row.querySelector("span[email]");
        return {
          id: threadIdOf(row) || "",
          from_name: sender ? (sender.getAttribute("name") || sender.innerText || "") : "",
          from_addr: sender ? sender.getAttribute("email") || "" : "",
          subject: (row.querySelector("span.bog") || {}).innerText || "",
          snippet: ((row.querySelector("span.y2") || {}).innerText || "").replace(/^\s*-\s*/, ""),
        };
      }).filter((e) => e.id);
      return { ok: true, emails };
    },

    async openThread({ threadId, searchHash }) {
      if (!resultRows().length && searchHash) { location.hash = searchHash; }
      const row = await waitFor(() => resultRows().find((r) => threadIdOf(r) === threadId), 15000);
      if (!row) return { ok: false, error: "email not found in the result list" };
      realClick(row.querySelector("span.bog") || row);
      const header = await waitFor(() => $all("h2.hP").find(visible), 15000);
      return header ? { ok: true } : { ok: false, error: "email did not open" };
    },

    async reply() {
      // the thread may already contain a reply draft (Gmail shows its editor instead of Reply)
      await sleep(800);
      if ($all('div[contenteditable="true"][role="textbox"]').some(visible))
        return { ok: false, skip: true, error: "this email already has a reply draft - left untouched" };
      const btn = await waitFor(() =>
        $all("span.ams.bkH").find(visible) ||                       // "Reply" chip under the email
        $all('[role="button"][aria-label], [role="button"][data-tooltip]').filter(visible).find((b) =>
          /^(reply|répondre)$/i.test((b.getAttribute("aria-label") || b.getAttribute("data-tooltip") || "").trim())),
        10000);
      if (!btn) return { ok: false, error: "Reply button not found" };
      realClick(btn);
      const editor = await waitFor(() => $all('div[contenteditable="true"][role="textbox"]').filter(visible).pop(), 10000);
      return editor ? { ok: true } : { ok: false, error: "reply editor did not open" };
    },

    async typeText({ text }) {
      const editor = $all('div[contenteditable="true"][role="textbox"]').filter(visible).pop();
      if (!editor) return { ok: false, error: "reply editor not found" };
      editor.focus();
      const sel = getSelection();
      sel.selectAllChildren(editor);
      sel.collapseToStart();
      // type word by word so the user sees it happen; never sends
      const lines = String(text || "").replace(/\r/g, "").split("\n");
      for (let li = 0; li < lines.length; li++) {
        const words = lines[li].split(/(\s+)/);
        for (const w of words) {
          if (!w) continue;
          document.execCommand("insertText", false, w);
          await sleep(w.trim() ? 35 : 5);
        }
        if (li < lines.length - 1) document.execCommand("insertLineBreak");
      }
      editor.dispatchEvent(new Event("input", { bubbles: true }));
      return { ok: true };
    },

    async backToResults({ searchHash }) {
      // leaving the thread makes Gmail save the open reply as a draft
      location.hash = searchHash;
      await waitFor(() => resultRows().length, 10000);
      return { ok: true };
    },
  };

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (!msg || msg.type !== "agentCmd" || window.top !== window) return false;
    const fn = agentCmds[msg.cmd];
    if (!fn) { sendResponse({ ok: false, error: "unknown command " + msg.cmd }); return false; }
    fn(msg.args || {}).then(sendResponse, (e) => sendResponse({ ok: false, error: String(e) }));
    return true;
  });

  // ---------------- dashboard -> extension bridge ----------------
  // Only the AutoPilot dashboard on localhost may start the agent.
  if (/^http:\/\/(localhost|127\.0\.0\.1):8000$/.test(location.origin)) {
    document.documentElement.setAttribute("data-autopilot-extension", "1");
    window.addEventListener("message", (e) => {
      const d = e.data;
      if (e.source !== window || !d || d.source !== "autopilot-dashboard") return;
      if (d.type === "recAdopt" && d.sessionId) {
        chrome.runtime.sendMessage({ type: "adopt", sessionId: d.sessionId }, () => void chrome.runtime.lastError);
      }
      if (d.type === "agentStart") {
        chrome.runtime.sendMessage({ type: "agentStart", runId: d.runId, query: d.query, maxEmails: d.maxEmails },
          (r) => window.postMessage({ source: "autopilot-extension", type: "agentStarted",
                                      ok: !chrome.runtime.lastError && r && r.ok,
                                      error: chrome.runtime.lastError ? chrome.runtime.lastError.message : (r && r.error) }, "*"));
      }
    });
  }

  // PRIVACY: a click label comes only from a real control (button, link, tab, menu item)
  // and preferably its aria-label. Never from editors (the text you are typing,
  // spell-check suggestions), list rows (email subjects/senders) or arbitrary text.
  const CONTROL = 'button, a[href], [role="button"], [role="link"], [role="tab"], [role="menuitem"], ' +
                  '[role="option"], [role="checkbox"], input[type="button"], input[type="submit"], summary';
  const clickLabel = (el) => {
    if (el.closest('[contenteditable=""], [contenteditable="true"], textarea, [role="textbox"]'))
      return { label: "text editor", tag: "editor" };
    if (el.closest('[role="row"], tr, [role="listitem"], li[role]'))
      return { label: "list item", tag: "row" };
    if (el.matches && el.matches("input, select")) return { label: fieldLabel(el), tag: el.tagName.toLowerCase() };
    const ctl = el.closest(CONTROL);
    if (!ctl) return { label: "", tag: el.tagName.toLowerCase() };
    let label = ctl.getAttribute("aria-label") || ctl.getAttribute("title") || "";
    if (!label) {
      const text = (ctl.innerText || "").trim();
      if (text && text.length <= 30 && !text.includes("\n")) label = text; // short button text only
    }
    return { label: mask(label), tag: ctl.tagName.toLowerCase() };
  };

  document.addEventListener("click", (e) => {
    const el = e.target;
    if (!el || !el.tagName || isOurs(e)) return;
    push("click", clickLabel(el));
  }, true);

  // ONE input event per field per editing burst (not one per keystroke).
  // NEVER log e.target.value - only the field label and type.
  let lastInputEl = null, lastInputAt = 0;
  document.addEventListener("input", (e) => {
    if (isOurs(e)) return;
    const now = Date.now();
    if (e.target === lastInputEl && now - lastInputAt < 8000) { lastInputAt = now; lastActivity = now; return; }
    lastInputEl = e.target; lastInputAt = now;
    const t = e.target;
    const isEditor = t.isContentEditable || (t.getAttribute && t.getAttribute("role") === "textbox");
    push("input", { label: isEditor ? "text editor" : fieldLabel(t),
                    type: isEditor ? "editor" : (t.type || t.tagName || "").toLowerCase() });
  }, true);

  document.addEventListener("copy", () => push("copy_paste", { action: "copy" }), true);
  document.addEventListener("paste", () => push("copy_paste", { action: "paste" }), true);

  // Navigation detail. Gmail titles are email subjects and its URLs hold search
  // queries / message ids, so for Gmail we keep only the kind of view.
  const navDetail = () => {
    if (location.hostname.includes("mail.google.com")) {
      const parts = location.hash.replace(/^#/, "").split("/");
      const view = parts[0] || "inbox";
      const opened = parts.length > (view === "search" || view === "label" ? 2 : 1);
      const kind = opened ? "open email" : view === "search" ? "search results" : view;
      return { url: `https://mail.google.com/#${view}`, title: `Gmail - ${kind}` };
    }
    return { url: location.href.split(/[?#]/)[0].slice(0, 120), title: mask(document.title) };
  };

  // SPA navigations + real navigations
  const checkNav = () => {
    if (location.href !== lastUrl) {
      push("navigation", navDetail());
      lastUrl = location.href;
    }
  };
  setInterval(checkNav, 1500);
  window.addEventListener("popstate", checkNav);

  // tab visibility as a tab_switch proxy
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") push("tab_switch", { to: navDetail().title });
  });

  // idle detection (>10s without activity)
  setInterval(() => {
    if (recording && !idleSent && Date.now() - lastActivity > 10000) {
      idleSent = true;
      push("idle", { seconds: Math.round((Date.now() - lastActivity) / 1000) });
    }
  }, 2000);

  // initial navigation event
  push("navigation", navDetail());
})();
