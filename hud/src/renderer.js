const STATES = ['idle', 'listening', 'thinking', 'speaking'];
const VOICE_URL = 'ws://127.0.0.1:8765';
const RECONNECT_MS = 2000;
const DIALOG_HIDE_MS = 10000;
const RETURN_TO_IDLE_MS = 4000;

let currentState = 'idle';
let ws = null;
let reconnectTimer = null;
let dialogHideTimer = null;
let idleTimer = null;

// What the core says under the name, per state. The name itself belongs to
// whatever project has focus; these are just the room's verbs.
const STATUS_LINE = {
  idle: 'STANDING BY',
  listening: 'LISTENING',
  thinking: 'PROCESSING',
  speaking: 'SPEAKING',
};

function el(id) { return document.getElementById(id); }
function setText(id, text) { const n = el(id); if (n) n.textContent = text; }

function setState(next) {
  if (!STATES.includes(next) || next === currentState) return;
  document.body.classList.remove('state-' + currentState);
  document.body.classList.add('state-' + next);
  currentState = next;

  setText('state-label', next.toUpperCase());
  setText('core-status', STATUS_LINE[next] || next.toUpperCase());
  setText('p-state', next.toUpperCase());

  document.querySelectorAll('#controls button[data-state]').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.state === next);
  });

  sound.onState(next);
  console.log('[state]', next);
}

function setVoiceStatus(online) {
  const node = el('voice-status');
  node.classList.toggle('online', online);
  node.classList.toggle('offline', !online);
  node.title = online ? 'Voice service online' : 'Voice service offline';

  setText('p-link', online ? 'ONLINE' : 'OFFLINE');
  const dot = el('p-core-dot');
  if (dot) dot.setAttribute('class', online ? 'p-dot p-dot-on' : 'p-dot p-dot-off');
  if (!online) stopUptime();
}

function showDialog() {
  el('dialog').classList.add('show');
  if (dialogHideTimer) clearTimeout(dialogHideTimer);
  dialogHideTimer = setTimeout(hideDialog, DIALOG_HIDE_MS);
}

function hideDialog() {
  el('dialog').classList.remove('show');
  el('you-said').classList.add('hidden');
  el('cletus-said').classList.add('hidden');
  dialogHideTimer = null;
}

function showUserLine(text) {
  const node = el('you-said');
  node.textContent = text;
  node.classList.remove('hidden');
  el('cletus-said').classList.add('hidden');
  showDialog();
}

function showCletusLine(text) {
  const node = el('cletus-said');
  node.textContent = text;
  node.classList.remove('hidden', 'filler');
  showDialog();
}

// "Let me look." spoken while the brain is off using tools. Styled as
// tentative so it doesn't read as the actual answer.
function showFillerLine(text) {
  const node = el('cletus-said');
  node.textContent = text;
  node.classList.remove('hidden');
  node.classList.add('filler');
  showDialog();
}

let followupTimer = null;

function openFollowup(seconds) {
  const node = el('followup');
  const count = el('followup-count');
  node.classList.remove('hidden');
  document.body.classList.add('followup-open');

  let left = Math.ceil(seconds || 0);
  count.textContent = left > 0 ? left : '';

  if (followupTimer) clearInterval(followupTimer);
  followupTimer = setInterval(() => {
    left -= 1;
    if (left <= 0) {
      closeFollowup();
      return;
    }
    count.textContent = left;
  }, 1000);
}

function closeFollowup() {
  if (followupTimer) {
    clearInterval(followupTimer);
    followupTimer = null;
  }
  el('followup').classList.add('hidden');
  el('followup-count').textContent = '';
  document.body.classList.remove('followup-open');
}

function scheduleReturnToIdle(delay = RETURN_TO_IDLE_MS) {
  if (idleTimer) clearTimeout(idleTimer);
  idleTimer = setTimeout(() => {
    setState('idle');
    idleTimer = null;
  }, delay);
}

// ---------------------------------------------------------------------------
// Live panels: the right column shows only what the service actually reports.
// No number appears here that didn't arrive over the socket.

