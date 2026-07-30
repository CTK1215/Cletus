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
2. Static workshop scene (done, cheesy SVG placeholder)
3. State machine + animations (done)
4. Voice input, wake word + Whisper (done)
5. Cletus brain hookup, basic Claude Sonnet 4.6 chat (done)
6. Voice output, Piper TTS (done)
7. Full agent capabilities via Claude Agent SDK, dual-auth, tools + MCP (done)

## Deferred

- Custom "Cletus" wake word (currently "hey jarvis")
- Cletus-voice TTS (currently a generic voice, either ElevenLabs clone or a country-sounding Piper voice)
- Prettier art (currently CSS/SVG placeholders)
- Conversation memory across exchanges (currently single-turn each wake)
- Real weather/clock/hat-tip polish
