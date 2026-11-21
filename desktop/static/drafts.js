/* Preview plain email text safely; only an explicit click can send a reply. */
(() => {
  const dialog = $('#draft-dialog');
  let review = null, sending = false, generation = 0, completionTimer = null;
  function resetCompletion() {
    clearTimeout(completionTimer);
    completionTimer = null;
    dialog.classList.remove('draft-complete');
    dialog.setAttribute('aria-labelledby', 'draft-title');
  }
  function celebrate() {
    resetCompletion();
    const colors = ['#ffd800', '#ffaf00', '#ff8205', '#fa500f', '#e10500'];
    const particles = Array.from({length: 40}, (_, index) => {
      const angle = Math.PI * 2 * index / 40 + Math.random() * .2;
      const distance = 65 + Math.random() * 75;
      const x = Math.cos(angle) * distance;
      const y = Math.sin(angle) * distance + 24;
      return `<span class="reply-particle" style="--x:${x.toFixed(1)}px;--y:${y.toFixed(1)}px;--turn:${Math.round(Math.random() * 540 - 270)}deg;--size:${4 + Math.round(Math.random() * 4)}px;--delay:${Math.round(Math.random() * 100)}ms;--duration:${1100 + Math.round(Math.random() * 650)}ms;--color:${colors[index % colors.length]}"></span>`;
    }).join('');
    dialog.classList.add('draft-complete');
    dialog.setAttribute('aria-labelledby', 'draft-complete-title');
    $('#draft-error').hidden = true;
    $('#draft-preview').innerHTML = `<div class="reply-celebration">
      <div class="reply-burst" aria-hidden="true">${particles}<img class="reply-mascot" src="/static/brand/mark-mistral.svg" alt="" width="100" height="100"></div>
      <h2 id="draft-complete-title">Done!</h2><p>Reply sent to Mail</p>
    </div>`;
    dialog.focus();
    completionTimer = setTimeout(close, 2800);
  }
  function error(message) {
    $('#draft-error').textContent = message;
    $('#draft-error').hidden = false;
  }
  const field = (label, value) => value ? `<div class="email-field"><span>${label}</span><span>${esc(value)}</span></div>` : '';
  function paint() {
    const send = $('#draft-send');
    send.disabled = true;
    if (!review) return;
    if (review.status === 'sent') { celebrate(); return; }
    if (review.status !== 'ready') {
      $('#draft-title').textContent = 'Check this reply in Mail';
      $('#draft-preview').innerHTML = '<p class="draft-status">Sending has already been requested. Check Sent and Outbox before taking further action.</p>';
      $('#draft-help').textContent = '';
      send.hidden = true;
      $('#draft-keep').textContent = 'Done';
      return;
    }
    const {draft, original} = review;
    $('#draft-title').textContent = original.subject || draft.subject || 'Email preview';
    $('#draft-preview').innerHTML = `<div class="email-preview-grid">
      <section class="email-original"><h3>Original email</h3><div class="email-envelope">${field('From', original.sender)}${field('Subject', original.subject)}</div><div class="email-body">${esc(original.body)}</div></section>
      <section class="email-reply"><h3>Reply draft</h3><div class="email-envelope">${field('From', draft.sender)}${field('To', draft.to.join(', '))}${field('Cc', draft.cc.join(', '))}${field('Bcc', draft.bcc.join(', '))}${field('Subject', draft.subject)}</div><div class="email-body">${esc(draft.body)}</div></section>
    </div>`;
    if (draft.attachments) $('#draft-help').textContent = 'This draft has attachments. Send it from Mail to preserve them.';
    send.disabled = !!draft.attachments || !draft.to.length;
  }
  async function open(id) {
    if (sending) return;
    const current = ++generation;
    resetCompletion();
    review = null;
    $('#draft-title').textContent = 'Email preview';
    $('#draft-error').hidden = true;
    $('#draft-preview').innerHTML = '<p class="draft-status">Loading email from Mail…</p>';
    $('#draft-help').textContent = 'Your reply stays in Drafts until you send it.';
    $('#draft-send').hidden = false; $('#draft-send').disabled = true; $('#draft-send').textContent = 'Send reply';
    $('#draft-keep').textContent = 'Keep draft';
    if (!dialog.open) dialog.showModal();
    try {
      const data = await api(`/api/drafts/${encodeURIComponent(id)}`);
      if (current !== generation || !dialog.open) return;
      review = data; paint();
    } catch (problem) {
      if (current !== generation || !dialog.open) return;
      $('#draft-preview').innerHTML = '';
      error(problem.message || String(problem));
    }
  }
  function close() { if (!sending) { generation++; dialog.close(); review = null; } }
  $('#draft-close').addEventListener('click', close);
  $('#draft-keep').addEventListener('click', close);
  dialog.addEventListener('close', resetCompletion);
  dialog.addEventListener('click', () => { if (dialog.classList.contains('draft-complete')) close(); });
  dialog.addEventListener('cancel', event => { if (sending) event.preventDefault(); else { generation++; review = null; } });
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-review-draft]');
    if (button) open(button.dataset.reviewDraft);
  });
  $('#draft-send').addEventListener('click', async () => {
    if (sending || !review?.draft || review.status !== 'ready') return;
    sending = true;
    $('#draft-error').hidden = true;
    $('#draft-send').disabled = true; $('#draft-send').textContent = 'Sending…';
    $('#draft-keep').disabled = true; $('#draft-close').disabled = true;
    try {
      const result = await api(`/api/drafts/${encodeURIComponent(review.id)}/send`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({revision: review.draft.revision, confirmed: true})
      });
      review.status = result.status; paint();
      window.activityUI?.refresh();
    } catch (problem) {
      error(problem.message || String(problem));
      $('#draft-help').textContent = 'Close and reopen this preview to check the latest status.';
      $('#draft-send').textContent = 'Send reply';
    } finally {
      sending = false; $('#draft-keep').disabled = false; $('#draft-close').disabled = false;
    }
  });
})();
