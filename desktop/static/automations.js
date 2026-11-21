/* A single supported recipe, backed by an actual Mistral schedule. */
(() => {
  let state = {status: 'inactive', enabled: false};
  let connection = null;
  let pendingActivation = null;
  let loading = null;
  let changing = false;
  let connecting = false;
  const supports = (rec) => rec.automation_recipe === 'meeting_reply_drafts' || rec.title?.toLowerCase().trim() === 'draft meeting replies from calendar availability';
  const html = (element, value) => { if (element.innerHTML !== value) element.innerHTML = value; };
  const labels = {inactive: 'Not active', activating: 'Activating…', active: 'Active', paused: 'Paused', pausing: 'Pausing…', paused_local: 'Paused on this Mac', error: 'Needs attention', offline: 'Waiting for this Mac', unverified: 'Connection unavailable'};
  const ready = () => connection && ['connected', 'mail', 'calendar', 'runtime_ready'].every(key => connection[key]);
  const badge = () => `<span class="automation-badge ${state.status === 'active' ? 'on' : state.status === 'error' ? 'issue' : ''}"><span></span>${esc(labels[state.status] || 'Not active')}</span>`;
  const action = (index, library = false) => {
    if (['activating', 'pausing'].includes(state.status)) return `<button class="secondary" disabled>${esc(labels[state.status])}</button>`;
    if (state.enabled || state.status === 'paused_local') return '<button class="secondary" data-automation-pause>Pause</button>';
    return `<button class="primary" ${library ? 'data-automation-resume' : `data-automation-activate="${index}"`}>${['paused', 'error'].includes(state.status) ? 'Resume' : 'Activate'}</button>`;
  };
  function stateText() {
    if (state.enabled && state.retry_after && new Date(state.retry_after) > new Date()) return `Mail is busy · Retrying at ${new Date(state.retry_after).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}`;
    const last = state.last_checked_at ? `Last checked ${fmtDate(state.last_checked_at)}` : 'First check pending';
    return state.message || (state.status === 'active' ? last : 'New meeting requests · Mail + Calendar · Drafts for review');
  }
  function paint() {
    const count = state.source_job_id ? 1 : 0;
    $('#automation-count').textContent = count;
    $('#nav-automations').setAttribute('aria-label', `Automations, ${count} automation${count === 1 ? '' : 's'}`);
    if (reportJob) reportJob.report.recommendations.forEach((rec, index) => {
      const target = document.querySelector(`[data-automation-action="${index}"]`);
      const detail = document.querySelector(`[data-automation-state="${index}"]`);
      if (!target || !detail) return;
      html(target, supports(rec) ? action(index) : '<span class="automation-unavailable">Setup not yet supported</span>');
      html(detail, supports(rec) ? `${badge()}<span>${esc(stateText())}</span><small>Checks every 30 seconds. Proposes weekday slots, 09:00–17:00, in your Mac’s timezone. You review and send.</small>` : '');
    });
    const library = $('#automation-library');
    if (!state.source_job_id) {
      html(library, '<div class="automation-empty"><div class="automation-empty-mark" aria-hidden="true">⌁</div><h2>No active automations</h2><p>Open a recording and activate a supported automation.</p><button class="secondary" data-open-recordings>View recordings</button></div>');
    } else {
      html(library, `<article class="suggestion"><div class="suggestion-heading"><h3>${esc(state.title || 'Meeting reply drafts')}</h3>${action(0, true)}</div><p>Prepare reply drafts for new meeting requests using your calendar availability.</p><div class="automation-state">${badge()}<span>${esc(stateText())}</span></div><div class="automation-facts"><span>Mail + Calendar</span><span>Every 30 seconds</span><span>Drafts only</span></div><p class="automation-note">Runs while this Mac is awake and online, including when chonkytasks is closed. Resuming catches up on messages received while paused.</p></article>`);
    }
  }
  async function refresh() {
    if (loading) return loading;
    loading = (async () => {
      try { state = await api('/api/automations'); paint(); }
      catch (error) {
        if (currentView === 'automations') showError(error);
        document.querySelectorAll('[data-automation-state]').forEach(el => { el.textContent = 'Automation status unavailable.'; });
      }
    })();
    try { await loading; } finally { loading = null; }
  }
  function open() {
    stopWatching(); activeJobId = null; clearError();
    history.replaceState(null, '', '/#automations'); showView('automations');
    paint(); refresh(); closeMobileSidebar(); window.scrollTo(0, 0);
  }
  function connectionError(error) { $('#connection-error').textContent = error.message || String(error); $('#connection-error').hidden = false; }
  function paintConnection() {
    $('#mistral-connection-state').textContent = connection?.connected ? 'Connected' : 'Not connected';
    $('#mistral-key-form').hidden = !!connection?.connected;
    $('#change-mistral-key').hidden = !connection?.connected;
    $('#mac-connection-state').textContent = connection?.mail ? 'Connected' : 'Access needed';
    $('#calendar-connection-state').textContent = connection?.calendar ? 'Connected' : 'Access needed';
    for (const [id, key] of [['connect-mac', 'mail'], ['connect-calendar', 'calendar']]) {
      $(`#${id}`).textContent = connection?.[key] ? 'Connected' : 'Allow access';
      $(`#${id}`).disabled = connecting || !!connection?.[key];
    }
    if (connection?.mail && connection?.calendar) $('#permission-help').hidden = true;
    else if (['denied', 'restricted', 'write_only'].includes(connection?.calendar_status)) $('#permission-help').hidden = false;
    $('#connection-done').textContent = pendingActivation ? 'Activate automation' : 'Done';
    $('#connection-done').disabled = connecting || (!!pendingActivation && !ready());
    if (connection?.message) connectionError(new Error(connection.message));
  }
  async function openConnections(intent = null) {
    pendingActivation = intent;
    $('#connection-error').hidden = true;
    $('#mistral-key').value = '';
    connection = null; paintConnection();
    if (!$('#connection-dialog').open) $('#connection-dialog').showModal();
    try { connection = await api('/api/connections'); paintConnection(); }
    catch (error) { connectionError(error); }
  }
  async function activate(intent) {
    if (changing) return;
    changing = true;
    try {
      connection = await api('/api/connections');
      if (!ready()) { await openConnections(intent); return; }
      state = await api(intent.resume ? '/api/automations/resume' : '/api/automations/activate', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: intent.resume ? undefined : JSON.stringify(intent)});
      paint();
      if ($('#connection-dialog').open) $('#connection-dialog').close();
      pendingActivation = null;
    } catch (error) {
      if ($('#connection-dialog').open) connectionError(error); else showError(error);
    } finally { changing = false; }
  }
  document.addEventListener('click', async event => {
    const activateButton = event.target.closest('[data-automation-activate]');
    if (activateButton && reportJob) { activate({job_id: reportJob.id, recommendation_index: Number(activateButton.dataset.automationActivate)}); return; }
    if (event.target.closest('[data-automation-resume]')) { activate({resume: true}); return; }
    if (event.target.closest('[data-open-recordings]')) { openLibrary(); return; }
    const settings = event.target.closest('[data-privacy-pane]');
    if (settings) {
      try { await api(`/api/connections/mac/settings/${settings.dataset.privacyPane}`, {method: 'POST'}); }
      catch (error) { connectionError(error); }
      return;
    }
    if (event.target.closest('[data-automation-pause]') && !changing) {
      changing = true;
      try { state = {...state, status: 'pausing'}; paint(); state = await api('/api/automations/pause', {method: 'POST'}); paint(); }
      catch (error) { showError(error); await refresh(); }
      finally { changing = false; }
    }
  });
  $('#nav-automations').addEventListener('click', open);
  $('#connections-button').addEventListener('click', () => openConnections());
  $('#connection-close').addEventListener('click', () => { if (!connecting) $('#connection-dialog').close(); });
  $('#connection-dialog').addEventListener('cancel', event => { if (connecting) event.preventDefault(); });
  $('#connection-dialog').addEventListener('close', () => { $('#mistral-key').value = ''; pendingActivation = null; });
  $('#change-mistral-key').addEventListener('click', () => { $('#mistral-key-form').hidden = false; $('#mistral-key').focus(); });
  $('#mistral-key-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (connecting) return;
    connecting = true; $('#connect-mistral').disabled = true; $('#connection-error').hidden = true; paintConnection();
    try {
      await api('/api/connections/mistral', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({api_key: $('#mistral-key').value.trim()})});
      $('#mistral-key').value = ''; connection = await api('/api/connections');
    } catch (error) { connectionError(error); }
    finally { connecting = false; $('#connect-mistral').disabled = false; paintConnection(); }
  });
  async function connectMac(service) {
    if (connecting) return;
    connecting = true; paintConnection(); $(`#${service === 'calendar' ? 'connect-calendar' : 'connect-mac'}`).textContent = 'Waiting for macOS…'; $('#connection-error').hidden = true;
    try {
      await api(`/api/connections/mac?service=${service}`, {method: 'POST'});
      connection = await api('/api/connections');
      if (!connection[service]) {
        $('#permission-help').hidden = false;
        throw new Error(`${service === 'calendar' ? 'Calendar' : 'Mail'} access is still off. Use the settings links above if macOS did not show a prompt.`);
      }
    } catch (error) { $('#permission-help').hidden = false; connectionError(error); }
    finally { connecting = false; paintConnection(); }
  }
  $('#connect-mac').addEventListener('click', () => connectMac('mail'));
  $('#connect-calendar').addEventListener('click', () => connectMac('calendar'));
  $('#connection-done').addEventListener('click', () => {
    if (pendingActivation) activate(pendingActivation); else $('#connection-dialog').close();
  });
  async function refreshConnections() {
    if (!$('#connection-dialog').open || connecting) return;
    try {
      connection = await api('/api/connections'); paintConnection();
      if (connection.mail && connection.calendar) $('#connection-error').hidden = true;
    } catch (_) { /* Keep the current form while the local service restarts. */ }
  }
  window.addEventListener('focus', refreshConnections);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshConnections(); });
  window.automationUI = {refresh, open, supports};
  setInterval(() => { if (!document.hidden && ['report', 'automations'].includes(currentView)) refresh(); }, 3000);
  refresh();
})();
