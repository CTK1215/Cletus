"""Cletus Voice Service."""

import asyncio
import atexit
import json
import logging
import os
import random
import re
import time
from collections import deque
from pathlib import Path

import numpy as np
import sounddevice as sd
import openwakeword
from openwakeword.model import Model
from faster_whisper import WhisperModel
from websockets.asyncio.server import serve

from brain import Brain
from dispatcher import Dispatcher
from tts import make_tts, PiperTts, ElevenLabsTts
from text_utils import strip_markdown, clamp_for_speech

VERSION = "0.15.0"
PID_FILE = Path(__file__).parent / "cletus.pid"


def _write_pid_file() -> None:
    try:
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass


def _remove_pid_file() -> None:
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


HOST = "127.0.0.1"
PORT = 8765

# Generated images: anything that lands in this folder pops onto the HUD's
# main screen and stays on disk as the archive. The voice brain is told to
# save its image work here; any other tool that drops a file in works too.
IMAGE_DIR = Path.home() / "Pictures" / "Cletus"
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
IMAGE_MAX_BYTES = 8 * 1024 * 1024   # data-URI over a local socket; keep it sane
IMAGE_POLL_S = 2.0

# Phase B live feeds: real uptime probes against the production sites. This is
# the first genuinely live business telemetry on the deck; anything the probe
# can't prove stays a dash on screen. Any HTTP response counts as UP (an auth
# wall still proves the server is alive); only timeouts and refusals are DOWN.
SITES = (
    ("v-nt-api",     "https://servesync-api.azurewebsites.net/"),
    ("v-nt-admin",   "https://nursetrack-admin.vercel.app/"),
    ("v-nt-landing", "https://nursetrack.app/"),
    ("v-ut-site",    "https://unshackledtruthmedia.com/"),
)
SITE_POLL_S = 120

# Wake word: a custom "hey cleetus" model trained 2026-08-13 on the official
# openWakeWord Colab (spelling is phonetic, chosen by ear from the synthesized
# samples). DIY models run less sure of themselves than the pretrained packs,
# so the threshold starts lower than hey_jarvis's 0.75; tune by ear in the room.
WAKE_WORD = "hey_cleetus"
WAKE_MODEL_PATH = Path(__file__).parent / "models" / "hey_cleetus.onnx"
WAKE_THRESHOLD = 0.6
WAKE_COOLDOWN_S = 2.0

# Audio
SAMPLE_RATE = 16000
CHUNK_SAMPLES = 1280  # 80ms at 16kHz

# Recording / silence detection
SILENCE_ENERGY = 0.015          # RMS threshold on float32 audio
# ~1.8s of silence. 1.2s clipped Chris mid-sentence at natural pauses and the
# fragments went to the brain as if they were whole thoughts.
SILENCE_CHUNKS_TO_STOP = 23
MAX_RECORD_CHUNKS = 125         # ~10s hard cap
POST_WAKE_GRACE_CHUNKS = 8      # ~640ms grace before we start counting silence

# Whisper
WHISPER_MODEL_SIZE = "small"
WHISPER_LANGUAGE = "en"
WHISPER_COMPUTE = "int8"
WHISPER_BEAM_SIZE = 5           # greedy (1) misheard proper nouns constantly

# Biases decoding toward the words actually spoken in this house. Without it,
# real transcripts came back "Hicksville" for Higgsfield and "11lbs" for
# eleven labs, and the brain acted on the garble.
WHISPER_VOCABULARY = (
    "Cletus, Higgsfield, ElevenLabs, eleven labs, Piper, NurseTrack, "
    "ServeSync, Kellybuilt, Wendell Turner, Unshackled Truth, Sanity, "
    "Vercel, TestFlight, Obsidian, the vault, dispatcher, HUD."
)

# Follow-up window: after Cletus finishes speaking, the mic stays open briefly so
# a reply doesn't need the wake word again. This is what makes it feel like a
# conversation instead of a series of commands.
FOLLOWUP_WINDOW_S = 7.0        # how long the mic stays open
FOLLOWUP_ARM_DELAY_S = 0.4     # let Cletus's own audio tail clear the room first
FOLLOWUP_ENERGY = 0.02         # well above the silence floor, below normal speech
FOLLOWUP_TRIGGER_CHUNKS = 2    # ~160ms of real voice, so a door slam won't do it

