# Cletus

Cletus is a local, always-on voice assistant with a real agent behind it. Say the wake word, talk, and it answers out loud; it can also read and write files, run shell commands, use MCP servers, and hand longer jobs to a background worker pointed at a named project. Architecture in one line: an Electron HUD on the desktop, a Python voice service that owns the microphone and speaker, and an agent brain built on the Claude Agent SDK, joined by a local WebSocket.

## How it works

1. **Wake word.** `sounddevice` streams 16 kHz mono audio in 80 ms chunks into an OpenWakeWord model. The wake word is a custom-trained "hey cleetus" ONNX model that ships in the repo (`voice/models/hey_cleetus.onnx`), scored at a 0.6 threshold with a 2 s cooldown. The detector is muted while Cletus is speaking so it cannot trigger on its own voice.
2. **Capture.** Recording runs until about 1.8 s of silence (RMS below a floor) or a 10 s hard cap. After every reply a 7 s follow-up window keeps the microphone open so a response does not need the wake word again, seeded with a 320 ms pre-roll so the first syllable is not clipped.
3. **Speech to text.** `faster-whisper` with the `small` model on CPU, int8 compute, beam size 5, VAD filtering, and an initial-prompt vocabulary that biases decoding toward names the speaker actually uses. Transcription runs in a thread executor so the audio loop never blocks.
4. **Routing.** Local commands are handled without a model call: voice-backend switching, "what are you working on" job status, and reset phrases. The dispatcher then checks whether the sentence is a work order. Anything else goes to the conversational brain.
5. **Reasoning.** The brain calls the Claude Agent SDK (`query` with `ClaudeAgentOptions`) using the `claude_code` system prompt preset plus a spoken-voice addendum, model `claude-sonnet-4-6`, and a six-turn cap. Each exchange resumes the previous session id, persisted to disk and expired after six idle hours, so back-to-back wakes are one conversation. The first tool call of an exchange triggers a short spoken filler line so tool use is never dead air.
6. **Text to speech.** Replies are stripped of Markdown and clamped to 1500 characters, then spoken through Piper (local, `en_US-lessac-medium`) or ElevenLabs (cloud, PCM 24 kHz, `eleven_flash_v2_5`). Piper is always loaded as the fallback, and all playback is serialized through one lock so a filler and a reply can never overlap.
7. **HUD state.** The service runs a `websockets` server on `ws://127.0.0.1:8765` and broadcasts events (`wake`, `transcribing`, `transcript`, `brain-thinking`, `filler`, `reply`, `speaking-start`, `speaking-end`, `job-*`, `show-image`, `site-status`, a 15 s `heartbeat`). The Electron renderer maps them onto four states (idle, listening, thinking, speaking), shows the exchange, and reconnects every 2 s if the service drops. The HUD can also send typed text back over the same socket, which enters the pipeline exactly like a transcript.

## Capabilities

- **Agent tools.** Because the brain runs on the Claude Agent SDK with the Claude Code preset and `bypassPermissions`, it has file read, write, and edit, Grep and Glob, Bash, and any MCP servers and skills configured in the user's Claude Code settings.
- **Job dispatcher.** A project registry (`voice/projects.py`) maps spoken aliases to folders on disk. A sentence dispatches when it names a registered project and contains an action verb ("fix", "build", "run the tests"), or uses an explicit prefix ("work on", "start a job"). Questions never dispatch, and a verb with no project named never dispatches; a wrong guess would point an unattended agent at the wrong repository, so silence is the safe default. A dispatched job runs as a detached `asyncio` task (`voice/worker.py`) with its working directory set to the project root, a 200-turn cap, and a system addendum that forbids pushing, deploying, merging, or deleting outside the project. The microphone stays live while it works; progress and completion are pushed to the HUD, and the worker's final message is read aloud.
- **Voice switching.** Say "switch to eleven labs" or "switch to piper" to swap backends mid-conversation. Matching is regex-based because the transcriber returns "11 Labs" as often as "eleven labs".
- **Image generation.** `voice/gen_image.py` calls OpenAI `gpt-image-1` and saves into a watched pictures folder; the service pushes any new image there to the HUD as a data URI (8 MB cap).
- **Live site probes.** The service polls a configured list of production URLs every two minutes and streams up/down plus latency to the HUD. Any HTTP response counts as up; only timeouts and refusals count as down.

## Project structure

