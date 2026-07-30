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
from tts import make_tts, PiperTts, ElevenLabsTts
from text_utils import strip_markdown, clamp_for_speech

VERSION = "0.9.0"
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

# Wake word
WAKE_WORD = "hey_jarvis"
WAKE_THRESHOLD = 0.75
WAKE_COOLDOWN_S = 2.0

# Audio
SAMPLE_RATE = 16000
CHUNK_SAMPLES = 1280  # 80ms at 16kHz

# Recording / silence detection
SILENCE_ENERGY = 0.015          # RMS threshold on float32 audio
SILENCE_CHUNKS_TO_STOP = 15     # ~1.2s of silence
MAX_RECORD_CHUNKS = 125         # ~10s hard cap
POST_WAKE_GRACE_CHUNKS = 8      # ~640ms grace before we start counting silence

# Whisper
WHISPER_MODEL_SIZE = "small"
WHISPER_LANGUAGE = "en"
WHISPER_COMPUTE = "int8"

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
                log.info("recv  %s", data)
            except json.JSONDecodeError:
                log.warning("recv non-json: %r", raw)
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
            beam_size=1,
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

    # Voice-switch commands are handled locally. No point spending a brain call
    # on "switch to piper".
    requested = _requested_backend(text)
    if requested is not None:
        await switch_tts(requested)
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


def load_brain() -> None:
    global brain
    try:
        brain = Brain()
        log.info("brain online")
    except Exception as e:
        log.warning("brain offline: %s", e)


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
        log.info("loading wake word model: %s", WAKE_WORD)
        wake_model = Model(wakeword_models=[WAKE_WORD], inference_framework="onnx")
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


async def main() -> None:
    global main_loop
    main_loop = asyncio.get_running_loop()

    load_brain()
    load_tts()
    load_whisper()
    start_wake_listener()

    log.info("starting cletus voice service v%s on ws://%s:%d", VERSION, HOST, PORT)
    async with serve(handler, HOST, PORT):
        await heartbeat_loop()


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