# Pre-roll keeps the last few chunks of audio on hand at all times. When a
# follow-up trips, we seed the recording with them so the first syllable isn't
# clipped off while the trigger was still counting.
PREROLL_CHUNKS = 4             # ~320ms

# Spoken while the brain is off using tools, so real work isn't dead silence.
FILLER_LINES = (
    "Let me take a look.",
    "Hang on, checkin' now.",
    "Give me a second here.",
    "Lookin' it up.",
)

# Say one of these to swap voices mid-conversation, so the two can be heard
# back to back without a restart. Matched only on short utterances so a phrase
# can't trip inside a longer sentence.
#
# These are regexes, not literal strings, on purpose. Speech recognition does
# not hand back what you said, it hands back what it thought you said. Real
# transcripts of "switch to eleven labs" so far: "Switch to 11 Labs." and
# "switch to 11 laps". Chasing exact spellings loses; matching the distinctive
# stem and tolerating the tail wins.
VOICE_SWITCH_PATTERNS = {
    "elevenlabs": (
        r"eleven\s*la",                      # eleven labs / laps / lab / elevenlabs
        r"(switch|change|use|go)\w*\s+(to\s+)?eleven\b",
        r"your (real|new|good) voice",
        r"the good voice",
    ),
    "piper": (
        r"\bpiper\b",
        r"\bpipe\s*r\b",
        r"your (old|robot|local) voice",
        r"the local voice",
    ),
}
VOICE_SWITCH_MAX_LEN = 45
VOICE_SWITCH_CONFIRM = {
    "elevenlabs": "Alright, this is the Eleven Labs voice. How's that sound?",
    "piper": "Back on the local voice.",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("cletus-voice")

connected_clients: set = set()
main_loop: asyncio.AbstractEventLoop | None = None

wake_model: Model | None = None
whisper_model: WhisperModel | None = None
audio_stream: sd.InputStream | None = None
brain: Brain | None = None
tts: PiperTts | ElevenLabsTts | None = None
# Runs real work in real project folders as detached tasks. The audio loop
# never awaits a job, which is the whole point: brain.think() is awaited
# inline below, so anything long would otherwise freeze the microphone, the
# wake word, and the HUD for its entire duration.
dispatcher: Dispatcher | None = None
is_speaking: bool = False

last_wake_at: float = 0.0
is_recording: bool = False
recording_buffer: list = []
silence_count: int = 0
grace_remaining: int = 0

# Follow-up window state. Armed as [followup_from, followup_until) on the
# monotonic clock; both zero means closed.
followup_from: float = 0.0
followup_until: float = 0.0
followup_voice_chunks: int = 0
preroll: deque = deque(maxlen=PREROLL_CHUNKS)

# Serializes every bit of TTS playback. Without it a filler line and the real
# reply can reach the sound device at the same time and talk over each other.
speech_lock = asyncio.Lock()


async def broadcast(msg: dict) -> None:
    if not connected_clients:
        return
    data = json.dumps(msg)
    await asyncio.gather(
        *(c.send(data) for c in connected_clients),
        return_exceptions=True,
    )


async def handler(websocket) -> None:
    connected_clients.add(websocket)
    client_id = id(websocket)
    log.info("HUD connected  [%s]  (%d total)", client_id, len(connected_clients))
    try:
        await websocket.send(json.dumps({
            "event": "connected",
            "service": "cletus-voice",
            "version": VERSION,
            "wake_word": WAKE_WORD,
        }))
        async for raw in websocket:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("recv non-json: %r", raw)
                continue
            # Typed input from the HUD text bar: same pipeline as speech.
            # Broadcast it as a transcript so every client shows the line,
            # then route it without blocking this receive loop.
            if data.get("event") == "user-text":
                typed = str(data.get("text", "")).strip()
                if typed:
                    log.info("typed  %r", typed)
                    await broadcast({"event": "transcript", "text": typed, "source": "typed"})
                    asyncio.create_task(process_utterance(typed))
            else:
                log.info("recv  %s", data)
    finally:
        connected_clients.discard(websocket)
        log.info("HUD disconnected  [%s]  (%d remaining)", client_id, len(connected_clients))


async def heartbeat_loop() -> None:
    while True:
        await asyncio.sleep(15)
        await broadcast({"event": "heartbeat"})


def _from_audio_thread(coro) -> None:
    """Schedule a coroutine on the main loop from the sounddevice thread."""
    if main_loop is not None:
        asyncio.run_coroutine_threadsafe(coro, main_loop)
    else:
        # No loop yet (startup, or a unit test). Close it so it doesn't sit
        # around as an un-awaited coroutine.
        coro.close()


def arm_followup() -> None:
    """Open the no-wake-word window. Called after Cletus finishes a reply."""
    global followup_from, followup_until, followup_voice_chunks
    now = time.monotonic()
    followup_from = now + FOLLOWUP_ARM_DELAY_S
    followup_until = followup_from + FOLLOWUP_WINDOW_S
    followup_voice_chunks = 0
    log.info("follow-up window open  (%.0fs, no wake word needed)", FOLLOWUP_WINDOW_S)
    _from_audio_thread(broadcast({"event": "followup-open", "seconds": FOLLOWUP_WINDOW_S}))


def close_followup(reason: str = "") -> None:
    global followup_from, followup_until, followup_voice_chunks
    if not followup_until:
        return
    followup_from = 0.0
    followup_until = 0.0
    followup_voice_chunks = 0
    if reason:
        log.info("follow-up window closed  (%s)", reason)
    _from_audio_thread(broadcast({"event": "followup-closed"}))


def _begin_recording(*, seed_preroll: bool) -> None:
    """Start capturing. Shared by the wake-word path and the follow-up path."""
    global is_recording, silence_count, grace_remaining
    recording_buffer.clear()
    if seed_preroll:
        # Replay the audio already in hand so the first syllable survives.
        recording_buffer.extend(preroll)
    preroll.clear()
    is_recording = True
    silence_count = 0
    grace_remaining = POST_WAKE_GRACE_CHUNKS


def audio_callback(indata, frames, time_info, status):
    """Runs on the sounddevice audio thread."""
    global last_wake_at, is_recording, silence_count, grace_remaining
    global followup_voice_chunks

    if status:
        log.debug("audio status: %s", status)

    audio_float = indata[:, 0].copy()
    audio_int16 = (audio_float * 32767).astype(np.int16)

    # Always feed the wake model so its internal buffer stays coherent
    # with the live audio stream. We just ignore results while recording
    # or while Cletus is speaking (prevents self-triggered wake).
    predictions = wake_model.predict(audio_int16) if wake_model is not None else {}

    if is_speaking:
        # Keep our own voice out of the pre-roll, or a follow-up would replay it.
        preroll.clear()
        return

    if is_recording:
        recording_buffer.append(audio_float)

        rms = float(np.sqrt(np.mean(audio_float ** 2)))
        if grace_remaining > 0:
            grace_remaining -= 1
        elif rms < SILENCE_ENERGY:
            silence_count += 1
        else:
            silence_count = 0

        if silence_count >= SILENCE_CHUNKS_TO_STOP or len(recording_buffer) >= MAX_RECORD_CHUNKS:
            audio_data = np.concatenate(recording_buffer).astype(np.float32)
            duration_s = len(audio_data) / SAMPLE_RATE
            log.info("recording stopped  duration=%.1fs  silence=%d chunks", duration_s, silence_count)
            recording_buffer.clear()
            is_recording = False
            silence_count = 0
            # Clear the wake model's internal state so we don't ghost-fire
            # on stale context right after transcription starts.
            if wake_model is not None:
                try:
                    wake_model.reset()
                except Exception:
                    pass
            _from_audio_thread(handle_transcription(audio_data))
        return

    # Idle. Keep a rolling pre-roll so a follow-up trigger doesn't clip the
    # first syllable while it was still counting chunks.
    preroll.append(audio_float)

    now = time.monotonic()

    # Follow-up window: reply without saying the wake word again.
    if followup_until:
        if now >= followup_until:
            close_followup("timed out")
        elif now >= followup_from:
            rms = float(np.sqrt(np.mean(audio_float ** 2)))
            if rms >= FOLLOWUP_ENERGY:
                followup_voice_chunks += 1
                if followup_voice_chunks >= FOLLOWUP_TRIGGER_CHUNKS:
                    log.info("FOLLOW-UP  (rms %.4f)  -> recording, no wake word", rms)
                    close_followup()
                    _begin_recording(seed_preroll=True)
                    _from_audio_thread(
                        broadcast({"event": "wake", "word": "follow-up", "score": 1.0})
                    )
                    return
            else:
                followup_voice_chunks = 0

    for name, score in predictions.items():
        if score <= WAKE_THRESHOLD:
            continue
        if now - last_wake_at < WAKE_COOLDOWN_S:
            return
        last_wake_at = now
        log.info("WAKE  %s  (score %.2f)  -> recording", name, score)

        close_followup()
        _begin_recording(seed_preroll=False)
        _from_audio_thread(
            broadcast({"event": "wake", "word": name, "score": float(score)})
        )
        return


def _normalize_utterance(text: str) -> str:
    """Flatten a transcript for phrase matching.

    Whisper writes spoken numbers as digits, so "eleven labs" arrives as
    "11 Labs". Punctuation and casing vary run to run too. Normalize all of
    it before comparing rather than trying to list every spelling.
    """
    s = text.strip().lower()
    s = re.sub(r"[^\w\s]", " ", s)          # punctuation to spaces
    s = re.sub(r"\b11\b", "eleven", s)      # 11 -> eleven
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _requested_backend(text: str) -> str | None:
    """Return a TTS backend name if this utterance is a voice-switch command."""
    s = _normalize_utterance(text)
    if not s or len(s) > VOICE_SWITCH_MAX_LEN:
        return None
    for backend, patterns in VOICE_SWITCH_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, s):
                log.info("voice-switch matched %r -> %s", text, backend)
                return backend
    return None


