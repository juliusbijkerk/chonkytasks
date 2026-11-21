/* Capture only after Start is clicked; hand the completed video to the existing pipeline. */
(() => {
  const native = window.webkit?.messageHandlers?.screenRecording;
  const bridge = (action, extra = {}) => native.postMessage({action, ...extra});
  let state = {status: 'idle'}, processing = false, polling = false;
  let stream = null, recorder = null, chunks = [], bytes = 0, blob = null, browserError = '';
  const handled = new Set(), accepted = new Set();
  const busy = () => ['selecting', 'starting', 'recording', 'stopping', 'submitting'].includes(state.status);
  const elapsed = () => state.started_at ? Math.max(0, Math.floor((Date.now() - Date.parse(state.started_at)) / 1000)) : 0;

  function paint() {
    const phase = state.status, recording = phase === 'recording';
    const waiting = ['selecting', 'starting', 'stopping', 'submitting'].includes(phase);
    $('#capture-card').dataset.state = phase;
    $('#capture-title').textContent = recording ? 'Recording your workflow' : phase === 'ready' ? 'Your recording is saved' : phase === 'stopping' ? 'Finishing your recording' : phase === 'submitting' ? 'Preparing your analysis' : 'Record your workflow';
    $('#capture-description').textContent = recording ? 'Work as usual. Come back here when you’re done.' : phase === 'ready' ? 'Your video is ready to analyze.' : 'Choose a screen or window, then work as usual.';
    $('#capture-status').textContent = state.error || (phase === 'selecting' ? 'Choose what to record in the screen picker.' : phase === 'starting' ? 'Starting your recording…' : phase === 'stopping' ? 'Saving your video…' : phase === 'submitting' ? 'Starting the analysis…' : recording ? 'Screen recording is active. Audio is off.' : phase === 'ready' ? 'Select Process recording to try again.' : 'Your recording will be analyzed when you stop.');
    $('#capture-clock').hidden = !recording && phase !== 'stopping';
    $('#capture-time').textContent = stamp(elapsed());
    $('#capture-button-label').textContent = recording ? 'Stop & analyze' : phase === 'ready' ? 'Process recording' : phase === 'selecting' ? 'Choose a screen…' : phase === 'starting' ? 'Starting…' : phase === 'stopping' ? 'Saving…' : phase === 'submitting' ? 'Preparing…' : 'Start recording';
    $('#capture-button').disabled = waiting || processing;
    $('#context').disabled = busy() || phase === 'ready';
    if (state.context !== undefined && phase !== 'idle') $('#context').value = state.context;
    $('#capture-quick-stop').hidden = !['recording', 'stopping'].includes(phase);
    $('#capture-quick-stop').disabled = !recording;
    $('#capture-quick-label').textContent = recording ? `Stop · ${stamp(elapsed())}` : 'Saving recording…';
  }
  function setState(value) { state = value; paint(); }
  function failure(problem) {
    setState({status: 'error', error: problem.message || String(problem)});
  }

  async function processRecording() {
    if (processing || state.status !== 'ready') return;
    const saved = {...state};
    processing = true;
    setState({...saved, status: 'submitting', error: ''});
    const form = new FormData();
    form.append('context', saved.context || '');
    let endpoint = '/api/jobs';
    if (native) {
      endpoint = '/api/jobs/from-inbox';
      form.append('name', saved.name); form.append('filename', saved.filename);
    } else {
      form.append('file', blob, saved.filename);
      form.append('capture_id', saved.capture_id);
    }
    try {
      const job = await submit(endpoint, form);
      if (!job) {
        setState({...saved, error: 'Your recording is kept. Try processing it again when the app is ready.'});
        return;
      }
      if (native) {
        accepted.add(saved.name);
        try { await bridge('acknowledge', {name: saved.name}); } catch { /* Polling retries only the local acknowledgement. */ }
      }
      blob = null; chunks = []; bytes = 0;
      $('#context').value = '';
      setState({status: 'idle'});
    } finally { processing = false; paint(); }
  }

  async function nativeState(value) {
    if (processing) return;
    if (value.status === 'ready' && accepted.has(value.name)) {
      try { await bridge('acknowledge', {name: value.name}); } catch { /* Keep the saved token for a later retry. */ }
      return;
    }
    setState(value);
    if (value.status === 'ready' && !handled.has(value.name)) {
      handled.add(value.name);
      await processRecording();
    }
  }

  async function start() {
    if (busy() || processing) return;
    clearError();
    const context = $('#context').value;
    setState({status: 'selecting', context});
    if (native) {
      try { await nativeState(await bridge('start', {context})); }
      catch (problem) { failure(problem); }
      return;
    }
    if (!navigator.mediaDevices?.getDisplayMedia || !window.MediaRecorder) {
      failure(new Error('Open chonkytasks on your Mac, or use a desktop browser that supports screen recording.'));
      return;
    }
    try {
      // Call directly from the button gesture so the browser can show its picker.
      stream = await navigator.mediaDevices.getDisplayMedia({video: {frameRate: {ideal: 15, max: 15}, width: {max: 1920}, height: {max: 1920}}, audio: false});
      const mimeType = ['video/mp4;codecs=avc1.42E01E', 'video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm'].find(type => MediaRecorder.isTypeSupported(type));
      recorder = new MediaRecorder(stream, {...(mimeType ? {mimeType} : {}), videoBitsPerSecond: 3000000});
      chunks = []; bytes = 0; browserError = '';
      const extension = recorder.mimeType.includes('mp4') ? 'mp4' : 'webm';
      const saved = {context, filename: `Recording ${new Date().toISOString().slice(0, 19).replace(/[T:]/g, '-')}.${extension}`, capture_id: crypto.randomUUID().replaceAll('-', '')};
      recorder.ondataavailable = event => {
        if (event.data.size) { chunks.push(event.data); bytes += event.data.size; }
        if (bytes >= 450 * 1024 * 1024 && recorder?.state === 'recording') stop();
      };
      recorder.onerror = () => { browserError = 'Screen recording was interrupted. Your captured video is kept; you can try processing it.'; stop(); };
      recorder.onstop = async () => {
        const type = recorder.mimeType;
        stream?.getTracks().forEach(track => track.stop());
        stream = null; recorder = null;
        blob = new Blob(chunks, {type}); chunks = [];
        if (!blob.size) { failure(new Error('No video was recorded. Try recording for a few seconds.')); return; }
        setState({...saved, status: 'ready', error: browserError});
        if (!browserError) await processRecording();
      };
      stream.getVideoTracks()[0].addEventListener('ended', stop, {once: true});
      recorder.start(1000);
      setState({...saved, status: 'recording', started_at: new Date().toISOString()});
    } catch (problem) {
      stream?.getTracks().forEach(track => track.stop()); stream = null; recorder = null;
      if (['NotAllowedError', 'AbortError'].includes(problem.name)) setState({status: 'idle', error: 'Recording wasn’t started. Select Start recording to choose a screen again.'});
      else failure(problem);
    }
  }
  async function stop() {
    if (state.status !== 'recording') return;
    const previous = state;
    setState({...state, status: 'stopping'});
    if (native) {
      try { await nativeState(await bridge('stop')); }
      catch (problem) { setState({...previous, error: problem.message || String(problem)}); }
    } else if (recorder?.state !== 'inactive') {
      recorder.stop();
    }
  }
  $('#capture-button').addEventListener('click', () => state.status === 'recording' ? stop() : state.status === 'ready' ? processRecording() : start());
  $('#capture-quick-stop').addEventListener('click', stop);
  window.addEventListener('beforeunload', event => {
    if (!native && (busy() || blob)) { event.preventDefault(); event.returnValue = ''; }
  });
  window.recordingUI = {nativeState, stop};
  async function pollNative() {
    if (!native || polling || processing || document.hidden) return;
    polling = true;
    try { await nativeState(await bridge('status')); }
    catch (problem) { if (state.status === 'idle') failure(problem); }
    finally { polling = false; }
  }
  setInterval(() => {
    if (state.status === 'recording') {
      $('#capture-time').textContent = stamp(elapsed());
      $('#capture-quick-label').textContent = `Stop · ${stamp(elapsed())}`;
      if (!native && elapsed() >= 895) stop();
    }
    pollNative();
  }, 1000);
  paint(); pollNative();
})();
