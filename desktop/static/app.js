const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
const stamp = (seconds) => `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;
const fmtDate = (date) => new Intl.DateTimeFormat(undefined, {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'}).format(new Date(date));
const fmtRecordingDate = (date) => new Intl.DateTimeFormat(undefined, {month: 'short', day: 'numeric'}).format(new Date(date));
const fmtSize = (bytes) => bytes >= 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;

let pollTimer = null;
let jobEvents = null;
let batchSignature = '';
let pending = false;
let activeJobId = null;
let currentView = 'home';
let jobs = [];
let historyRequest = null;
let reportObserver = null;
let busyJob = null;
let reportJob = null;
let analysisSteps = [];
let selectedStepIndex = -1;
let deletingJobId = null;
let deleting = false;
let editingJobId = null;
let savingDetails = false;
const recordingName = (job) => job.display_name || job.filename;
const pencilIcon = '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="m12.5 3.5 4 4M3 17l4.5-1 9-9a2.8 2.8 0 0 0-4-4l-9 9L3 17Z"/></svg>';
const trashIcon = '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 6h12M8 3h4l1 3M6 6l1 11h6l1-11M9 9v5M11 9v5"/></svg>';
const deleteButton = (job) => `<button class="icon-button delete-button" data-delete="${esc(job.id)}" aria-label="Delete ${esc(recordingName(job))}" title="Delete recording" ${['queued', 'processing'].includes(job.status) ? 'disabled' : ''}>${trashIcon}</button>`;
const recordingActions = (job) => `<span class="recording-actions"><button class="icon-button edit-button" data-edit="${esc(job.id)}" aria-label="Edit ${esc(recordingName(job))}" title="Rename or add notes">${pencilIcon}</button>${deleteButton(job)}</span>`;
const narrowWindow = matchMedia('(max-width: 760px)');
let desktopSidebarOpen = true;
try { desktopSidebarOpen = localStorage.getItem('sidebar-open') !== 'false'; } catch { /* optional preference */ }

function setSidebar(open, restoreFocus = false) {
  document.body.classList.toggle('sidebar-closed', !open);
  $('#sidebar').inert = !open;
  $('#sidebar-toggle').setAttribute('aria-expanded', String(open));
  $('#sidebar-toggle').setAttribute('aria-label', open ? 'Hide sidebar' : 'Show sidebar');
  $('#sidebar-shade').hidden = !open || !narrowWindow.matches;
  $('#workspace').inert = open && narrowWindow.matches;
  if (open && narrowWindow.matches) $('#sidebar-close').focus();
  if (!open && restoreFocus) $('#sidebar-toggle').focus();
}
function closeMobileSidebar() { if (narrowWindow.matches) setSidebar(false, true); }
$('#sidebar-toggle').addEventListener('click', () => {
  const open = document.body.classList.contains('sidebar-closed');
  if (!narrowWindow.matches) {
    desktopSidebarOpen = open;
    try { localStorage.setItem('sidebar-open', String(open)); } catch { /* optional preference */ }
  }
  setSidebar(open);
});
$('#sidebar-close').addEventListener('click', () => setSidebar(false, true));
$('#sidebar-shade').addEventListener('click', () => setSidebar(false, true));
narrowWindow.addEventListener('change', () => setSidebar(!narrowWindow.matches && desktopSidebarOpen));
document.addEventListener('keydown', (event) => {
  if ($('#delete-dialog').open || $('#edit-dialog').open || $('#connection-dialog').open) return;
  if (!narrowWindow.matches || document.body.classList.contains('sidebar-closed')) return;
  if (event.key === 'Escape') { event.preventDefault(); setSidebar(false, true); }
  if (event.key === 'Tab') {
    const buttons = [...$('#sidebar').querySelectorAll('button:not(:disabled)')].filter((el) => el.getClientRects().length);
    if (event.shiftKey && document.activeElement === buttons[0]) { event.preventDefault(); buttons.at(-1).focus(); }
    else if (!event.shiftKey && document.activeElement === buttons.at(-1)) { event.preventDefault(); buttons[0].focus(); }
  }
});
setSidebar(!narrowWindow.matches && desktopSidebarOpen);

function syncNavigation() {
  for (const [id, active] of [['nav-new', currentView === 'home'], ['nav-recordings', ['library', 'busy', 'report'].includes(currentView)], ['nav-automations', currentView === 'automations'], ['nav-activity', currentView === 'activity']]) {
    const button = $(`#${id}`);
    button.classList.toggle('active', active);
    if (active) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
  }
  $('#history').querySelectorAll('[data-job]').forEach((button) => {
    const active = button.dataset.job === activeJobId;
    button.classList.toggle('active', active);
    if (active) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
  });
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let message;
    try { const body = await response.json(); message = typeof body.detail === 'string' ? body.detail : 'The request could not be processed.'; }
    catch { message = 'The server could not complete this request.'; }
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}
function showError(error) { const el = $('#error'); el.textContent = error.message || String(error); el.hidden = false; }
function clearError() { $('#error').hidden = true; }
function showView(name) {
  if (currentView === 'report' && name !== 'report') $('#report-video')?.pause();
  currentView = name;
  for (const view of ['home', 'library', 'busy', 'report', 'automations', 'activity']) $(`#${view}-view`).hidden = view !== name;
  $('#report-nav').hidden = name !== 'report';
  if (name !== 'report') { reportObserver?.disconnect(); reportJob = null; }
  if (name !== 'busy') busyJob = null;
  if (name === 'home') $('#view-title').textContent = 'New recording';
  if (name === 'library') $('#view-title').textContent = 'Recordings';
  if (name === 'automations') $('#view-title').textContent = 'Automations';
  if (name === 'activity') $('#view-title').textContent = 'Activity';
  syncNavigation();
}
function home() {
  stopWatching(); activeJobId = null;
  history.replaceState(null, '', '/'); clearError(); showView('home'); refreshHistory();
  closeMobileSidebar(); window.scrollTo(0, 0);
}
$('#busy-back').addEventListener('click', home);
$('#nav-new').addEventListener('click', home);
$('#nav-recordings').addEventListener('click', () => openLibrary());
function openLibrary() {
  stopWatching(); activeJobId = null; clearError();
  history.replaceState(null, '', '/#recordings');
  showView('library'); renderLibrary(); refreshHistory(); closeMobileSidebar(); window.scrollTo(0, 0);
}
$('#recording-search').addEventListener('input', renderLibrary);
for (const id of ['history', 'recording-list']) $(`#${id}`).addEventListener('click', (event) => {
  const edit = event.target.closest('[data-edit]');
  if (edit) { requestEdit(edit.dataset.edit); return; }
  const remove = event.target.closest('[data-delete]');
  if (remove) { requestDelete(remove.dataset.delete); return; }
  const button = event.target.closest('[data-job]');
  if (button) openJob(button.dataset.job);
});
function requestDelete(id) {
  const job = jobs.find((item) => item.id === id);
  if (!job || ['queued', 'processing'].includes(job.status)) return;
  deletingJobId = id;
  $('#delete-filename').textContent = recordingName(job);
  $('#delete-error').hidden = true;
  $('#delete-dialog').showModal();
}
$('#delete-cancel').addEventListener('click', () => { if (!deleting) $('#delete-dialog').close(); });
$('#delete-dialog').addEventListener('cancel', (event) => { if (deleting) event.preventDefault(); });
$('#delete-dialog').addEventListener('close', () => { deletingJobId = null; });
$('#delete-confirm').addEventListener('click', async () => {
  if (!deletingJobId || deleting) return;
  const id = deletingJobId;
  deleting = true;
  $('#delete-confirm').disabled = true; $('#delete-cancel').disabled = true;
  try {
    await api(`/api/jobs/${id}`, {method: 'DELETE'});
    $('#delete-dialog').close();
    if (activeJobId === id) openLibrary();
    // A history request started before deletion may contain the old entry.
    if (historyRequest) await historyRequest;
    await refreshHistory();
    if (narrowWindow.matches && !document.body.classList.contains('sidebar-closed')) $('#nav-recordings').focus();
    else $('#sidebar-toggle').focus();
  } catch (error) { $('#delete-error').textContent = error.message; $('#delete-error').hidden = false; }
  finally { deleting = false; $('#delete-confirm').disabled = false; $('#delete-cancel').disabled = false; }
});
function requestEdit(id) {
  const job = [busyJob, reportJob, ...jobs].find((item) => item?.id === id);
  if (!job) return;
  editingJobId = id;
  $('#recording-name').value = recordingName(job);
  $('#recording-notes').value = job.notes || '';
  $('#original-name').textContent = `Original file: ${job.filename}`;
  $('#edit-error').hidden = true;
  $('#edit-dialog').showModal();
  $('#recording-name').focus(); $('#recording-name').select();
}
$('#busy-edit').innerHTML = pencilIcon;
$('#busy-edit').addEventListener('click', () => requestEdit(activeJobId));
$('#edit-cancel').addEventListener('click', () => { if (!savingDetails) $('#edit-dialog').close(); });
$('#edit-dialog').addEventListener('cancel', (event) => { if (savingDetails) event.preventDefault(); });
$('#edit-dialog').addEventListener('close', () => { editingJobId = null; });
$('#edit-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!editingJobId || savingDetails) return;
  const name = $('#recording-name').value.trim();
  if (!name) { $('#recording-name').focus(); return; }
  const id = editingJobId;
  savingDetails = true;
  $('#edit-save').disabled = true; $('#edit-cancel').disabled = true;
  try {
    const job = await api(`/api/jobs/${id}`, {method: 'PATCH', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({display_name: name, notes: $('#recording-notes').value})});
    if (activeJobId === id) {
      if (currentView === 'busy') renderBusy(job);
      if (currentView === 'report') {
        reportJob = {...reportJob, display_name: job.display_name, notes: job.notes};
        $('#view-title').textContent = recordingName(job);
        $('#recording-meta').textContent = `${recordingName(job)} · ${stamp(job.metadata.duration)}`;
        $('#report-recording-actions').innerHTML = recordingActions(job);
        renderNotes(job);
      }
    }
    $('#edit-dialog').close();
    if (historyRequest) await historyRequest;
    await refreshHistory();
  } catch (error) { $('#edit-error').textContent = error.message; $('#edit-error').hidden = false; }
  finally { savingDetails = false; $('#edit-save').disabled = false; $('#edit-cancel').disabled = false; }
});
function renderNotes(job) {
  const notes = $('#saved-notes');
  notes.hidden = !job.notes;
  notes.innerHTML = `<strong>Notes</strong>${esc(job.notes || '')}`;
}
$('#report-nav').addEventListener('click', (event) => {
  const button = event.target.closest('[data-section]');
  if (!button) return;
  closeMobileSidebar();
  document.getElementById(button.dataset.section)?.scrollIntoView({behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'start'});
});