async def _speak(text: str, *, announce: bool = True) -> None:
    """Serialized TTS playback.

    announce=False is for filler lines: they should be heard, but they should
    not flip the HUD into its full speaking state or fire speaking-end, which
    is what arms the follow-up window.
    """
    global is_speaking
    if tts is None or not text:
        return

    async with speech_lock:
        if announce:
            await broadcast({"event": "speaking-start"})
        is_speaking = True
        try:
            await tts.speak(clamp_for_speech(text))
        except Exception as e:
            log.error("tts playback failed: %s", e)
        finally:
            is_speaking = False
            # Our own voice was just in the wake model's input buffer.
            if wake_model is not None:
                try:
                    wake_model.reset()
                except Exception:
                    pass
            if announce:
                await broadcast({"event": "speaking-end"})


async def handle_transcription(audio_data: np.ndarray) -> None:
    if whisper_model is None:
        log.warning("whisper not loaded, skipping")
        return

    rms = float(np.sqrt(np.mean(audio_data ** 2)))
    if rms < SILENCE_ENERGY * 0.7:
        log.info("recording is mostly silence  (rms=%.4f)  skipping", rms)
        await broadcast({"event": "transcript", "text": "", "note": "silence"})
        return

    await broadcast({"event": "transcribing"})
    log.info("transcribing  %.1fs of audio ...", len(audio_data) / SAMPLE_RATE)

    def do_transcribe() -> str:
        segments, _info = whisper_model.transcribe(
            audio_data,
            language=WHISPER_LANGUAGE,
            beam_size=WHISPER_BEAM_SIZE,
            initial_prompt=WHISPER_VOCABULARY,
            vad_filter=True,
        )
        return " ".join(s.text.strip() for s in segments).strip()

    try:
        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(None, do_transcribe)
        log.info("transcript  %r", text)
        await broadcast({"event": "transcript", "text": text})
    except Exception as e:
        log.error("transcription failed: %s", e)
        await broadcast({"event": "error", "message": str(e)})
        return

    if not text:
        return

    await process_utterance(text)


