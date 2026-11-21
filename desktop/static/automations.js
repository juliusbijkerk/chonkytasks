/* Activation and status for the recipes supported by the background worker. */
(() => {
  const meetingKind = 'meeting_reply_drafts';
  const office = {
    mailToTask: {
      source: 'mail', title: 'Create a task from email',
      summary: 'Prepare a task draft from a matching email.',
      input: 'The message should include Task and Due lines. Use YYYY-MM-DD for the due date.',
      placeholder: 'e.g. Action:',
      prepare: 'Prepare a task draft from the matching message.',
      save: 'Review the source and save the task on this Mac.',
    },
    mailToNote: {
      source: 'mail', title: 'Save an email as a note',
      summary: 'Prepare a note from a matching email.',
      input: 'The note includes the sender, subject and message.',
      placeholder: 'e.g. Notes:',
      prepare: 'Prepare a note from the matching message.',
      save: 'Review the source and save the note on this Mac.',
    },
    invoiceFiling: {
      source: 'files', title: 'File invoices',
      summary: 'Prepare invoices for filing from an input folder.',
      input: 'Add text or Markdown invoices with Invoice, Vendor, Date and Total fields.',
      prepare: 'Choose a filing location from the vendor and invoice date.',
      save: 'Review the invoice destination and save the copy on this Mac.',
    },
    spreadsheetReport: {
      source: 'files', title: 'Prepare a spreadsheet report',
      summary: 'Summarize incoming spreadsheet exports.',
      input: 'Add a CSV export with Status and Amount columns.',
      prepare: 'Prepare a report using the status filter you set.',
      save: 'Review the report and save it on this Mac.',
    },
    orderDraft: {
      source: 'files', title: 'Prepare order drafts',
      summary: 'Prepare an order draft from an incoming order request.',
      input: 'Add text or Markdown requests with Order, Customer, SKU and Quantity fields.',
      prepare: 'Turn a detected order request into a structured draft.',
      save: 'Review the details and save the order draft on this Mac.',
    },
  };
  const labels = {inactive: 'Not active', activating: 'Activating…', active: 'Active', paused: 'Paused', pausing: 'Pausing…', paused_local: 'Paused on this Mac', error: 'Needs attention', offline: 'Waiting for this Mac', unverified: 'Connection unavailable'};
  const inactive = {status: 'inactive', enabled: false};
  let meetingState = inactive;
  let officeStates = [];
  let connection = null;
  let pendingActivation = null;
  let loading = null;
  let changing = false;
  let connecting = false;
  let stateVersion = 0;
  const html = (element, value) => { if (element.innerHTML !== value) element.innerHTML = value; };
  const officeRecipe = kind => Object.hasOwn(office, kind) ? office[kind] : null;
  const workflowTitle = kind => officeRecipe(kind)?.title;
  const recipe = rec => {
    if ('activation_ready' in rec) {
      const kind = rec.automation_recipe;
      return rec.activation_ready === true && (kind === meetingKind || officeRecipe(kind)) ? kind : null;
    }
    const isMeeting = rec.automation_recipe === meetingKind || rec.title?.toLowerCase().trim() === 'draft meeting replies from calendar availability';
    return isMeeting ? meetingKind : null;
  };
  const supports = rec => !!recipe(rec);
  const sourceLabel = info => info.source === 'mail' ? 'Mail' : 'Input folder';
  function details(rec) {
    const kind = recipe(rec);
    if (!kind) return null;
    const info = officeRecipe(kind);
    if (info) return {
      steps: [info.source === 'mail' ? 'Check new Mail messages for the subject prefix you set.' : 'Check the workflow’s input folder for new files.', info.prepare, info.save],
      apps: info.source === 'mail' ? 'Mail on this Mac · Mistral Workflows schedule' : 'Files on this Mac · Mistral Workflows schedule',
    };
    return {
      steps: ['Checks new messages every 30 seconds while this Mac is awake and online.', 'Identifies meeting requests and checks your calendar for availability.', 'Prepares an acceptance, the next free weekday slot, or a request for clarification.', 'You review and approve the reply and any calendar event.'],
      apps: 'Mail and Calendar on this Mac · Mistral Workflows',
    };
  }
  function ready(intent) {
    if (!connection?.connected || !connection.runtime_ready) return false;
    const info = officeRecipe(intent?.kind);
    if (!info) return connection.mail && connection.calendar;
    return info.source === 'files' || connection.mail;
  }
  function setState(data, kind = meetingKind) {
    if (Array.isArray(data.office_workflows)) {
      meetingState = data;
      officeStates = data.office_workflows;
    } else if (kind === meetingKind) meetingState = data;
    else if (officeStates.some(state => state.kind === kind)) officeStates = officeStates.map(state => state.kind === kind ? data : state);
    else officeStates = [...officeStates, data];
    paint();
  }
  const stateFor = kind => kind === meetingKind ? meetingState : officeStates.find(state => state.kind === kind) || inactive;
  const badge = state => `<span class="automation-badge ${state.status === 'active' ? 'on' : state.status === 'error' ? 'issue' : ''}"><span></span>${esc(labels[state.status] || 'Not active')}</span>`;
  function action(kind, state, index) {
    if (['activating', 'pausing'].includes(state.status)) return `<button class="secondary" disabled>${esc(labels[state.status])}</button>`;
    if (state.enabled || state.status === 'paused_local') return `<button class="secondary" data-automation-pause="${esc(kind)}">Pause</button>`;
    const resumable = state.source_job_id && (kind !== meetingKind || index === undefined);
    if (resumable) return `<button class="primary" data-automation-resume="${esc(kind)}">Resume</button>`;
    const label = kind === meetingKind ? (['paused', 'error'].includes(state.status) ? 'Resume' : 'Activate') : 'Set up';
    return `<button class="primary" data-automation-activate="${index}">${label}</button>`;
  }
  function stateText(kind, state) {
    const info = officeRecipe(kind);
    if (state.enabled && state.retry_after && new Date(state.retry_after) > new Date()) return `Mail is busy · Retrying at ${new Date(state.retry_after).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}`;
    const lastChecked = state.last_checked_at || state.checked_at;
    const last = lastChecked ? `Last checked ${fmtDate(lastChecked)}` : 'First check pending';
    return state.message || (state.status === 'active' ? last : info ? `${sourceLabel(info)} · Results for review` : 'New meeting requests · Mail + Calendar · Drafts for review');
  }
  function officeCard(state) {
    const kind = state.kind;
    const info = officeRecipe(kind);
    if (!info) return '';
    const {subject_prefix: prefix, status_filter: status} = state.config || {};
    const input = state.input_folder ? `<div class="automation-folder"><button class="secondary" data-automation-open-inbox="${esc(kind)}">Open input folder</button><small>${esc(state.input_folder)}</small></div>` : '';
    return `<article class="suggestion"><div class="suggestion-heading"><h3>${esc(state.title || info.title)}</h3>${action(kind, state)}</div>
      <p>${esc(info.summary)}</p><div class="automation-state">${badge(state)}<span>${esc(stateText(kind, state))}</span></div>
      <div class="automation-facts"><span>${sourceLabel(info)}</span><span>Every 30 seconds</span><span>Approval required</span>
      ${prefix ? `<span>Subject starts with “${esc(prefix)}”</span>` : ''}${status ? `<span>Status is “${esc(status)}”</span>` : ''}</div>
      ${input}<p class="automation-note">Review and save each result from Activity.</p></article>`;
  }
  function paint() {
    const count = Number(!!meetingState.source_job_id) + officeStates.length;
    $('#automation-count').textContent = count;
    $('#nav-automations').setAttribute('aria-label', `Automations, ${count} automation${count === 1 ? '' : 's'}`);
    if (reportJob) reportJob.report.recommendations.forEach((rec, index) => {
      const target = document.querySelector(`[data-automation-action="${index}"]`);
      const detail = document.querySelector(`[data-automation-state="${index}"]`);
      if (!target || !detail) return;
      const kind = recipe(rec);
      const state = stateFor(kind);
      html(target, kind ? action(kind, state, index) : '<span class="automation-unavailable">Setup not yet supported</span>');
      const hint = kind === meetingKind ? 'Checks every 30 seconds. The reply and any calendar event wait for approval.' :
        'Checks every 30 seconds. Review and save each result in Activity.';
      html(detail, kind ? `${badge(state)}<span>${esc(stateText(kind, state))}</span><small>${hint}</small>` : '');
    });
    const library = $('#automation-library');
    if (!count) {
      html(library, '<div class="automation-empty"><div class="automation-empty-mark" aria-hidden="true">⌁</div><h2>No automations set up</h2><p>Open a recording and activate a supported automation.</p><button class="secondary" data-open-recordings>View recordings</button></div>');
      return;
    }
    const state = meetingState;
    const meeting = state.source_job_id ? `<article class="suggestion"><div class="suggestion-heading"><h3>${esc(state.title || 'Meeting reply drafts')}</h3>${action(meetingKind, state)}</div><p>Prepare reply drafts for new meeting requests using your calendar availability.</p><div class="automation-state">${badge(state)}<span>${esc(stateText(meetingKind, state))}</span></div><div class="automation-facts"><span>Mail + Calendar</span><span>Every 30 seconds</span><span>Drafts only</span></div><p class="automation-note">Runs while this Mac is awake and online, including when chonkytasks is closed. Resuming catches up on messages received while paused.</p></article>` : '';
    html(library, meeting + officeStates.map(officeCard).join(''));
  }
  async function refresh() {
    if (loading) return loading;
    if (changing) return;
    const version = stateVersion;
    loading = (async () => {
      try {
        const data = await api('/api/automations');
        if (version === stateVersion) setState(data);
      } catch (error) {
        if (version !== stateVersion) return;
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
    const info = officeRecipe(pendingActivation?.kind);
    const settingUp = info && !pendingActivation.resume;
    const requiresMail = !info || info.source === 'mail';
    const requiresCalendar = !info;
    $('#connection-title').textContent = settingUp ? pendingActivation.title || info.title : 'Connect your apps';
    $('#connection-intro').textContent = settingUp ? info.summary : 'Connect once, then activate automations in one click.';
    $('#workflow-config').hidden = !settingUp;
    $('#workflow-subject-field').hidden = !settingUp || !requiresMail;
    $('#workflow-subject-prefix').required = !!settingUp && requiresMail;
    $('#workflow-status-field').hidden = !settingUp || pendingActivation.kind !== 'spreadsheetReport';
    if (settingUp) {
      $('#workflow-source-hint').textContent = requiresMail ? `Only new Mail messages with this subject prefix are checked. ${info.input}` : `${info.input} You can open this folder from Automations after setup.`;
      $('#workflow-subject-prefix').placeholder = info.placeholder || '';
      $('#workflow-status-filter').placeholder = 'e.g. Open';
    }
    $('#mistral-connection-state').textContent = connection?.connected ? 'Connected' : 'Not connected';
    $('#mistral-key-form').hidden = !!connection?.connected;
    $('#change-mistral-key').hidden = !connection?.connected;
    $('#mac-connections').hidden = !requiresMail;
    $('#calendar-connection').hidden = !requiresCalendar;
    $('[data-privacy-pane="Calendars"]').hidden = !requiresCalendar;
    $('#permission-copy').textContent = requiresCalendar ? 'Enable calendar access for chonkytasks in Calendars settings. Mail access is under Automation.' : 'Enable Mail access for chonkytasks under Automation in macOS settings.';
    $('#mac-connection-state').textContent = connection?.mail ? 'Connected' : 'Access needed';
    $('#calendar-connection-state').textContent = connection?.calendar ? 'Connected' : 'Access needed';
    for (const [id, key] of [['connect-mac', 'mail'], ['connect-calendar', 'calendar']]) {
      $(`#${id}`).textContent = connection?.[key] ? 'Connected' : 'Allow access';
      $(`#${id}`).disabled = changing || connecting || !!connection?.[key];
    }
    if (!requiresMail || (connection?.mail && (!requiresCalendar || connection?.calendar))) $('#permission-help').hidden = true;
    else if (requiresCalendar && ['denied', 'restricted', 'write_only'].includes(connection?.calendar_status)) $('#permission-help').hidden = false;
    $('#connection-footnote').textContent = info ?
      'Mistral Workflows schedules checks every 30 seconds while this Mac is awake and online. Sources and draft results stay on this Mac.' :
      'The meeting workflow may send new message text to Mistral to identify requests. Checks run every 30 seconds while this Mac is awake and online, even after you close chonkytasks.';
    $('#connection-done').textContent = pendingActivation ? (pendingActivation.resume ? 'Resume automation' : 'Activate automation') : 'Done';
    $('#connection-done').disabled = connecting || (!!pendingActivation && (changing || !ready(pendingActivation)));
    $('#connection-close').disabled = changing || connecting;
    if (connection?.message) connectionError(new Error(connection.message));
    else if (pendingActivation && connection && !connection.runtime_ready) connectionError(new Error('The background service is missing. Install the desktop app with its Workflows runtime to continue.'));
  }
  async function openConnections(intent = null) {
    pendingActivation = intent;
    $('#connection-error').hidden = true;
    $('#permission-help').hidden = true;
    $('#mistral-key').value = '';
    $('#workflow-subject-prefix').value = intent?.config?.subject_prefix || '';
    $('#workflow-subject-prefix').setCustomValidity('');
    $('#workflow-status-filter').value = intent?.config?.status_filter || '';
    connection = null; paintConnection();
    if (!$('#connection-dialog').open) $('#connection-dialog').showModal();
    try { connection = await api('/api/connections'); paintConnection(); }
    catch (error) { connectionError(error); }
  }
  const workflowPath = (kind, operation) => kind === meetingKind ? `/api/automations/${operation}` : `/api/automations/${encodeURIComponent(kind)}/${operation}`;
  async function activate(intent) {
    if (changing) return;
    changing = true; stateVersion++;
    if ($('#connection-dialog').open) paintConnection();
    try {
      connection = await api('/api/connections');
      if (!ready(intent)) { await openConnections(intent); return; }
      const {job_id, recommendation_index, config} = intent;
      const options = intent.resume ? {method: 'POST'} : {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({job_id, recommendation_index, config}),
      };
      setState(await api(intent.resume ? workflowPath(intent.kind, 'resume') : '/api/automations/activate', options), intent.kind);
      if ($('#connection-dialog').open) $('#connection-dialog').close();
      pendingActivation = null;
    } catch (error) {
      if ($('#connection-dialog').open) connectionError(error); else showError(error);
    } finally {
      changing = false;
      if ($('#connection-dialog').open) paintConnection();
    }
  }
  document.addEventListener('click', async event => {
    const activateButton = event.target.closest('[data-automation-activate]');
    if (activateButton && reportJob && !changing) {
      const index = Number(activateButton.dataset.automationActivate);
      const rec = reportJob.report.recommendations[index];
      if (!rec) return;
      const kind = recipe(rec);
      if (!kind) return;
      const intent = {kind, title: rec.title, job_id: reportJob.id, recommendation_index: index};
      if (officeRecipe(kind)) openConnections(intent); else activate(intent);
      return;
    }
    const resumeButton = event.target.closest('[data-automation-resume]');
    if (resumeButton) { activate({kind: resumeButton.dataset.automationResume, resume: true}); return; }
    if (event.target.closest('[data-open-recordings]')) { openLibrary(); return; }
    const settings = event.target.closest('[data-privacy-pane]');
    if (settings) {
      try { await api(`/api/connections/mac/settings/${settings.dataset.privacyPane}`, {method: 'POST'}); }
      catch (error) { connectionError(error); }
      return;
    }
    const inboxButton = event.target.closest('[data-automation-open-inbox]');
    if (inboxButton) {
      try { await api(workflowPath(inboxButton.dataset.automationOpenInbox, 'open-inbox'), {method: 'POST'}); }
      catch (error) { showError(error); }
      return;
    }
    const pauseButton = event.target.closest('[data-automation-pause]');
    if (pauseButton && !changing) {
      changing = true; stateVersion++; pauseButton.disabled = true;
      const kind = pauseButton.dataset.automationPause;
      try { setState(await api(workflowPath(kind, 'pause'), {method: 'POST'}), kind); }
      catch (error) { showError(error); }
      finally { changing = false; pauseButton.disabled = false; }
    }
  });
  $('#nav-automations').addEventListener('click', open);
  $('#connections-button').addEventListener('click', () => openConnections());
  $('#connection-close').addEventListener('click', () => { if (!connecting && !changing) $('#connection-dialog').close(); });
  $('#connection-dialog').addEventListener('cancel', event => { if (connecting || changing) event.preventDefault(); });
  $('#connection-dialog').addEventListener('close', () => { $('#mistral-key').value = ''; pendingActivation = null; });
  $('#change-mistral-key').addEventListener('click', () => { $('#mistral-key-form').hidden = false; $('#mistral-key').focus(); });
  $('#mistral-key-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (connecting || changing) return;
    connecting = true; $('#connect-mistral').disabled = true; $('#connection-error').hidden = true; paintConnection();
    try {
      await api('/api/connections/mistral', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({api_key: $('#mistral-key').value.trim()})});
      $('#mistral-key').value = ''; connection = await api('/api/connections');
      await refreshHealth();
    } catch (error) { connectionError(error); }
    finally { connecting = false; $('#connect-mistral').disabled = false; paintConnection(); }
  });
  async function connectMac(service) {
    if (connecting || changing) return;
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
  function finishConnection() {
    if (connecting || changing) return;
    if (!pendingActivation) { $('#connection-dialog').close(); return; }
    if (!ready(pendingActivation)) return;
    const intent = pendingActivation;
    const info = officeRecipe(intent.kind);
    if (!info || intent.resume) { activate(intent); return; }
    const prefix = $('#workflow-subject-prefix');
    prefix.setCustomValidity(prefix.required && !prefix.value.trim() ? 'Enter a subject prefix to match.' : '');
    if (!$('#workflow-config').reportValidity()) return;
    const config = {};
    if (info.source === 'mail') config.subject_prefix = prefix.value.trim();
    if (intent.kind === 'spreadsheetReport') config.status_filter = $('#workflow-status-filter').value.trim();
    activate({...intent, config});
  }
  $('#connection-done').addEventListener('click', finishConnection);
  $('#workflow-subject-prefix').addEventListener('input', event => event.target.setCustomValidity(''));
  $('#workflow-config').addEventListener('submit', event => { event.preventDefault(); finishConnection(); });
  async function refreshConnections() {
    if (!$('#connection-dialog').open || connecting || changing) return;
    try {
      connection = await api('/api/connections'); paintConnection();
      if (ready(pendingActivation)) $('#connection-error').hidden = true;
    } catch (_) { /* Keep the current form while the local service restarts. */ }
  }
  window.addEventListener('focus', refreshConnections);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshConnections(); });
  window.automationUI = {refresh, open, supports, details, workflowTitle};
  setInterval(() => { if (!document.hidden && ['report', 'automations'].includes(currentView)) refresh(); }, 3000);
  refresh();
})();