/* Existing native file imports still use the same analysis pipeline. */
window.addEventListener('dragover', (event) => event.preventDefault());
window.addEventListener('drop', (event) => event.preventDefault());

async function submit(path, form) {
  if (pending) return null;
  pending = true; clearError();
  try { const job = await api(path, {method: 'POST', body: form}); openJob(job.id); return job; }
  catch (error) { showError(error); return null; }
  finally { pending = false; }
}

/* Bridge used by the native macOS app: it copies a dropped file into the server
   inbox and hands over just the inbox name. */
window.aiDiscovery = window.flowlens = {
  dropped(name, filename) {
    const form = new FormData();
    form.append('name', name);
    form.append('filename', filename || '');
    form.append('context', $('#context').value);
    submit('/api/jobs/from-inbox', form);
  },
  home
};

async function refreshHistory() {
  if (historyRequest) return historyRequest;
  historyRequest = loadHistory();
  try { await historyRequest; } finally { historyRequest = null; }
}
const jobStatus = (job) => job.status === 'completed' ? 'Done' : job.status === 'failed' ? 'Needs attention' : `Analyzing · ${job.progress}%`;
async function loadHistory() {
  try {
    const fresh = await api('/api/jobs');
    if (JSON.stringify(fresh) !== JSON.stringify(jobs)) {
      jobs = fresh;
      $('#recording-count').textContent = jobs.length;
      $('#history').innerHTML = jobs.slice(0, 20).map((job) =>
        `<div class="history-row"><button class="history-item" data-job="${esc(job.id)}" title="${esc(recordingName(job))}"><strong>${esc(recordingName(job))}</strong><small>${esc(jobStatus(job))} · ${esc(fmtRecordingDate(job.created_at))}${job.notes ? ' · Note' : ''}</small></button>${recordingActions(job)}</div>`).join('') || '<p class="sidebar-empty">No recordings yet</p>';
      if (currentView === 'library') renderLibrary();
    }
    syncNavigation();
  } catch (error) { if (currentView === 'library') showError(error); }
}
function renderLibrary() {
  const query = $('#recording-search').value.trim().toLocaleLowerCase();
  const visible = jobs.filter((job) => [recordingName(job), job.filename, job.notes || ''].join(' ').toLocaleLowerCase().includes(query));
  $('#recording-list').innerHTML = visible.map((job) => `<div class="library-row"><button class="recording-row" data-job="${esc(job.id)}">
    <span class="recording-mark" aria-hidden="true">▷</span><span class="recording-name"><strong>${esc(recordingName(job))}</strong><small>${esc(fmtRecordingDate(job.created_at))}</small>${job.notes ? `<span class="notes-preview">${esc(job.notes)}</span>` : ''}</span><span class="recording-status">${esc(jobStatus(job))}</span></button>${recordingActions(job)}</div>`).join('') || `<div class="empty">${query ? 'No matching recordings.' : 'Your recordings will appear here.'}</div>`;
}

