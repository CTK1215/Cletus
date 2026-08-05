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

function setState(next) {
  if (!STATES.includes(next) || next === currentState) return;
  document.body.classList.remove('state-' + currentState);
  document.body.classList.add('state-' + next);
  currentState = next;

  document.getElementById('state-label').textContent = next.toUpperCase();

  document.querySelectorAll('#controls button[data-state]').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.state === next);
  });

  console.log('[state]', next);
}

function setVoiceStatus(online) {
  const el = document.getElementById('voice-status');
  el.classList.toggle('online', online);
  el.classList.toggle('offline', !online);
  el.title = online ? 'Voice service online' : 'Voice service offline';
}

function showDialog() {
  document.getElementById('dialog').classList.add('show');
  if (dialogHideTimer) clearTimeout(dialogHideTimer);
  dialogHideTimer = setTimeout(hideDialog, DIALOG_HIDE_MS);
}

function hideDialog() {
  document.getElementById('dialog').classList.remove('show');
  document.getElementById('you-said').classList.add('hidden');
  document.getElementById('cletus-said').classList.add('hidden');
  dialogHideTimer = null;
}

function showUserLine(text) {
  const el = document.getElementById('you-said');
  el.textContent = text;
  el.classList.remove('hidden');
  document.getElementById('cletus-said').classList.add('hidden');
  showDialog();
}

function showCletusLine(text) {
  const el = document.getElementById('cletus-said');
  el.textContent = text;
  el.classList.remove('hidden', 'filler');
  showDialog();
}

// "Let me look." spoken while the brain is off using tools. Styled as
// tentative so it doesn't read as the actual answer.
function showFillerLine(text) {
  const el = document.getElementById('cletus-said');
  el.textContent = text;
  el.classList.remove('hidden');
  el.classList.add('filler');
  showDialog();
}

let followupTimer = null;

function openFollowup(seconds) {
  const el = document.getElementById('followup');
  const count = document.getElementById('followup-count');
  el.classList.remove('hidden');
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
  document.getElementById('followup').classList.add('hidden');
  document.getElementById('followup-count').textContent = '';
  document.body.classList.remove('followup-open');
}

function scheduleReturnToIdle(delay = RETURN_TO_IDLE_MS) {
  if (idleTimer) clearTimeout(idleTimer);
  idleTimer = setTimeout(() => {
    setState('idle');
    idleTimer = null;
  }, delay);
}

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
  switch (msg.event) {
    case 'connected':
      console.log('[voice] service v' + msg.version + ' ready  wake=' + msg.wake_word);
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
    // only place Chris can see that something is still happening. Without
    // these, a twenty-minute build looks identical to an idle room.

    case 'job-started':
      console.log('[voice] job started:', msg.id, msg.project, msg.request);
      addJob(msg.id, msg.projectSpoken || msg.project);
      break;

    case 'job-progress':
      updateJob(msg.id, `${msg.toolCalls} steps · ${formatElapsed(msg.elapsed)}`);
      break;

    case 'job-done':
      console.log('[voice] job done:', msg.id, msg.ok ? 'ok' : 'failed');
      finishJob(msg.id, msg.ok, formatElapsed(msg.elapsed));
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
// Job tray
//
// Dispatched work runs as a detached task, so nothing in the room changes
// while it happens: no speech, no state flip, no waveform. This tray is the
// only signal that Cletus is busy rather than asleep, which matters most
// exactly when a job is taking longer than expected.
//
// Finished jobs linger briefly rather than vanishing, so a result that lands
// while Chris is out of the room is still there when he walks back in.

const JOB_LINGER_MS = 45000;

function jobTray() {
  let el = document.getElementById('job-tray');
  if (!el) {
    el = document.createElement('div');
    el.id = 'job-tray';
    document.body.appendChild(el);
  }
  return el;
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
  const row = document.getElementById(`job-${id}`);
  if (row) row.querySelector('.job-meta').textContent = meta;
}

function finishJob(id, ok, elapsed) {
  const row = document.getElementById(`job-${id}`);
  if (!row) return;
  row.classList.remove('job-running');
  row.classList.add(ok ? 'job-ok' : 'job-failed');
  row.querySelector('.job-meta').textContent = `${ok ? 'done' : 'failed'} · ${elapsed}`;
  setTimeout(() => row.remove(), JOB_LINGER_MS);
}

// Kelly.dev focus system: highlights the monitor + traces of the currently-active project.
// Wire via WebSocket later (voice sends {event:'focus', project:'nursetrack'}).
// For now, F1-F4 flip focus manually; Escape clears.
const FOCUS_KEYS = {
  F1: 'nursetrack',
  F2: 'unshackled',
  F3: 'wendell',
  F4: 'cletus',
};

function setFocus(project) {
  if (project) {
    document.body.setAttribute('data-focus', project);
    console.log('[focus]', project);
  } else {
    document.body.removeAttribute('data-focus');
    console.log('[focus] cleared');
  }
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('#controls button[data-state]').forEach(btn => {
    btn.addEventListener('click', () => setState(btn.dataset.state));
  });

  document.getElementById('close-btn').addEventListener('click', () => {
    window.cletus.close();
  });

  document.addEventListener('keydown', (e) => {
    if (FOCUS_KEYS[e.key]) {
      e.preventDefault();
      setFocus(FOCUS_KEYS[e.key]);
    } else if (e.key === 'Escape') {
      setFocus(null);
    }
  });

  console.log('Cletus HUD v' + window.cletus.version + ' - phase 5 online');
  connectVoice();
});