let connectedAt = null;
let uptimeTimer = null;
let exchanges = 0;

function startUptime() {
  connectedAt = Date.now();
  if (uptimeTimer) clearInterval(uptimeTimer);
  uptimeTimer = setInterval(() => {
    const s = Math.floor((Date.now() - connectedAt) / 1000);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    setText('p-uptime', h > 0 ? `${h}h ${m}m` : `${m}m ${s % 60}s`);
  }, 1000);
}

function stopUptime() {
  if (uptimeTimer) { clearInterval(uptimeTimer); uptimeTimer = null; }
  setText('p-uptime', '—');
}

function clockNow() {
  const d = new Date();
  return d.toTimeString().slice(0, 5);
}

// ---------------------------------------------------------------------------
// Focus: the center of the room belongs to whatever is being worked on.
// A dispatched job claims it automatically; F1-F4 override; Escape clears.
// Dispatcher project keys collapse into four color families.

const FOCUS_FAMILY = {
  nursetrack: 'nursetrack', admin: 'nursetrack', api: 'nursetrack', 'servesync-app': 'nursetrack',
  unshackled: 'unshackled',
  wendell: 'wendell', senior: 'wendell',
  cletus: 'cletus', vault: 'cletus', kellybuilt: 'cletus',
};

const FOCUS_NAME = {
  nursetrack: 'NURSETRACK', admin: 'NT ADMIN', api: 'SERVESYNC', 'servesync-app': 'SERVESYNC',
  unshackled: 'UNSHACKLED', wendell: 'WENDELL', senior: 'PASS AREA',
  cletus: 'CLETUS', vault: 'THE VAULT', kellybuilt: 'KELLYBUILT',
};

function setFocus(projectKey) {
  if (projectKey && FOCUS_FAMILY[projectKey]) {
    document.body.setAttribute('data-focus', FOCUS_FAMILY[projectKey]);
    setText('core-name', FOCUS_NAME[projectKey] || projectKey.toUpperCase());
    setText('core-eyebrow', 'FOCUS');
    setText('p-focus', FOCUS_NAME[projectKey] || projectKey.toUpperCase());
    console.log('[focus]', projectKey);
  } else {
    document.body.removeAttribute('data-focus');
    setText('core-name', 'CLETUS');
    setText('core-eyebrow', 'READY');
    setText('p-focus', 'NONE');
    console.log('[focus] cleared');
  }
  updateTicker();
}

// ---------------------------------------------------------------------------
// Sound: synthesized on the fly, no audio files. Deliberately quiet; the
// microphone across the desk must never mistake the room for a voice.
// M toggles mute and the choice persists.