/* ---- Live analysis progress ---- */
function renderBusy(job) {
  showView('busy');
  busyJob = job;
  $('#view-title').textContent = recordingName(job);
  $('#busy-name').textContent = recordingName(job);
  $('#busy-edit').hidden = false;
  $('#bar-fill').style.width = `${job.progress}%`;
  $('#busy-msg').textContent = job.status === 'failed' ? job.message : (job.message || 'Working…');
  $('#retry-button').hidden = job.status !== 'failed';
  $('#progress-bar').classList.toggle('working', job.status !== 'failed');
  $('#progress-bar').setAttribute('aria-valuenow', job.progress);
  $('#progress-bar').setAttribute('aria-valuetext', `${job.progress}% · ${job.message || 'Working'}`);
  $('#busy-stages').hidden = false;
  const stage = job.progress < 25 ? 0 : job.progress < 76 ? 1 : job.progress < 94 ? 2 : 3;
  const detail = job.analysis_progress || {};
  const count = detail.screenshots ?? job.frames?.length ?? 0;
  const completed = detail.completed_screenshots ?? (stage > 1 ? count : 0);
  const steps = detail.identified_steps ?? job.observations?.length ?? 0;
  const descriptions = [
    count ? `${count} screenshots selected` : (stage === 0 ? job.message : ''),
    count ? `${completed} of ${count} screenshots read${detail.batch ? ` · batch ${detail.batch} of ${detail.batches}` : ''}${steps ? ` · ${steps} steps identified` : ''}` : 'Identify the steps in each screenshot',
    'Build the summary and suggestions',
    'Verify the steps and screenshot references'
  ];
  const stages = ['Prepare recording', 'Read screenshots', 'Write results', 'Check results'];
  const markup = stages.map((title, i) => `<li class="${i < stage ? 'done' : i === stage ? `current${job.status === 'failed' ? ' failed' : ''}` : ''}"><span class="process-marker" aria-hidden="true">${i < stage ? '✓' : i === stage && job.status === 'failed' ? '!' : i + 1}</span><div><strong>${title}</strong><small>${esc(descriptions[i])}</small></div></li>`).join('');
  if ($('#busy-stages').innerHTML !== markup) $('#busy-stages').innerHTML = markup;
  const request = job.model_request;
  $('#request-status').hidden = !request;
  if (request) {
    const phases = {connecting: 'Connecting to model', waiting: 'Waiting for first tokens', thinking: 'Model is analyzing', streaming: 'Receiving response', checking: 'Checking response', complete: 'Response received', retrying: 'Retrying response', failed: 'Response stopped'};
    $('#request-state').textContent = job.status === 'failed' ? 'Response stopped' : (phases[request.phase] || 'Working');
    $('#request-attempt').textContent = `Request ${request.request_number} · attempt ${request.attempt} of ${request.max_attempts}`;
    $('#live-tokens').textContent = `${request.tokens_estimated && request.output_tokens ? '≈' : ''}${Number(request.output_tokens || 0).toLocaleString()}`;
    $('#stream-updates').textContent = Number(request.chunks_received || 0).toLocaleString();
    const retryNotes = {response_limit: 'Increasing the response limit and trying again.', invalid_format: 'Requesting a complete, correctly formatted response.', interrupted: 'The response was interrupted. Trying again.', service_busy: 'The service is busy. Retrying shortly.'};
    $('#request-note').textContent = request.phase === 'retrying' ? (retryNotes[request.reason] || 'Trying again.') :
      request.tokens_estimated ? 'Token count is estimated while streaming; the provider reports the exact total at the end.' : 'Final token count reported by the provider.';
  }
  const frameIds = detail.frame_ids || [];
  $('#batch-preview').hidden = !frameIds.length || stage !== 1;
  const signature = `${job.id}:${frameIds.join(',')}`;
  if (signature !== batchSignature) {
    batchSignature = signature;
    $('#batch-caption').textContent = `This batch · ${frameIds.length} screenshots`;
    $('#batch-frames').innerHTML = (job.frames || []).filter((frame) => frameIds.includes(frame.id)).map((frame) =>
      `<figure><img src="${esc(frame.url)}" alt="Screenshot at ${stamp(frame.timestamp)}"><figcaption>${stamp(frame.timestamp)}</figcaption></figure>`).join('');
  }
  updateElapsed();
}
function updateElapsed() {
  if (!busyJob) return;
  const seconds = Math.max(0, Math.floor((Date.now() - Date.parse(busyJob.started_at || busyJob.created_at)) / 1000));
  $('#busy-stats').textContent = busyJob.status === 'failed' ? '' : `${busyJob.progress}% · ${stamp(seconds)} elapsed`;
  const request = busyJob.model_request;
  if (request) {
    const finished = busyJob.status === 'failed' || ['complete', 'failed', 'retrying'].includes(request.phase);
    const end = finished ? Date.parse(request.finished_at || request.last_event_at || request.started_at) : Date.now();
    $('#request-time').textContent = stamp(Math.max(0, Math.floor((end - Date.parse(request.started_at)) / 1000)));
  }
}
setInterval(updateElapsed, 1000);
setInterval(() => { if (!document.hidden) refreshHistory(); }, 10000);
async function openJob(id) {
  stopWatching(); clearError(); activeJobId = id; busyJob = null;
  history.replaceState(null, '', `/#job=${encodeURIComponent(id)}`);
  showView('busy');
  $('#busy-name').textContent = 'Loading…';
  $('#busy-msg').textContent = ''; $('#busy-stats').textContent = '';
  $('#retry-button').hidden = true; $('#busy-stages').hidden = true;
  $('#request-status').hidden = true; $('#batch-preview').hidden = true; $('#busy-edit').hidden = true;
  $('#bar-fill').style.width = '0%'; $('#progress-bar').classList.add('working');
  $('#progress-bar').removeAttribute('aria-valuenow');
  $('#progress-bar').setAttribute('aria-valuetext', 'Loading recording');
  $('#view-title').textContent = 'Recording';
  closeMobileSidebar(); window.scrollTo(0, 0);
  await pollJob(id);
}
async function pollJob(id) {
  if (activeJobId !== id) return;
  try {
    const job = await api(`/api/jobs/${id}`);
    if (activeJobId !== id) return;
    receiveJob(job);
    if (['queued', 'processing'].includes(job.status)) watchJob(id);
    refreshHistory();
  } catch (error) {
    if (activeJobId !== id) return;
    showError(error);
    $('#busy-msg').textContent = 'Cannot reach the local server. Reconnecting…';
    pollTimer = setTimeout(() => pollJob(id), 5000);
  }
}
function stopWatching() {
  clearTimeout(pollTimer);
  jobEvents?.close(); jobEvents = null;
}
function receiveJob(job) {
  if (activeJobId !== job.id) return;
  clearError();
  if (job.status === 'completed') { stopWatching(); renderReport(job); refreshHistory(); }
  else { renderBusy(job); if (job.status === 'failed') { stopWatching(); refreshHistory(); } }
}
function watchJob(id) {
  if (jobEvents || activeJobId !== id) return;
  const source = new EventSource(`/api/jobs/${id}/events`);
  jobEvents = source;
  source.onmessage = (event) => {
    if (jobEvents !== source || activeJobId !== id) return;
    try { receiveJob(JSON.parse(event.data)); } catch { reconnect(); }
  };
  const reconnect = () => {
    if (jobEvents !== source || activeJobId !== id) return;
    stopWatching();
    $('#request-state').textContent = 'Reconnecting to the local app…';
    pollTimer = setTimeout(() => pollJob(id), 2000);
  };
  source.onerror = reconnect;
}
$('#retry-button').addEventListener('click', async () => {
  const id = activeJobId;
  $('#retry-button').disabled = true;
  try { await api(`/api/jobs/${id}/retry`, {method: 'POST'}); await openJob(id); }
  catch (error) { showError(error); }
  finally { $('#retry-button').disabled = false; }
});

