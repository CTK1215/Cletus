"""Play voice candidates back to back so you can pick one by ear.

    voice\venv\Scripts\python.exe voice\audition.py

Plays the local Piper voice first as a baseline, then each ElevenLabs
candidate. Prints the voice id next to each so you can set the winner:

    setx ELEVENLABS_VOICE_ID "<the id you liked>"

Needs ELEVENLABS_API_KEY in the environment. Voices that your plan cannot
use are reported and skipped rather than failing the run.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

import numpy as np
import sounddevice as sd

LINE = "Yes sir, let's get cookin. That's a tangent from NurseTrack, you want to pursue it or park it?"

# Free-tier premade voices, ordered by how close they sit to the Cletus
# register: laid back, down to earth, mature, warm.
CANDIDATES = [
    ("CwhRBWXzGAHq8TQ4Fs17", "Roger",  "laid-back, casual, resonant"),
    ("iP95p4xoKVk53GoZ742B", "Chris",  "charming, down-to-earth"),
    ("pqHfZKP75CvOlQylNhV4", "Bill",   "wise, mature, balanced"),
    ("nPczCjzI2devNBz1zQrb", "Brian",  "deep, resonant, comforting"),
    ("N2lVS1w4EtoT3dr4eOWO", "Callum", "husky trickster"),
    ("bIHbv24MWmeRgasZH58o", "Will",   "relaxed optimist"),
    # Paid plan required. Left in on purpose so the run tells you plainly
    # whether the good one has unlocked yet.
    ("oubi7HGxNVjXMnWLgwBT", "Cletus", "US SOUTHERN, the one you want"),
]

MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_flash_v2_5")
RATE = 24000


def play(audio: np.ndarray, rate: int) -> None:
    if len(audio) == 0:
        return
    sd.play(audio, samplerate=rate)
    sd.wait()


def piper_baseline() -> None:
    print("\n--- Piper (local, free, what you have now) ---")
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from tts import PiperTts

        p = PiperTts()
        if not p.ready:
            print("  piper not available")
            return
        audio = p._synthesize_sync(LINE)
        print("  playing...")
        play(audio, p.sample_rate)
    except Exception as e:
        print("  piper failed: %s" % e)


def eleven(voice_id: str, key: str) -> np.ndarray | None:
    url = "https://api.elevenlabs.io/v1/text-to-speech/%s?output_format=pcm_%d" % (voice_id, RATE)
    body = json.dumps({"text": LINE, "model_id": MODEL}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"xi-api-key": key, "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return np.frombuffer(r.read(), dtype="<i2")


def main() -> None:
    key = os.environ.get("ELEVENLABS_API_KEY")
    print("=" * 62)
    print("VOICE AUDITION")
    print("=" * 62)
    print("Line: %r" % LINE)

    piper_baseline()

    if not key:
        print("\nELEVENLABS_API_KEY not set, skipping the cloud voices.")
        return

    usable = []
    for vid, name, desc in CANDIDATES:
        print("\n--- %s (%s) ---" % (name, desc))
        print("  id %s" % vid)
        try:
            t0 = time.time()
            audio = eleven(vid, key)
            ms = int((time.time() - t0) * 1000)
            print("  %d ms to synthesize, playing..." % ms)
            play(audio, RATE)
            usable.append((vid, name))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read()).get("detail", {}).get("message", "")
            except Exception:
                pass
            print("  SKIPPED  HTTP %s  %s" % (e.code, detail))
        except Exception as e:
            print("  SKIPPED  %s" % e)
        time.sleep(0.4)

    print("\n" + "=" * 62)
    if usable:
        print("Played: %s" % ", ".join(n for _, n in usable))
        print("\nSet the one you liked:")
        print('  setx ELEVENLABS_VOICE_ID "<id from above>"')
        print("Then restart Cletus and say: switch to eleven labs")
    else:
        print("No cloud voices were usable. Check the key and the plan.")


if __name__ == "__main__":
    main()