const sound = (() => {
  let ctx = null;
  let master = null;
  let chatterTimer = null;
  let airFilter = null;   // the room's breath; thought opens it up
  let bedGain = null;
  let muted = localStorage.getItem('hud-muted') === '1';

  function ensure() {
    if (ctx) return true;
    try {
      ctx = new (window.AudioContext || window.webkitAudioContext)();
      master = ctx.createGain();
      master.gain.value = muted ? 0 : 0.06;
      master.connect(ctx.destination);

      // Ambient bed: filtered noise "air" over a pair of detuned low
      // triangles. Very quiet, but the room is never dead silent again.
      bedGain = ctx.createGain();
      bedGain.gain.value = 0.16;
      bedGain.connect(master);

      const len = ctx.sampleRate * 2;
      const buf = ctx.createBuffer(1, len, ctx.sampleRate);
      const data = buf.getChannelData(0);
      for (let i = 0; i < len; i++) data[i] = Math.random() * 2 - 1;
      const noise = ctx.createBufferSource();
      noise.buffer = buf;
      noise.loop = true;
      airFilter = ctx.createBiquadFilter();
      airFilter.type = 'lowpass';
      airFilter.frequency.value = 190;
      airFilter.Q.value = 0.7;
      const airGain = ctx.createGain();
      airGain.gain.value = 0.35;
      noise.connect(airFilter).connect(airGain).connect(bedGain);
      noise.start();

      [49, 49.6].forEach(f => {
        const o = ctx.createOscillator();
        o.type = 'triangle';
        o.frequency.value = f;
        const g = ctx.createGain();
        g.gain.value = 0.22;
        o.connect(g).connect(bedGain);
        o.start();
      });

      return true;
    } catch (e) {
      console.warn('[sound] unavailable', e);
      return false;
    }
  }

  // One filtered tone. Everything audible is built from these.
  function tone(freq, dur, delay = 0, type = 'sine', peak = 0.5, glideTo = null) {
    if (!ensure() || muted) return;
    const t = ctx.currentTime + delay;
    const osc = ctx.createOscillator();
    const g = ctx.createGain();
    const lp = ctx.createBiquadFilter();
    lp.type = 'lowpass';
    lp.frequency.value = Math.max(1200, freq * 3);
    osc.type = type;
    osc.frequency.setValueAtTime(freq, t);
    if (glideTo) osc.frequency.exponentialRampToValueAtTime(glideTo, t + dur);
    g.gain.setValueAtTime(0, t);
    g.gain.linearRampToValueAtTime(peak, t + 0.012);
    g.gain.exponentialRampToValueAtTime(0.001, t + dur);
    osc.connect(lp).connect(g).connect(master);
    osc.start(t);
    osc.stop(t + dur + 0.05);
  }

  function arp(notes, step, dur, type = 'sine', peak = 0.4) {
    notes.forEach((f, i) => tone(f, dur, i * step, type, peak));
  }

  function startChatter() {
    if (chatterTimer) return;
    const tick = () => {
      // two bands of machine chatter, occasionally a triple burst
      const base = Math.random() < 0.5 ? 1500 : 2300;
      tone(base + Math.random() * 500, 0.025, 0, 'square', 0.16);
      if (Math.random() < 0.22) {
        tone(base * 0.75, 0.02, 0.05, 'square', 0.12);
        tone(base * 1.2, 0.02, 0.1, 'square', 0.12);
      }
      chatterTimer = setTimeout(tick, 110 + Math.random() * 320);
    };
    tick();
    if (airFilter) airFilter.frequency.linearRampToValueAtTime(520, ctx.currentTime + 0.6);
  }

  function stopChatter() {
    if (chatterTimer) { clearTimeout(chatterTimer); chatterTimer = null; }
    if (airFilter && ctx) airFilter.frequency.linearRampToValueAtTime(190, ctx.currentTime + 1.2);
  }

  function onState(state) {
    if (state === 'thinking') startChatter();
    else stopChatter();
    if (state === 'listening') {
      tone(480, 0.22, 0, 'sine', 0.5, 1050);          // rising sweep: I hear you
      tone(1320, 0.1, 0.16, 'sine', 0.25);            // shimmer on top
    }
    if (state === 'speaking') tone(520, 0.07, 0, 'sine', 0.3);
  }

  function onConnect()  { tone(220, 0.5, 0, 'sine', 0.35, 660); arp([660, 990], 0.12, 0.18, 'sine', 0.3); }
  function onFollowup() { tone(880, 0.06, 0, 'sine', 0.3); tone(1174, 0.09, 0.08, 'sine', 0.3); }

  function onImage()   { arp([784, 988, 1175, 1568], 0.06, 0.12, 'sine', 0.35); }
  function jobStarted() { arp([392, 494, 587, 740], 0.07, 0.08, 'triangle', 0.4); }
  function jobDone(ok) {
    if (ok) arp([523, 659, 784, 1047], 0.09, 0.16, 'sine', 0.42);
    else    arp([330, 262, 196], 0.12, 0.2, 'sawtooth', 0.28);
  }

  function toggleMute() {
    muted = !muted;
    localStorage.setItem('hud-muted', muted ? '1' : '0');
    if (master) master.gain.value = muted ? 0 : 0.06;
    el('mute-badge').classList.toggle('hidden', !muted);
    console.log('[sound]', muted ? 'muted' : 'unmuted');
  }

  function syncBadge() { el('mute-badge').classList.toggle('hidden', !muted); }

  return { onState, onConnect, onFollowup, onImage, jobStarted, jobDone, toggleMute, syncBadge };
})();