/* ---- Report ---- */
const list = (items, cls = 'clean-list') => `<ul class="${cls}">${items.map((item) => `<li>${esc(item)}</li>`).join('')}</ul>`;
const seekBtn = (seconds, label) => `<button class="seek" data-seek="${seconds}">${esc(label ?? stamp(seconds))}</button>`;

function renderRecommendation(rec, frames, index) {
  const executable = window.automationUI?.supports(rec);
  const summary = executable ? 'Checks new meeting requests in Mail and prepares reply drafts using Calendar availability. You review and send.' : rec.summary;
  return `<article class="suggestion">
    <div class="suggestion-heading"><h3>${esc(rec.title)}</h3><div class="automation-action" data-automation-action="${index}"></div></div>
    <p>${esc(summary)}</p>
    <div class="automation-state" data-automation-state="${index}"></div>
    <details class="automation-details"><summary>View details</summary>
    ${executable ? `<div class="sub">How it runs</div>${list(['Checks new messages every 30 seconds while this Mac is awake and online.', 'Identifies meeting requests with Mistral and checks your calendar for availability.', 'Proposes weekday slots, 09:00–17:00, within the next two weeks.', 'Saves a reply draft in Mail for you to review and send.'])}<div class="sub">Connected apps</div><p>Mail and Calendar on this Mac · Mistral Workflows</p>` : `<div class="sub">Automate</div>${list(rec.automated_steps)}
    ${rec.human_steps.length ? `<div class="sub">Keep manual</div>${list(rec.human_steps)}` : ''}
    ${rec.integrations.length ? `<div class="sub">Connects to</div>${rec.integrations.map((i) => `<div class="integration"><strong>${esc(i.name)}</strong><p>${esc(i.purpose)}</p><small>${esc(i.requirement)}</small></div>`).join('')}` : ''}
    <div class="sub">How to build</div>${list(rec.implementation_steps, 'num-list')}`}
    <div class="sub">Evidence</div>${rec.evidence_frame_ids.map((id) => seekBtn(frames[id].timestamp)).join(' ')}
    <div class="savings"><strong>${rec.savings.estimated_seconds_per_run === null ? 'No reliable time estimate from this recording' : `~${rec.savings.estimated_seconds_per_run}s saved per run (estimate, not measured)`}</strong>
      ${rec.savings.assumptions.length ? `<ul>${rec.savings.assumptions.map((a) => `<li>${esc(a)}</li>`).join('')}</ul>` : ''}</div>
    ${rec.assumptions.length ? `<div class="sub">Assumptions to check</div>${list(rec.assumptions)}` : ''}
    </details>
  </article>`;
}

