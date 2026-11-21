/* Native ScreenCaptureKit controls. Browser uploads remain available independently. */
(() => {
  const handler = window.webkit?.messageHandlers?.chonkytasksCapture;
  if (!handler || window.chonkytasksCaptureAvailable === false) return;

  const byId = id => document.getElementById(id);
  const home = byId('capture-home');
  const start = byId('open-capture');
  const sources = byId('capture-sources');
  const display = byId('capture-display');
  const active = byId('active-capture');
  const stop = byId('stop-capture');
  const statusText = byId('capture-status');
  const indicator = byId('capture-indicator');
  const activeText = {starting: 'Starting…', recording: 'Recording', stopping: 'Saving…'};

  function showSources(visible) {
    sources.hidden = !visible;
    start.setAttribute('aria-expanded', String(visible));
  }

  window.chonkytasksCaptureState = ({status, message = ''}) => {
    const capturing = Object.hasOwn(activeText, status);
    home.hidden = capturing;
    active.hidden = !capturing;
    active.dataset.state = status;
    indicator.textContent = capturing ? activeText[status] : '';
    stop.hidden = status === 'starting';
    stop.disabled = status !== 'recording';
    start.disabled = status === 'selecting';
    if (status !== 'ready') showSources(false);
    if (status === 'error' && message) showError(new Error(message));
    const pickerHint = status === 'selecting' ? 'Choose what to record in the macOS picker.' : '';
    statusText.textContent = status === 'error' ? '' : message || pickerHint;
    statusText.hidden = !statusText.textContent;
  };

  function requestCapture(action) {
    window.chonkytasksCaptureState(action === 'stop' ? {status: 'stopping'} :
      {status: 'selecting', message: 'Choose what to record in the macOS picker.'});
    try { handler.postMessage(action); }
    catch { window.chonkytasksCaptureState({status: 'error', message: 'Could not open the macOS recording controls.'}); }
  }

  start.addEventListener('click', () => {
    clearError();
    const opening = sources.hidden;
    showSources(opening);
    if (opening) display.focus();
  });
  home.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || sources.hidden) return;
    event.preventDefault();
    showSources(false);
    start.focus();
  });
  display.addEventListener('click', () => requestCapture('display'));
  byId('capture-window').addEventListener('click', () => requestCapture('window'));
  stop.addEventListener('click', () => requestCapture('stop'));
  home.hidden = false;
})();