// ---------------------------------------------------------------------------

function connectVoice() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

  try {
    ws = new WebSocket(VOICE_URL);
  } catch (err) {
    console.warn('[voice] connect threw', err);
    scheduleReconnect();
    return;
  }

  ws.addEventListener('open', () => {
    console.log('[voice] connected');
    setVoiceStatus(true);
    startUptime();
  });

  ws.addEventListener('message', (evt) => {
    let msg;
    try {
      msg = JSON.parse(evt.data);
    } catch (_) {
      console.warn('[voice] non-json message', evt.data);
      return;
    }
    handleVoiceEvent(msg);
  });

  ws.addEventListener('close', () => {
    console.log('[voice] disconnected');
    setVoiceStatus(false);
    closeFollowup();
    scheduleReconnect();
  });

  ws.addEventListener('error', () => {
    // close event will follow
  });
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectVoice();
  }, RECONNECT_MS);
}

function handleVoiceEvent(msg) {
  flashActivity();
  switch (msg.event) {
    case 'connected':
      console.log('[voice] service v' + msg.version + ' ready  wake=' + msg.wake_word);
      setText('p-version', 'v' + msg.version);
      setText('p-wake', String(msg.wake_word || '—').replace(/_/g, ' ').toUpperCase());
      serviceVersion = 'v' + msg.version;
      wakePhrase = String(msg.wake_word || '').replace(/_/g, ' ').toUpperCase();
      sound.onConnect();
      updateTicker();
      break;

    case 'heartbeat':
      break;

    case 'wake':
      if (idleTimer) { clearTimeout(idleTimer); idleTimer = null; }
      closeFollowup();
      setState('listening');
      break;

    case 'followup-open':
      openFollowup(msg.seconds);
      sound.onFollowup();
      break;

    case 'followup-closed':
      closeFollowup();
      break;

    case 'filler':
      console.log('[voice] filler:', msg.text);
      showFillerLine(msg.text);
      break;

    case 'transcribing':
      setState('thinking');
      break;

    case 'transcript':
      console.log('[voice] transcript:', msg.text);
      if (msg.text) {
        showUserLine(msg.text);
        setText('p-last', clockNow());
        setState('thinking');
      } else {
        console.log('[voice] empty transcript', msg.note || '');
        scheduleReturnToIdle(1000);
      }
      break;

    case 'brain-thinking':
      setState('thinking');
      break;

    case 'reply':
      console.log('[voice] reply:', msg.text);
      showCletusLine(msg.text);
      exchanges += 1;
      setText('p-exchanges', String(exchanges));
      // state flip happens on speaking-start; keep a safety timer in case tts is offline
      scheduleReturnToIdle(12000);
      break;

    case 'speaking-start':
      if (idleTimer) { clearTimeout(idleTimer); idleTimer = null; }
      setState('speaking');
      break;

    case 'speaking-end':
      scheduleReturnToIdle(800);
      break;

    // ---- Background jobs ------------------------------------------------
    // A dispatched job runs detached from the audio loop, so the HUD is the
    // only place Chris can see that something is still happening. A job also
    // claims the room's focus: the center names the project being worked.

    case 'job-started':
      console.log('[voice] job started:', msg.id, msg.project, msg.request);
      addJob(msg.id, msg.projectSpoken || msg.project);
      setFocus(msg.project);
      jobs.running += 1;
      renderJobCounts();
      sound.jobStarted();
      break;

    case 'job-progress':
      updateJob(msg.id, `${msg.toolCalls} steps · ${formatElapsed(msg.elapsed)}`);
      break;

    case 'job-done':
      console.log('[voice] job done:', msg.id, msg.ok ? 'ok' : 'failed');
      finishJob(msg.id, msg.ok, formatElapsed(msg.elapsed));
      jobs.running = Math.max(0, jobs.running - 1);
      if (msg.ok) jobs.done += 1; else jobs.failed += 1;
      renderJobCounts();
      sound.jobDone(!!msg.ok);
      break;

    // Phase B: a live uptime probe reported in. The value is real latency
    // against the real production site, or a real DOWN.
    case 'site-status': {
      const node = el(msg.key);
      if (node) {
        node.textContent = msg.up ? `UP·${msg.ms}ms` : 'DOWN';
        node.setAttribute('class', msg.up ? 's-val up' : 's-val down');
      }
      break;
    }

    // A generated image landed in the watched folder: it takes the main
    // screen, framed and captioned, until dismissed or replaced.
    case 'show-image':
      console.log('[voice] image:', msg.name);
      showImage(msg.name, msg.data);
      break;

    case 'error':
      console.error('[voice] error:', msg.message);
      scheduleReturnToIdle(1000);
      break;

    default:
      console.log('[voice] event:', msg);
  }
}