// Map every screenshot to a step, then split non-consecutive returns into
// separate visits. Older saved reports can contain overlapping frame ranges.
function buildAnalysisSteps(job) {
  const timeline = job.report.timeline;
  const frames = [...job.frames].sort((a, b) => a.timestamp - b.timestamp);
  const visits = [];
  const seen = new Set();
  for (const frame of frames) {
    const candidates = timeline.map((step, index) => ({step, index})).filter(({step}) =>
      step.evidence_frame_ids ? step.evidence_frame_ids.includes(frame.id) :
        frame.timestamp >= step.start_seconds && frame.timestamp <= step.end_seconds);
    candidates.sort((a, b) => (a.step.end_seconds - a.step.start_seconds) - (b.step.end_seconds - b.step.start_seconds));
    const match = candidates[0];
    const previous = visits.at(-1);
    if (match && previous?.sourceIndex === match.index) {
      previous.frames.push(frame); previous.end_seconds = frame.timestamp;
    } else {
      const revisit = match && seen.has(match.index);
      visits.push({...(match?.step || {}), sourceIndex: match?.index ?? -1,
        title: match ? (revisit ? `Return to ${match.step.application || 'previous view'}` : match.step.title) : 'Screenshot without a description',
        description: match?.step.description || 'This screenshot was not described in the saved analysis.',
        start_seconds: frame.timestamp, end_seconds: frame.timestamp,
        frames: [frame], uncovered: !match, revisit: !!revisit});
      if (match) seen.add(match.index);
    }
  }
  return visits;
}
const stepRange = (step) => step.start_seconds === step.end_seconds ? stamp(step.start_seconds) : `${stamp(step.start_seconds)}–${stamp(step.end_seconds)}`;

