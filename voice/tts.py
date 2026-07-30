"""Cletus TTS.

Local text-to-speech via Piper. Voice model is downloaded on first run
into voice/models/. Placeholder voice; a country twang comes later.
"""

import asyncio
import logging
from pathlib import Path
from urllib.request import urlretrieve

import numpy as np
import sounddevice as sd
from piper import PiperVoice

log = logging.getLogger("cletus-tts")

MODEL_DIR = Path(__file__).parent / "models"
VOICE_NAME = "en_US-lessac-medium"
VOICE_URL_BASE = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/main"
    "/en/en_US/lessac/medium"
)
MODEL_URL = f"{VOICE_URL_BASE}/{VOICE_NAME}.onnx"
CONFIG_URL = f"{VOICE_URL_BASE}/{VOICE_NAME}.onnx.json"


class Tts:
    def __init__(self):
        self.voice: PiperVoice | None = None
        self.sample_rate: int = 22050
        self._load()

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
            log.info("tts ready  voice=%s  sample_rate=%d", VOICE_NAME, self.sample_rate)
        except Exception as e:
            log.error("failed to load tts: %s", e)
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