async def process_utterance(text: str) -> None:
    """Route one utterance: local commands first, then the dispatcher, then
    the conversational brain. Typed input from the HUD's text bar arrives
    here exactly like a spoken transcript, same commands, same brain, same
    spoken reply."""
    # Voice-switch commands are handled locally. No point spending a brain call
    # on "switch to piper".
    requested = _requested_backend(text)
    if requested is not None:
        await switch_tts(requested)
        return

    # "What are you working on?" is answered locally. It is the one question
    # about jobs that must never wait on a brain call, because Chris asks it
    # precisely when he is wondering whether something is stuck.
    if dispatcher is not None and _is_job_status_query(text):
        await _speak(dispatcher.status_line())
        arm_followup()
        return

    # Work orders go to a detached task and are NOT awaited here. Everything
    # below this line blocks the microphone, which is fine for a two-sentence
    # answer and unacceptable for a twenty-minute build.
    if dispatcher is not None:
        try:
            if await dispatcher.maybe_dispatch(text):
                arm_followup()
                return
        except Exception as e:
            log.error("dispatch failed: %s", e)
            await _speak("I couldn't start that job.")
            arm_followup()
            return

    if brain is None:
        return

    await broadcast({"event": "brain-thinking"})

    filler_task: asyncio.Task | None = None

    async def on_first_tool() -> None:
        """Brain reached for a tool, so this answer is going to take a moment.
        Say something instead of leaving the room silent."""
        nonlocal filler_task
        line = random.choice(FILLER_LINES)
        log.info("filler  %r", line)
        await broadcast({"event": "filler", "text": line})
        filler_task = asyncio.create_task(_speak(line, announce=False))

    raw_reply = await brain.think(text, on_first_tool=on_first_tool)
    reply = strip_markdown(raw_reply)
    if not reply:
        return

    await broadcast({"event": "reply", "text": reply})

    # Let the filler finish before the real answer starts, so they can never
    # land out of order.
    if filler_task is not None:
        try:
            await filler_task
        except Exception as e:
            log.warning("filler playback failed: %s", e)

    await _speak(reply)

    # Cletus is done talking, so the floor is Chris's. Open the mic.
    arm_followup()


