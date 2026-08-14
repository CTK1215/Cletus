"""Generate an image with OpenAI's gpt-image-1 and drop it on the big screen.

Usage:
    python gen_image.py "a red barn at sunset" [filename-hint]

The file lands in C:\\Users\\Christopher\\Pictures\\Cletus, which the voice
service watches; anything saved there pops onto the HUD's center screen on
its own. Stdout is written for the brain to read back to Chris.

The API key comes from the OPENAI_API_KEY environment variable, with a
fallback to the user registry (HKCU\\Environment). The fallback matters:
processes launched from a stale shell inherit an environment older than a
`setx`, but the registry always has the current value. The key itself is
never printed anywhere.
"""

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

OUT_DIR = Path.home() / "Pictures" / "Cletus"
API_URL = "https://api.openai.com/v1/images/generations"
MODEL = "gpt-image-1"
SIZE = "1024x1024"
QUALITY = "medium"
TIMEOUT_S = 180


def api_key() -> str | None:
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as reg:
            value, _ = winreg.QueryValueEx(reg, "OPENAI_API_KEY")
            return value or None
    except OSError:
        return None


def slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].rstrip("-") or "image"


def main() -> int:
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print("No prompt given. Usage: gen_image.py \"what to paint\" [filename-hint]")
        return 2

    prompt = sys.argv[1].strip()
    hint = sys.argv[2].strip() if len(sys.argv) > 2 else prompt

    key = api_key()
    if not key:
        print("No OPENAI_API_KEY found in the environment or user registry. "
              "Set it with setx and try again.")
        return 3

    body = json.dumps({
        "model": MODEL,
        "prompt": prompt,
        "size": SIZE,
        "quality": QUALITY,
        "n": 1,
    }).encode("utf-8")

    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
        except Exception:
            pass
        print(f"Image generation failed (HTTP {e.code}). {detail}".strip())
        return 4
    except Exception as e:
        print(f"Image generation failed: {e}")
        return 4

    try:
        b64 = payload["data"][0]["b64_json"]
    except (KeyError, IndexError):
        print("The API answered but returned no image data.")
        return 4

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{slug(hint)}-{time.strftime('%H%M%S')}.png"
    path = OUT_DIR / name
    path.write_bytes(base64.b64decode(b64))

    print(f"Saved {name} ({path.stat().st_size // 1024} KB) to the Cletus picture folder. "
          "It's on the big screen now.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
