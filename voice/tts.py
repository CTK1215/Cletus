"""Cletus TTS.

Two backends behind one interface:

  piper       Local, free, instant, flat. Voice model downloads on first run
              into voice/models/.
  elevenlabs  Cloud, paid, sounds like a person. Needs ELEVENLABS_API_KEY.

Piper is always loaded, even when ElevenLabs is selected, because it doubles
as the fallback. If a cloud call fails (no network, bad key, quota gone),
Cletus still talks instead of going silent.

Backend is chosen by the CLETUS_TTS env var and can be switched at runtime by
voice, so the two can be compared back to back without a restart.
"""

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from urllib.request import urlretrieve

import numpy as np
import sounddevice as sd
from piper import PiperVoice

log = logging.getLogger("cletus-tts")

# ---------------------------------------------------------------------------
# Piper
# ---------------------------------------------------------------------------

MODEL_DIR = Path(__file__).parent / "models"
VOICE_NAME = "en_US-lessac-medium"
VOICE_URL_BASE = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/main"
    "/en/en_US/lessac/medium"
)
MODEL_URL = f"{VOICE_URL_BASE}/{VOICE_NAME}.onnx"
CONFIG_URL = f"{VOICE_URL_BASE}/{VOICE_NAME}.onnx.json"

# ---------------------------------------------------------------------------
# ElevenLabs
# ---------------------------------------------------------------------------

ELEVEN_API = "https://api.elevenlabs.io/v1/text-to-speech"
ELEVEN_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "oubi7HGxNVjXMnWLgwBT")
# flash_v2_5 is their low-latency model. Quality models add hundreds of ms,
# which is the opposite of what this project just spent an afternoon fixing.
ELEVEN_MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_flash_v2_5")
# Raw 16-bit PCM means no MP3 decoder in the audio path.
ELEVEN_FORMAT = os.environ.get("ELEVENLABS_FORMAT", "pcm_24000")
ELEVEN_SAMPLE_RATE = int(ELEVEN_FORMAT.rsplit("_", 1)[-1]) if "pcm_" in ELEVEN_FORMAT else 24000
ELEVEN_TIMEOUT_S = 30

DEFAULT_BACKEND = os.environ.get("CLETUS_TTS", "piper").strip().lower()


class PiperTts:
    """Local Piper voice. Always available once the model is on disk."""

    name = "piper"

    def __init__(self):
        self.voice: PiperVoice | None = None
        self.sample_rate: int = 22050
        self._load()

    @property
    def ready(self) -> bool:
        return self.voice is not None

    def _download_if_missing(self) -> tuple[Path, Path]:
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        model_path = MODEL_DIR / f"{VOICE_NAME}.onnx"
        config_path = MODEL_DIR / f"{VOICE_NAME}.onnx.json"

        if not model_path.exists():
            log.info("downloading Piper voice %s (~60MB, one-time)...", VOICE_NAME)
            urlretrieve(MODEL_URL, model_path)
            log.info("voice model downloaded")

        if not config_path.exists():
            log.info("downloading voice config...")
            urlretrieve(CONFIG_URL, config_path)

        return model_path, config_path

    def _load(self) -> None:
        try:
            model_path, config_path = self._download_if_missing()
            log.info("loading Piper voice %s", VOICE_NAME)
            self.voice = PiperVoice.load(model_path, config_path=config_path)
            # sample rate is per-chunk; peek by synthesizing a short probe.
            for chunk in self.voice.synthesize("test"):
                self.sample_rate = chunk.sample_rate
                break
            log.info("tts ready  backend=piper  voice=%s  sample_rate=%d", VOICE_NAME, self.sample_rate)
        except Exception as e:
            log.error("failed to load piper tts: %s", e)
            self.voice = None

    def _synthesize_sync(self, text: str) -> np.ndarray:
        if self.voice is None:
            return np.array([], dtype=np.int16)
        pieces = []
        for chunk in self.voice.synthesize(text):
            pieces.append(chunk.audio_int16_array)
            self.sample_rate = chunk.sample_rate
        if not pieces:
            return np.array([], dtype=np.int16)
        return np.concatenate(pieces)

    def _play_sync(self, audio: np.ndarray) -> None:
        if len(audio) == 0:
            return
        sd.play(audio, samplerate=self.sample_rate)
        sd.wait()

    async def speak(self, text: str) -> None:
        if self.voice is None or not text:
            return
        loop = asyncio.get_running_loop()
        audio = await loop.run_in_executor(None, self._synthesize_sync, text)
        if len(audio) == 0:
            return
        await loop.run_in_executor(None, self._play_sync, audio)