// ---------------------------------------------------------------------------
// Job tray + live job counters
//
// Finished jobs linger briefly rather than vanishing, so a result that lands
// while Chris is out of the room is still there when he walks back in.

const JOB_LINGER_MS = 45000;
const jobs = { running: 0, done: 0, failed: 0 };

function renderJobCounts() {
  setText('p-jobs-run', String(jobs.running));
  setText('p-jobs-done', String(jobs.done));
  setText('p-jobs-fail', String(jobs.failed));
  const dot = el('p-jobs-dot');
  if (dot) dot.setAttribute('class', jobs.running > 0 ? 'p-dot p-dot-run' : 'p-dot p-dot-off');
  updateTicker();
}

function jobTray() {
  let node = el('job-tray');
  if (!node) {
    node = document.createElement('div');
    node.id = 'job-tray';
    document.body.appendChild(node);
  }
  return node;
}

function formatElapsed(seconds) {
  const s = Number(seconds) || 0;
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return `${m}m`;
}

function addJob(id, label) {
  const row = document.createElement('div');
  row.className = 'job-row job-running';
  row.id = `job-${id}`;
  row.innerHTML =
    `<span class="job-dot"></span>` +
    `<span class="job-label"></span>` +
    `<span class="job-meta">starting…</span>`;
  row.querySelector('.job-label').textContent = label;
  jobTray().appendChild(row);
}

function updateJob(id, meta) {
  const row = el(`job-${id}`);
  if (row) row.querySelector('.job-meta').textContent = meta;
}

function finishJob(id, ok, elapsed) {
  const row = el(`job-${id}`);
  if (!row) return;
  row.classList.remove('job-running');
  row.classList.add(ok ? 'job-ok' : 'job-failed');
  row.querySelector('.job-meta').textContent = `${ok ? 'done' : 'failed'} · ${elapsed}`;
  setTimeout(() => row.remove(), JOB_LINGER_MS);
}

// ---------------------------------------------------------------------------
// Image screen: a generated picture takes over the main display. Escape
// clears it; a fresh image replaces it; it steps down on its own after a
// while so the room comes back.

const IMAGE_LINGER_MS = 90000;
let imageTimer = null;

function showImage(name, dataUri) {
  const wrap = el('image-screen');
  const img = el('image-el');
  if (!wrap || !img || !dataUri) return;
  img.src = dataUri;
  setText('image-caption', (name || 'IMAGE').toUpperCase());
  wrap.classList.remove('hidden');
  // retrigger the power-on animation
  wrap.style.animation = 'none';
  void wrap.offsetWidth;
  wrap.style.animation = '';
  sound.onImage();
  if (imageTimer) clearTimeout(imageTimer);
  imageTimer = setTimeout(hideImage, IMAGE_LINGER_MS);
}

function hideImage() {
  const wrap = el('image-screen');
  if (wrap) wrap.classList.add('hidden');
  const img = el('image-el');
  if (img) img.src = '';
  if (imageTimer) { clearTimeout(imageTimer); imageTimer = null; }
}

// ---------------------------------------------------------------------------
// Deco layer: everything on the top strip and the ticker is real. Clock and
// date are the machine's, FPS is measured off requestAnimationFrame, memory
// is the renderer's own heap, and the activity dot lights on socket traffic.

