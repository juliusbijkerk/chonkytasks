/* Persistent background-run history; all entries come from the worker ledger. */
(() => {
  let items = [], nextBefore = null, filter = 'all', loading = null;
  let clearing = false, clearable = 0, clearedAt = null, generation = 0;
  const time = value => new Date(value).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'});
  const date = value => new Date(value).toLocaleDateString([], {month: 'short', day: 'numeric'});
  const duration = item => {
    const seconds = Math.max(0, Math.round((new Date(item.finished_at || Date.now()) - new Date(item.started_at)) / 1000));
    return seconds < 60 ? `${seconds}s` : stamp(seconds);
  };
  function runSummary(item, drafts, running, deferred, failed) {
    const office = item.workflow_kind;
    const fromMail = !office || office.startsWith('mailTo');
    const draftCount = item.drafted || drafts.length;
    const noun = fromMail ? 'message' : 'file';
    const checked = `${item.checked} ${noun}${item.checked === 1 ? '' : 's'} checked`;
    if (running || deferred || failed) {
      const title = running ? 'Check in progress' : failed ? 'Check needs attention' : fromMail ? 'Waiting for Mail' : 'Check deferred';
      return {title, result: item.message, checked};
    }
    if (office) {
      const allSaved = drafts.length && drafts.every(draft => draft.status === 'saved');
      if (allSaved) return {title: drafts.length === 1 ? 'Result saved' : 'Results saved', result: 'Saved on this Mac.', checked};
      const pending = drafts.length ? drafts.filter(draft => draft.status === 'ready').length : draftCount;
      if (pending) return {title: pending === 1 ? 'Result ready to review' : `${pending} results ready to review`, result: 'Review the source and proposed output before saving.', checked};
      return {title: 'Check completed', result: item.message || 'No new items matched this workflow.', checked};
    }
    const allSent = drafts.length && drafts.every(draft => draft.status === 'sent');
    const title = allSent ? `${drafts.length > 1 ? 'Replies' : 'Reply'} sent to Mail` : draftCount ? `${draftCount} reply draft${draftCount === 1 ? '' : 's'} created` : 'Check completed';
    const result = allSent ? 'Reply sent to Mail.' : draftCount ? 'Preview the email and decide whether to send.' : item.checked ? 'No meeting requests needed a reply.' : 'No new messages to process.';
    return {title, result, checked};
  }
  function draftButton(draft, index, drafts, office) {
    if (office) {
      const label = draft.status === 'saved' ? 'View saved result' : 'Review result';
      return `<button class="secondary" data-review-workflow-draft="${esc(draft.id)}">${label}${drafts.length > 1 ? ` ${index + 1}` : ''}</button>`;
    }
    const label = draft.status === 'sent' ? 'Completed' : draft.status === 'calendar_attention' ? 'Review calendar event' : draft.status === 'send_uncertain' ? 'Check send status' : `Review reply${drafts.length > 1 ? ` ${index + 1}` : ''}`;
    return `<button class="secondary" data-review-draft="${esc(draft.id)}">${label}</button>`;
  }
  function paint() {
    const target = $('#activity-list');
    const html = items.length ? items.map(item => {
      const running = item.status === 'running';
      const deferred = item.status === 'deferred';
      const failed = ['failed', 'interrupted'].includes(item.status);
      const office = item.workflow_kind;
      const drafts = (office ? item.workflow_drafts : item.drafts) || [];
      const {title, result, checked} = runSummary(item, drafts, running, deferred, failed);
      return `<article class="activity-row ${item.unread ? 'unread' : ''}">
        <span class="activity-symbol ${running ? 'running' : failed ? 'failed' : item.drafted ? 'result' : ''}" aria-hidden="true">${running ? `<img class="activity-chonk" src="/static/brand/mark-mistral.svg" data-chonk-motion="spin" data-motion-key="run-${esc(item.id)}" alt="" width="30" height="30">` : deferred ? '◷' : failed ? '!' : item.drafted ? '↗' : '✓'}</span>
        <div class="activity-content"><div class="activity-row-heading"><h3>${esc(title)}</h3><time datetime="${esc(item.started_at)}" title="${esc(fmtDate(item.started_at))}">${esc(date(item.started_at))} · ${esc(time(item.started_at))}</time></div>
          <p>${esc(result)}</p><div class="activity-meta"><span>${esc(item.title)}</span><span>${esc(checked)}</span><span>${esc(duration(item))}${running ? ' elapsed' : ''}</span></div>
          ${failed && item.drafted ? `<p>${item.drafted} draft${item.drafted === 1 ? '' : 's'} saved before this check stopped.</p>` : ''}
          ${drafts.length ? `<div class="activity-drafts">${drafts.map((draft, index) => draftButton(draft, index, drafts, office)).join('')}</div>` : ''}
        </div>
      </article>`;
    }).join('') : `<div class="activity-empty"><span aria-hidden="true">${filter === 'results' ? '✓' : '◷'}</span><h2>${filter === 'results' ? 'No notifications to show' : 'No activity to show'}</h2><p>${filter === 'results' ? 'New drafts and checks needing attention will appear here.' : 'New background runs and their results will appear here.'}</p></div>`;
    window.chonkMotion.render(target, html);
    $('#activity-more').hidden = !nextBefore;
    $('#activity-clear').disabled = clearing || !clearable;
    $('#activity-clear').textContent = clearing ? 'Clearing…' : 'Clear notifications';
    $('#activity-read').disabled = clearing;
    document.querySelectorAll('[data-activity-filter]').forEach(button => {
      const selected = button.dataset.activityFilter === filter;
      button.classList.toggle('active', selected); button.setAttribute('aria-pressed', String(selected));
    });
  }
  async function refresh(more = false) {
    if (loading) return loading;
    const requestGeneration = generation;
    loading = (async () => {
      try {
        const visible = currentView === 'activity';
        const params = new URLSearchParams({limit: visible ? '50' : '1', results_only: String(filter === 'results')});
        if (more && nextBefore) params.set('before', nextBefore);
        const data = await api(`/api/activity?${params}`);
        if (requestGeneration !== generation) return;
        clearable = data.clearable_count ?? data.items.filter(item => item.status !== 'running').length;
        if (clearedAt !== (data.cleared_at || null)) {
          items = []; nextBefore = null; clearedAt = data.cleared_at || null;
        }
        const count = $('#activity-count');
        count.textContent = data.unread_count > 99 ? '99+' : data.unread_count;
        count.hidden = !data.unread_count;
        $('#nav-activity').setAttribute('aria-label', data.unread_count ? `Activity, ${data.unread_count} unread results` : 'Activity');
        $('#activity-read').hidden = !data.unread_count;
        if (visible) {
          const previousCount = items.length;
          const merged = new Map(items.map(item => [item.id, item]));
          data.items.forEach(item => merged.set(item.id, item));
          items = [...merged.values()].sort((a, b) => b.started_at.localeCompare(a.started_at));
          if (more || previousCount <= 50) nextBefore = data.next_before;
          paint();
        }
      } catch (error) { if (currentView === 'activity' && requestGeneration === generation) showError(error); }
    })();
    try { await loading; } finally { loading = null; $('#activity-more').disabled = false; }
  }
  function open() {
    stopWatching(); activeJobId = null; clearError();
    $('#activity-feedback').hidden = true;
    items = []; nextBefore = null;
    history.replaceState(null, '', '/#activity'); showView('activity');
    paint(); if (!clearing) refresh(); closeMobileSidebar(); window.scrollTo(0, 0);
  }
  $('#nav-activity').addEventListener('click', open);
  $('#activity-more').addEventListener('click', () => { if (!clearing) { $('#activity-more').disabled = true; refresh(true); } });
  document.querySelectorAll('[data-activity-filter]').forEach(button => button.addEventListener('click', () => {
    if (loading || clearing) return;
    filter = button.dataset.activityFilter; items = []; nextBefore = null; paint(); refresh();
  }));
  $('#activity-read').addEventListener('click', async () => {
    if (clearing) return;
    try {
      await api('/api/activity/read', {method: 'POST'});
      items.forEach(item => { item.unread = false; }); paint(); await refresh();
    } catch (error) { showError(error); }
  });
  $('#activity-clear').addEventListener('click', async () => {
    if (clearing || !clearable) return;
    clearing = true; generation += 1; clearError(); paint();
    $('#activity-feedback').hidden = true;
    try {
      // Discard an older poll before accepting the server's cleared state.
      await loading;
      const result = await api('/api/activity/clear', {method: 'POST'});
      items = items.filter(item => item.status === 'running' || item.workflow_drafts?.some(draft => draft.status === 'ready'));
      nextBefore = null; clearable = 0; clearedAt = result.cleared_at;
      $('#activity-count').hidden = true;
      $('#nav-activity').setAttribute('aria-label', 'Activity');
      $('#activity-read').hidden = true;
      $('#activity-feedback').textContent = 'Notifications cleared. New activity will appear here.';
      $('#activity-feedback').hidden = false;
      paint();
      await refresh();
    } catch (error) { showError(error); }
    finally { clearing = false; paint(); }
  });
  window.activityUI = {open, refresh};
  setInterval(() => { if (!document.hidden && !clearing) refresh(); }, 3000);
  refresh();
})();