function renderReport(job) {
  showView('report'); clearError();
  reportJob = job;
  analysisSteps = buildAnalysisSteps(job);
  selectedStepIndex = -1;
  $('#view-title').textContent = recordingName(job);
  const report = job.report;
  const frames = Object.fromEntries(job.frames.map((f) => [f.id, f]));
  const frameSteps = Object.fromEntries(analysisSteps.flatMap((step, index) => step.frames.map((frame) => [frame.id, index])));
  const missing = analysisSteps.filter((step) => step.uncovered).length;
  const wf = report.workflow;
  $('#report-view').innerHTML = `
    <div class="report-head">
      <div><h1>${esc(report.title)}</h1><span class="meta" id="recording-meta">${esc(recordingName(job))} · ${stamp(job.metadata.duration)}</span></div>
      <div class="report-actions"><a class="link export" href="/api/jobs/${job.id}/export/markdown" download>Export report ↗</a><span id="report-recording-actions">${recordingActions(job)}</span></div>
    </div>
    <div class="recording-notes" id="saved-notes" hidden></div>
    <section id="report-summary"><h2 class="label">Summary</h2>
      <p class="summary-text">${esc(report.summary)}</p>
      ${wf.occurrences.length > 1 ? `<p class="occ"><span class="wf">${esc(wf.name)}</span> · ${wf.observed_occurrence_count} occurrences
        ${wf.occurrences.map((o) => seekBtn(o.start_seconds, `${stamp(o.start_seconds)}–${stamp(o.end_seconds)}`)).join(' ')}</p>` : ''}
    </section>
    <section id="report-steps">
      <div class="analysis-workspace">
        <div class="video-column">
          <video id="report-video" class="video" src="${esc(job.video_url)}" ${job.frames[0] ? `poster="${esc(job.frames[0].url)}"` : ''} controls preload="metadata" playsinline aria-label="Recording preview"></video>
          <div class="video-caption"><span id="playback-time">0:00 / ${stamp(job.metadata.duration)}</span><span>${job.frames.length} screenshots</span></div>
          <div class="frames" aria-label="Screenshots">${job.frames.map((f) => `<button class="frame" data-frame="${esc(f.id)}" data-seek="${f.timestamp}" title="${esc(analysisSteps[frameSteps[f.id]]?.title)} at ${stamp(f.timestamp)}" aria-label="Screenshot at ${stamp(f.timestamp)}, step ${frameSteps[f.id] + 1}"><img src="${esc(f.url)}" alt="Screenshot at ${stamp(f.timestamp)}" loading="lazy"><span>${stamp(f.timestamp)} · Step ${frameSteps[f.id] + 1}</span></button>`).join('')}</div>
          <div class="step-detail" id="step-detail" hidden>
            <div class="step-detail-top"><span class="step-eyebrow" id="step-position"></span><div class="step-controls"><button class="icon-button" id="previous-step" aria-label="Previous step" title="Previous step">‹</button><button class="icon-button" id="next-step" aria-label="Next step" title="Next step">›</button></div></div>
            <h3 id="step-title"></h3><p class="step-description" id="step-description"></p><div class="step-facts" id="step-facts"></div>
            <details class="step-uncertainty" id="step-uncertainty" hidden><summary>Unclear details</summary><p></p></details>
          </div>
        </div>
        <div class="step-panel">
          <div class="step-panel-head"><h2>Analysis steps</h2><span>${analysisSteps.length} steps</span></div>
          <nav class="step-list" id="analysis-step-list" aria-label="Analysis steps">${analysisSteps.map((step, index) => `<button class="analysis-step" data-step="${index}" aria-controls="step-detail"><span class="step-number">${index + 1}</span><span class="step-content"><strong>${esc(step.title)}</strong><small>${stepRange(step)} · ${esc(step.uncovered ? 'Not described' : step.application)}${step.revisit ? ' · Return visit' : ''}</small></span></button>`).join('')}</nav>
          ${missing ? `<p class="coverage-note">${missing} screenshot${missing === 1 ? '' : 's'} without a description in this saved report.</p>` : ''}
        </div>
      </div>
    </section>
    <section id="report-suggestions"><h2 class="label">Automations</h2>
      ${report.recommendations.length ? report.recommendations.map((r, index) => renderRecommendation(r, frames, index)).join('') : '<div class="empty">No automations identified for this recording.</div>'}
    </section>`;

  renderNotes(job);
  window.automationUI?.refresh();
  $('#report-view').addEventListener('click', handleReportClick);
  $('#previous-step').addEventListener('click', () => selectStep(selectedStepIndex - 1, true));
  $('#next-step').addEventListener('click', () => selectStep(selectedStepIndex + 1, true));
  const video = $('#report-video');
  video.addEventListener('timeupdate', syncPlayback);
  video.addEventListener('seeked', syncPlayback);
  selectStep(0);
  syncPlayback();
  reportObserver?.disconnect();
  reportObserver = new IntersectionObserver((entries) => {
    const visible = entries.filter((entry) => entry.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
    if (!visible.length) return;
    $('#report-nav').querySelectorAll('[data-section]').forEach((button) => {
      const active = button.dataset.section === visible[0].target.id;
      button.classList.toggle('active', active);
      if (active) button.setAttribute('aria-current', 'location'); else button.removeAttribute('aria-current');
    });
  }, {rootMargin: '-60px 0px -55% 0px'});
  for (const id of ['report-summary', 'report-steps', 'report-suggestions']) reportObserver.observe(document.getElementById(id));
}

function handleReportClick(event) {
  const edit = event.target.closest('[data-edit]');
  if (edit) { requestEdit(edit.dataset.edit); return; }
  const remove = event.target.closest('[data-delete]');
  if (remove) { requestDelete(remove.dataset.delete); return; }
  const step = event.target.closest('[data-step]');
  if (step) { selectStep(Number(step.dataset.step), true); return; }
  const seek = event.target.closest('[data-seek]');
  if (seek) {
    const video = $('#report-video');
    video.pause(); video.currentTime = Number(seek.dataset.seek); syncPlayback();
    if (!seek.dataset.frame) $('#report-steps').scrollIntoView({block: 'start'});
  }
}
function selectStep(index, seek = false) {
  const step = analysisSteps[index];
  if (!step || !reportJob) return;
  const changed = selectedStepIndex !== index;
  selectedStepIndex = index;
  if (seek) { const video = $('#report-video'); video.pause(); video.currentTime = step.start_seconds; }
  if (!changed) return;
  $('#step-detail').hidden = false;
  $('#step-position').textContent = `Step ${index + 1} of ${analysisSteps.length} · ${stepRange(step)}`;
  $('#step-title').textContent = step.title;
  $('#step-description').textContent = step.description;
  $('#step-facts').innerHTML = [step.application, step.evidence_type === 'inferred' ? 'Inferred' : (step.uncovered ? 'Not described' : 'Observed'),
    `${step.frames.length} screenshot${step.frames.length === 1 ? '' : 's'}`, step.confidence ? `${step.confidence[0].toUpperCase()}${step.confidence.slice(1)} confidence` : ''].filter(Boolean).map((value) => `<span>${esc(value)}</span>`).join('');
  const uncertainty = step.uncertainty?.trim();
  $('#step-uncertainty').hidden = !uncertainty || /^(none|n\/a|no uncertainty|none noted|not applicable)[.!]?$/i.test(uncertainty);
  $('#step-uncertainty').open = false;
  $('#step-uncertainty p').textContent = uncertainty || '';
  $('#previous-step').disabled = index === 0;
  $('#next-step').disabled = index === analysisSteps.length - 1;
  $('#analysis-step-list').querySelectorAll('[data-step]').forEach((button) => {
    const active = Number(button.dataset.step) === index;
    button.classList.toggle('active', active);
    if (active) button.setAttribute('aria-current', 'step'); else button.removeAttribute('aria-current');
  });
  // Keep the current row visible within its own panel without moving the page.
  const list = $('#analysis-step-list');
  const row = list.querySelector(`[data-step="${index}"]`);
  const rowRect = row.getBoundingClientRect(), listRect = list.getBoundingClientRect();
  if (rowRect.top < listRect.top) list.scrollTop += rowRect.top - listRect.top - 6;
  else if (rowRect.bottom > listRect.bottom) list.scrollTop += rowRect.bottom - listRect.bottom + 6;
}
function syncPlayback() {
  const video = $('#report-video');
  if (!video || !reportJob) return;
  const time = video.currentTime;
  $('#playback-time').textContent = `${stamp(time)} / ${stamp(reportJob.metadata.duration)}`;
  let index = 0;
  analysisSteps.forEach((step, i) => { if (step.start_seconds <= time + .02) index = i; });
  selectStep(index);
  const frame = reportJob.frames.filter((f) => f.timestamp <= time + .02).at(-1);
  $('#report-view').querySelectorAll('[data-frame]').forEach((button) => {
    const active = button.dataset.frame === frame?.id;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });
}

/* ---- Init ---- */
async function init() {
  await Promise.all([refreshHistory(), (async () => {
    try {
      const health = await api('/api/health');
      const ready = health.configured && health.ffmpeg && !health.error;
      const el = $('#status');
      el.classList.toggle('bad', !ready);
      el.innerHTML = `<span class="dot"></span>${ready ? 'Ready' : 'Setup needed'}`;
      el.title = `${health.model} · ${ready ? 'API key configured' : 'Configuration incomplete'}`;
      if (!ready) showError(new Error(health.error || (!health.configured ? 'Add OPENROUTER_API_KEY to .env and restart the app.' : 'FFmpeg is required. Install it and restart the app.')));
    } catch { showError(new Error('Cannot reach the local server.')); }
  })()]);
  const match = location.hash.match(/^#job=([a-f0-9]{32})$/);
  if (match) openJob(match[1]);
  else if (location.hash === '#recordings') openLibrary();
  else if (location.hash === '#automations') window.automationUI?.open();
  else if (location.hash === '#activity') window.activityUI?.open();
}
init();