let serviceVersion = '—';
let wakePhrase = '—';
let activityTimer = null;

function flashActivity() {
  const dot = el('ts-activity');
  if (!dot) return;
  dot.classList.add('hot');
  if (activityTimer) clearTimeout(activityTimer);
  activityTimer = setTimeout(() => dot.classList.remove('hot'), 140);
}

function updateTicker() {
  const focus = document.body.getAttribute('data-focus');
  const parts = [
    `CLETUS OS ${serviceVersion}`,
    `WAKE PHRASE ${wakePhrase}`,
    `FOCUS ${focus ? focus.toUpperCase() : 'NONE'}`,
    `EXCHANGES ${exchanges}`,
    `JOBS ${jobs.running} RUN / ${jobs.done} DONE / ${jobs.failed} FAILED`,
    `SAY A PROJECT AND A VERB TO DISPATCH WORK`,
  ];
  setText('ticker-text', parts.join('      ▪      '));
}

function initDeco() {
  // clock + date, straight off the machine
  const tick = () => {
    const d = new Date();
    setText('ts-clock', d.toTimeString().slice(0, 8));
    setText('ts-date', d.toDateString().toUpperCase().slice(0, 10));
  };
  tick();
  setInterval(tick, 1000);

  // FPS, measured for real
  let frames = 0;
  let lastStamp = performance.now();
  const loop = (now) => {
    frames += 1;
    if (now - lastStamp >= 500) {
      setText('ts-fps', Math.round((frames * 1000) / (now - lastStamp)) + ' FPS');
      frames = 0;
      lastStamp = now;
    }
    requestAnimationFrame(loop);
  };
  requestAnimationFrame(loop);

  // renderer heap, when the runtime exposes it
  if (performance.memory) {
    const mem = () => setText('ts-mem', Math.round(performance.memory.usedJSHeapSize / 1048576) + ' MB');
    mem();
    setInterval(mem, 2000);
  } else {
    setText('ts-mem', '—');
  }

  // hex rain: texture only, duplicated once so the CSS loop is seamless
  const inner = el('hexcol-inner');
  if (inner) {
    let lines = '';
    for (let i = 0; i < 60; i++) {
      lines += Math.floor(Math.random() * 256).toString(16).padStart(2, '0').toUpperCase() + '\n';
    }
    inner.textContent = lines + lines;
  }

  updateTicker();
  setInterval(updateTicker, 6000);
}

// F1-F4 remain the manual override for the four color families; Escape clears.
const FOCUS_KEYS = {
  F1: 'nursetrack',
  F2: 'unshackled',
  F3: 'wendell',
  F4: 'cletus',
};

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('#controls button[data-state]').forEach(btn => {
    btn.addEventListener('click', () => setState(btn.dataset.state));
  });

  el('close-btn').addEventListener('click', () => {
    if (window.cletus) window.cletus.close();
  });

  // Typed input rides the same socket the room listens on.
  const typeBar = el('type-bar');
  const typeInput = el('type-input');
  typeBar.addEventListener('submit', (e) => {
    e.preventDefault();
    const text = (typeInput.value || '').trim();
    if (!text) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      console.warn('[type] voice service offline, text not sent');
      return;
    }
    ws.send(JSON.stringify({ event: 'user-text', text }));
    typeInput.value = '';
  });
  typeInput.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') typeInput.blur();
    e.stopPropagation();  // typing must never trip the room's shortcuts
  });

  document.addEventListener('keydown', (e) => {
    if (e.target === typeInput) return;
    if (FOCUS_KEYS[e.key]) {
      e.preventDefault();
      setFocus(FOCUS_KEYS[e.key]);
    } else if (e.key === 'Escape') {
      if (!el('image-screen').classList.contains('hidden')) hideImage();
      else setFocus(null);
    } else if (e.key === 'm' || e.key === 'M') {
      sound.toggleMute();
    }
  });

  sound.syncBadge();
  renderJobCounts();
  initDeco();

  const v = window.cletus ? window.cletus.version : 'dev';
  console.log('Cletus HUD v' + v + ' - reactor room online');
  connectVoice();
});