```
cletus.bat              Windows launcher: starts the voice service, opens the HUD, shuts both down
voice/
  main.py               Voice service: audio loop, wake word, transcription, routing, WebSocket server
  brain.py              Claude Agent SDK integration, session persistence, auth handling
  dispatcher.py         Work-order detection and job lifecycle
  worker.py             Detached agent run inside a project folder
  projects.py           Registry of spoken aliases to project paths
  tts.py                Piper and ElevenLabs backends behind one interface
  text_utils.py         Markdown stripping, transcript flattening, speech clamping
  gen_image.py          Image generation helper the brain shells out to
  audition.py           Plays voice candidates back to back for picking a voice by ear
  test_dispatcher.py    Pytest suite for dispatch detection
  models/               Wake word model (tracked) and Piper voice (downloaded on first run)
  requirements.txt      Pinned Python dependencies
hud/
  main.js               Electron main process: frameless, transparent, always-on-top window
  preload.js            Context bridge exposing a close action to the renderer
  src/                  index.html, renderer.js, style.css for the command-deck UI
logs/                   Voice service log lands here at runtime
```

## Setup

**Prerequisites**

- Python 3.10.x (the pinned dependencies were verified on 3.10.9)
- Node 24.x with npm (verified on Node 24.18.1, npm 11.16.0)
- The Claude Code CLI installed and signed in; the Agent SDK spawns it as a subprocess and uses its stored login
- A microphone and speakers

**Use Python 3.10 with an explicit interpreter.** `numpy==2.2.6` publishes no wheel for Python 3.14, so building the venv with whatever `python` resolves to on a newer system makes pip fall back to compiling numpy from source, which fails deep into the install with `metadata-generation-failed`. Always create the venv with a named interpreter (`py -0` lists what is installed).

```
git clone <repo> cletus
cd cletus

py -3.10 -m venv voice\venv
voice\venv\Scripts\python.exe --version          REM confirm it says 3.10.x
voice\venv\Scripts\python.exe -m pip install -r voice\requirements.txt

cd hud
npm install
cd ..

cletus.bat
```

`requirements.txt` pins eight packages: `websockets`, `openwakeword`, `sounddevice`, `numpy`, `faster-whisper`, `anthropic`, `piper-tts`, and `claude-agent-sdk`. Electron is pinned by `hud/package-lock.json`.

**Environment variables** (all optional)

| Variable | Effect |
|---|---|
| `CLETUS_TTS` | `piper` (default) or `elevenlabs`. Selects the boot voice. |
| `ELEVENLABS_API_KEY` | Enables the ElevenLabs backend. Without it the service warns and uses Piper. |
| `ELEVENLABS_VOICE_ID` | Overrides the ElevenLabs voice. |
| `ELEVENLABS_MODEL` | Overrides the ElevenLabs model (default `eleven_flash_v2_5`). |
| `ELEVENLABS_FORMAT` | Overrides the output format (default `pcm_24000`). |
| `OPENAI_API_KEY` | Enables image generation through `gen_image.py`. |

**First run** downloads the Piper voice (about 60 MB) into `voice/models/`, the faster-whisper `small` model into the Hugging Face cache, and the OpenWakeWord base models. Expect the first start to take a few minutes; after that it is seconds. Model files other than the wake word are gitignored.

**Launcher.** `cletus.bat` starts the voice service minimized with output to `logs/voice.log`, waits for the models to load, then runs `npm start` in `hud/`. When the HUD window closes, the launcher kills the service by the PID it wrote to `voice/cletus.pid`. The HUD opens frameless and transparent in the bottom-right corner of the primary display.

## Auth model

The brain uses the Claude Code CLI's stored OAuth login and nothing else. Before spawning the CLI it clears `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` from the subprocess environment so the SDK cannot silently route to a billed API key. On a rate limit the service says so out loud and stops rather than falling back. The code comments record that an API-key fallback used to exist and was removed on purpose: an unreachable fallback would have started billing the moment a key was set for some unrelated purpose. Background workers reuse the same environment builder, so there is one auth path to audit.

## Tests

```
voice\venv\Scripts\python.exe -m pytest voice\test_dispatcher.py -q
```

The suite covers the highest-consequence logic in the service: sentences that must dispatch (project plus verb, explicit prefixes), sentences that must never dispatch (questions about a project, verbs with no project named, nothing named at all), longest-alias-wins resolution so a specific name is not shadowed by a shorter one, every alias resolving to its own project, and real transcriber output with capitalization and trailing punctuation.

## Status

The full loop works end to end: wake word, capture with follow-up window, transcription, agent reasoning with tools, spoken reply through either voice, session memory across wakes, and the HUD reflecting every stage. The job dispatcher, worker, and project registry are built and covered by the unit suite; a full live mic-to-speaker pass of a dispatched job is the open item before it merges to `main`. On the HUD, the F1 to F4 focus keys are a local override only and are not yet driven by the service, and monitor panels with no live feed show a dash rather than a number.