# Short utterances that ask about running jobs. Length-capped for the same
# reason the reset and voice-switch phrases are: "I was wondering what you're
# working on next month" must not be swallowed as a status query.
JOB_STATUS_PHRASES = (
    "what are you working on",
    "what are you doing",
    "how's that job",
    "hows that job",
    "are you done",
    "job status",
    "still working",
)
JOB_STATUS_MAX_LEN = 45


def _is_job_status_query(text: str) -> bool:
    s = text.strip().lower().rstrip(".!?, ")
    if len(s) > JOB_STATUS_MAX_LEN:
        return False
    return any(p in s for p in JOB_STATUS_PHRASES)


def load_brain() -> None:
    global brain
    try:
        brain = Brain()
        log.info("brain online")
    except Exception as e:
        log.warning("brain offline: %s", e)


def load_dispatcher() -> None:
    """Wire the job runner to the same broadcast and speech paths the rest of
    the service uses, so job events reach the HUD and job results are spoken
    through the one lock that serializes all playback."""
    global dispatcher
    try:
        dispatcher = Dispatcher(on_event=broadcast, speak=_speak)
        log.info("dispatcher online")
    except Exception as e:
        log.warning("dispatcher offline: %s", e)


def load_tts() -> None:
    global tts
    try:
        tts = make_tts()
        if tts.ready:
            log.info("tts online  backend=%s", tts.name)
        else:
            log.warning("tts loaded but not ready")
            tts = None
    except Exception as e:
        log.warning("tts offline: %s", e)


async def switch_tts(backend: str) -> None:
    """Swap the voice mid-conversation so the two can be compared back to back."""
    global tts
    current = tts.name if tts is not None else "none"
    if current == backend:
        await _speak("Already on that one.")
        arm_followup()
        return

    try:
        new = make_tts(backend)
    except Exception as e:
        log.error("could not switch tts to %s: %s", backend, e)
        return

    if not new.ready:
        log.warning("backend %s is not ready, staying on %s", backend, current)
        await _speak("That voice isn't set up yet, stayin' put.")
        arm_followup()
        return

    tts = new
    log.info("tts switched  %s -> %s", current, new.name)
    await broadcast({"event": "tts-backend", "backend": new.name})
    await _speak(VOICE_SWITCH_CONFIRM.get(backend, "Voice switched."))
    arm_followup()


def load_whisper() -> None:
    global whisper_model
    try:
        log.info("loading whisper %s model  (downloads on first run)...", WHISPER_MODEL_SIZE)
        whisper_model = WhisperModel(
            WHISPER_MODEL_SIZE,
            device="cpu",
            compute_type=WHISPER_COMPUTE,
        )
        log.info("whisper ready")
    except Exception as e:
        log.error("failed to load whisper: %s", e)


