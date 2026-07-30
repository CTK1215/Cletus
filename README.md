# Cletus

Voice-driven personal assistant with a real agent brain. Wake word plus a visual workshop scene, and Cletus can actually do work (read/write vault files, run commands, use MCP tools) via the Claude Agent SDK.

## Layout

```
Cletus/
├── cletus.bat            One-click launcher
├── hud/                  Electron app, the visual interface
├── voice/                Python voice pipeline (wake, whisper, brain, tts)
│   ├── main.py           Voice service (WebSocket + audio pipeline)
│   ├── brain.py          Claude Agent SDK integration, dual-auth
│   ├── tts.py            Piper text-to-speech
│   ├── text_utils.py     Markdown stripping for clean speech
│   └── requirements.txt
├── logs/                 Voice service log lands here
└── docs/                 Design notes
```

## Daily use

Double-click **`cletus.bat`**. Voice service starts minimized, HUD opens in the bottom-right corner. Say the wake word and talk. Close the HUD to shut everything down.

## Capabilities

Cletus can:

- Listen for a wake word ("hey jarvis" for now) and transcribe your speech
- Reply out loud through the Piper TTS voice (generic American male for now)
- **Read files** in your vault and elsewhere (Read tool)
- **Write and edit files** (Write, Edit tools)
- **Search the codebase** (Grep, Glob tools)
- **Run shell commands** (Bash tool, permissions bypassed)
- **Use any MCP servers** you have configured for Claude Code (Gmail, Notion, Higgsfield, etc.)
- **Use skills** you have installed

Identity loads from `C:\Users\Christopher\CLAUDE.md`, same as your typing sessions.

## Auth

Dual-auth so voice keeps working even when your Claude Code sessions are hot:

1. **Max plan (OAuth)** first, via your stored Claude Code login. Free tokens.
2. **API key fallback** on rate limit. Uses `ANTHROPIC_API_KEY` env var. Small per-token cost.

## First-time setup

If you haven't already:

**1. Voice service Python deps:**
```
cd voice
pip install -r requirements.txt
```

**2. HUD Node deps:**
```
cd hud
npm install
```

**3. Anthropic API key** for the fallback. Get one at [console.anthropic.com](https://console.anthropic.com/settings/keys), then in PowerShell:
```
setx ANTHROPIC_API_KEY "sk-ant-api-..."
```
Close and reopen your terminals so the env var takes.

**4. Max plan token** is auto-used from your Claude Code login. Nothing to set.

## Restore from a clean clone

The repo tracks source only. Three things are deliberately not in git because they
are large, machine-specific, and rebuildable:

| Not tracked | Size | How it comes back |
|---|---|---|
| `voice/venv/` | ~900MB | `pip install -r voice/requirements.txt` |
| `hud/node_modules/` | ~360MB | `npm install` (Electron pinned by `package-lock.json`) |
| `voice/models/` | ~60MB | Downloads itself on first run |

A committed venv would not work anyway. Its `Scripts/*.exe` and `pyvenv.cfg` have
absolute paths baked in, so it is not portable between machines.

**Known-good toolchain:** Python 3.10.9, Node 24.18.1, npm 11.16.0.

To rebuild from nothing:

```
git clone <repo> cletus
cd cletus

python -m venv voice\venv
voice\venv\Scripts\python.exe -m pip install -r voice\requirements.txt

cd hud
npm install
cd ..

cletus.bat
```

First launch downloads the Piper voice (~60MB) into `voice/models/`, the
faster-whisper `small` model into the HuggingFace cache, and the OpenWakeWord
models. Expect the first start to take a few minutes. After that it is seconds.

To verify a restore without launching the full service (useful when an instance
is already running and holding port 8765 and the microphone):

```
voice\venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'voice'); import main, brain, tts, text_utils; print('all modules import clean')"
```

## Make a desktop shortcut

1. Right-click `cletus.bat` → **Send to** → **Desktop (create shortcut)**
2. Right-click the new shortcut → **Properties**
3. Under **Run:** pick **Minimized** so the launcher window stays out of your way
4. Optional: **Change Icon...** to pick your own `.ico`
5. Rename it to **Cletus**

Now Cletus is one double-click away.

## Autostart on Windows login (optional)

1. Press `Win + R`, type `shell:startup`, hit enter
2. Copy your Cletus shortcut into that folder
3. Next login, Cletus starts on its own

## Where things live

- **Voice service log:** `logs/voice.log` (all Python output)
- **HUD console:** open with `Ctrl+Shift+I` inside the HUD window
- **Voice service PID:** `voice/cletus.pid` (used by the launcher to shut it down)
- **Voice working dir:** `C:\Users\Christopher\.cletus-voice\` (isolates SDK sessions from your typed Claude Code sessions)

## Build history

1. Project skeleton + Electron shell (done)
2. Static workshop scene (done, replaced in step 8)
3. State machine + animations (done)
4. Voice input, wake word + Whisper (done)
5. Cletus brain hookup, basic Claude Sonnet 4.6 chat (done)
6. Voice output, Piper TTS (done)
7. Full agent capabilities via Claude Agent SDK, dual-auth, tools + MCP (done)
8. Cyber HQ scene rebuild + conversation memory (done 2026-07-30)

## Deferred

- Custom "Cletus" wake word (currently "hey jarvis")
- Cletus-voice TTS (currently a generic voice, either ElevenLabs clone or a country-sounding Piper voice)
- Wire the F1-F4 project focus system to the WebSocket (`renderer.js:177`)
- Feed the 4 monitors live KPIs (currently hardcoded SVG text)
- Real weather/clock/hat-tip polish