class ElevenLabsTts:
    """Cloud voice. Falls back to Piper on any failure so Cletus never goes mute."""

    name = "elevenlabs"

    def __init__(self, fallback: PiperTts | None = None):
        self.api_key = os.environ.get("ELEVENLABS_API_KEY")
        self.voice_id = ELEVEN_VOICE_ID
        self.model = ELEVEN_MODEL
        self.sample_rate = ELEVEN_SAMPLE_RATE
        self.fallback = fallback
        self._warned = False
        # Set when the API says the account itself is the problem (quota,
        # key, permissions). Stops the voice flip-flopping where short
        # replies still fit under the remaining quota and long ones don't.
        # An explicit "switch to eleven labs" builds a fresh instance via
        # make_tts(), so the flag resets when Chris asks for a retry.
        self._disabled = False

        if not self.api_key:
            log.warning(
                "elevenlabs selected but ELEVENLABS_API_KEY is not set; "
                "set it and restart, falling back to piper for now"
            )
        else:
            log.info(
                "tts ready  backend=elevenlabs  voice=%s  model=%s  format=%s",
                self.voice_id, self.model, ELEVEN_FORMAT,
            )

    @property
    def ready(self) -> bool:
        # Ready if we can speak at all, by either path.
        return bool(self.api_key) or bool(self.fallback and self.fallback.ready)

    def _synthesize_sync(self, text: str) -> np.ndarray:
        url = "%s/%s?output_format=%s" % (ELEVEN_API, self.voice_id, ELEVEN_FORMAT)
        body = json.dumps({"text": text, "model_id": self.model}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"xi-api-key": self.api_key, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=ELEVEN_TIMEOUT_S) as r:
            raw = r.read()
        # pcm_* is signed 16-bit little-endian mono.
        return np.frombuffer(raw, dtype="<i2")

    def _play_sync(self, audio: np.ndarray) -> None:
        if len(audio) == 0:
            return
        sd.play(audio, samplerate=self.sample_rate)
        sd.wait()

    async def speak(self, text: str) -> None:
        if not text:
            return

        if self.api_key and not self._disabled:
            loop = asyncio.get_running_loop()
            try:
                audio = await loop.run_in_executor(None, self._synthesize_sync, text)
                if len(audio):
                    await loop.run_in_executor(None, self._play_sync, audio)
                    return
                log.warning("elevenlabs returned no audio, falling back to piper")
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read()[:200].decode("utf-8", "replace")
                except Exception:
                    pass
                # A quota, key, or permissions problem will not fix itself
                # mid-session, so actually stop calling the API instead of just
                # logging that we will. Anything else (5xx, timeout shapes) is
                # transient and stays a per-reply fallback.
                if e.code in (401, 402, 403):
                    self._disabled = True
                    log.error("elevenlabs HTTP %s: %s", e.code, detail)
                    log.error("disabled for this session, using piper (say 'switch to eleven labs' to retry)")
                elif not self._warned:
                    self._warned = True
                    log.error("elevenlabs HTTP %s: %s", e.code, detail)
                else:
                    log.warning("elevenlabs HTTP %s, using piper", e.code)
            except Exception as e:
                log.warning("elevenlabs call failed (%s), using piper", e)

        if self.fallback is not None and self.fallback.ready:
            await self.fallback.speak(text)


_piper_singleton: PiperTts | None = None


def _get_piper() -> PiperTts:
    """Load Piper once and reuse it. Switching backends mid-session should not
    pay the model load again."""
    global _piper_singleton
    if _piper_singleton is None:
        _piper_singleton = PiperTts()
    return _piper_singleton


def make_tts(backend: str | None = None) -> PiperTts | ElevenLabsTts:
    """Build the configured backend. Piper is always constructed, since it is
    both a valid choice and the fallback for the cloud path."""
    choice = (backend or DEFAULT_BACKEND).strip().lower()
    piper = _get_piper()

    if choice in ("elevenlabs", "eleven", "11labs", "11"):
        return ElevenLabsTts(fallback=piper)
    if choice != "piper":
        log.warning("unknown CLETUS_TTS value %r, using piper", choice)
    return piper


# Backwards-compatible alias: older code constructed Tts() directly.
Tts = PiperTts