def start_wake_listener() -> None:
    global wake_model, audio_stream

    log.info("ensuring wake word models are present...")
    try:
        openwakeword.utils.download_models()
    except Exception as e:
        log.warning("model download step: %s", e)

    try:
        log.info("loading wake word model: %s (%s)", WAKE_WORD, WAKE_MODEL_PATH.name)
        wake_model = Model(wakeword_models=[str(WAKE_MODEL_PATH)], inference_framework="onnx")
    except Exception as e:
        log.error("failed to load wake word model: %s", e)
        log.error("wake detection disabled, WebSocket only")
        return

    try:
        log.info("opening default microphone at %d Hz", SAMPLE_RATE)
        audio_stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=CHUNK_SAMPLES,
            callback=audio_callback,
        )
        audio_stream.start()
        log.info("wake listener active  ->  say '%s'", WAKE_WORD.replace("_", " "))
    except Exception as e:
        log.error("failed to start audio input: %s", e)
        log.error("wake detection disabled, WebSocket only")


async def watch_images() -> None:
    """Broadcast new images dropped in IMAGE_DIR to the HUD as data URIs.

    Files already present at boot are treated as seen, so a restart doesn't
    replay the whole archive onto the screen. A file is only sent once its
    size has stopped changing, so a slow download can't ship half an image.
    """
    import base64
    import mimetypes

    try:
        IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        log.warning("image dir unavailable: %s", e)
        return

    def current() -> dict:
        out = {}
        for p in IMAGE_DIR.iterdir():
            if p.suffix.lower() in IMAGE_EXTS:
                try:
                    out[p.name] = p.stat().st_mtime
                except OSError:
                    pass
        return out

    seen = current()
    log.info("image watcher on %s  (%d existing ignored)", IMAGE_DIR, len(seen))

    while True:
        await asyncio.sleep(IMAGE_POLL_S)
        try:
            for name, mtime in current().items():
                if seen.get(name) == mtime:
                    continue
                p = IMAGE_DIR / name
                size_before = p.stat().st_size
                await asyncio.sleep(0.6)
                if not p.exists() or p.stat().st_size != size_before:
                    continue  # still being written; catch it next pass
                seen[name] = p.stat().st_mtime
                if size_before > IMAGE_MAX_BYTES:
                    log.warning("image %s skipped, %d bytes is too big for the HUD", name, size_before)
                    continue
                mime = mimetypes.guess_type(name)[0] or "image/png"
                data = base64.b64encode(p.read_bytes()).decode("ascii")
                log.info("image -> HUD  %s  (%d KB)", name, size_before // 1024)
                await broadcast({
                    "event": "show-image",
                    "name": name,
                    "data": f"data:{mime};base64,{data}",
                })
        except Exception as e:
            log.warning("image watcher pass failed: %s", e)


async def watch_sites() -> None:
    """Probe the production sites and stream real UP/DOWN + latency to the HUD."""
    import urllib.error
    import urllib.request

    def probe(url: str):
        req = urllib.request.Request(url, headers={"User-Agent": "cletus-hud/1.0"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=6) as r:
                r.read(64)
            return True, int((time.time() - t0) * 1000)
        except urllib.error.HTTPError:
            # the server answered, even if it answered "no" - that is UP
            return True, int((time.time() - t0) * 1000)
        except Exception:
            return False, None

    loop = asyncio.get_running_loop()
    while True:
        for key, url in SITES:
            up, ms = await loop.run_in_executor(None, probe, url)
            await broadcast({"event": "site-status", "key": key, "up": up, "ms": ms})
        await asyncio.sleep(SITE_POLL_S)


async def main() -> None:
    global main_loop
    main_loop = asyncio.get_running_loop()

    load_brain()
    load_dispatcher()
    load_tts()
    load_whisper()
    start_wake_listener()
    asyncio.create_task(watch_images())
    asyncio.create_task(watch_sites())

    log.info("starting cletus voice service v%s on ws://%s:%d", VERSION, HOST, PORT)
    try:
        async with serve(handler, HOST, PORT):
            await heartbeat_loop()
    finally:
        # Cancel any job still running so Ctrl-C or a HUD close does not leave
        # an orphaned agent editing files with nobody watching.
        if dispatcher is not None:
            await dispatcher.shutdown()


if __name__ == "__main__":
    # Claim the PID file only when actually run as the service. Doing this at
    # import time meant any tooling that imported this module stomped the
    # running service's PID file and deleted it on exit, which broke the
    # launcher's shutdown path.
    _write_pid_file()
    atexit.register(_remove_pid_file)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        if audio_stream is not None:
            try:
                audio_stream.stop()
                audio_stream.close()
            except Exception:
                pass
