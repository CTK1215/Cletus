"""Cletus Voice Service."""

import asyncio
import atexit
import json
import logging
import os
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
import openwakeword
from openwakeword.model import Model
from faster_whisper import WhisperModel
from websockets.asyncio.server import serve

from brain import Brain
from tts import Tts
from text_utils import strip_markdown, clamp_for_speech

VERSION = "0.7.0"
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


_write_pid_file()
atexit.register(_remove_pid_file)
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
tts: Tts | None = None
is_speaking: bool = False

last_wake_at: float = 0.0
is_recording: bool = False
recording_buffer: list = []
silence_count: int = 0
grace_remaining: int = 0


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


def audio_callback(indata, frames, time_info, status):
    """Runs on the sounddevice audio thread."""
    global last_wake_at, is_recording, silence_count, grace_remaining

    if status:
        log.debug("audio status: %s", status)

    audio_float = indata[:, 0].copy()
    audio_int16 = (audio_float * 32767).astype(np.int16)

    # Always feed the wake model so its internal buffer stays coherent
    # with the live audio stream. We just ignore results while recording
    # or while Cletus is speaking (prevents self-triggered wake).
    predictions = wake_model.predict(audio_int16) if wake_model is not None else {}

    if is_speaking:
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
            if main_loop is not None:
                asyncio.run_coroutine_threadsafe(handle_transcription(audio_data), main_loop)
        return

    for name, score in predictions.items():
        if score <= WAKE_THRESHOLD:
            continue
        now = time.monotonic()
        if now - last_wake_at < WAKE_COOLDOWN_S:
            return
        last_wake_at = now
        log.info("WAKE  %s  (score %.2f)  -> recording", name, score)

        is_recording = True
        recording_buffer.clear()
        silence_count = 0
        grace_remaining = POST_WAKE_GRACE_CHUNKS

        if main_loop is not None:
            asyncio.run_coroutine_threadsafe(
                broadcast({"event": "wake", "word": name, "score": float(score)}),
                main_loop,
            )
        return


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

    if brain is None:
        return
    if not text:
        return

    await broadcast({"event": "brain-thinking"})
    raw_reply = await brain.think(text)
    reply = strip_markdown(raw_reply)
    if not reply:
        return

    await broadcast({"event": "reply", "text": reply})

    if tts is None:
        return

    global is_speaking
    await broadcast({"event": "speaking-start"})
    is_speaking = True
    try:
        await tts.speak(clamp_for_speech(reply))
    except Exception as e:
        log.error("tts playback failed: %s", e)
    finally:
        is_speaking = False
        # Reset wake model since our own voice was in its input buffer.
        if wake_model is not None:
            try:
                wake_model.reset()
            except Exception:
                pass
        await broadcast({"event": "speaking-end"})


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
        tts = Tts()
        if tts.voice is not None:
            log.info("tts online")
        else:
            log.warning("tts loaded but voice is None")
            tts = None
    except Exception as e:
        log.warning("tts offline: %s", e)


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
