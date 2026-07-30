# Cletus Voice Service

Local Python service that handles wake word detection, speech transcription, and voice output. Talks to the HUD over WebSocket on `ws://127.0.0.1:8765`.

## First-time setup

From this folder:

```
pip install -r requirements.txt
```

## Run

```
python main.py
```

Expected output:

```
HH:MM:SS  INFO   starting cletus voice service v0.4.0 on ws://127.0.0.1:8765
```

Leave it running in one terminal. Start the HUD in another. The HUD connects automatically and the voice indicator turns green.

## Phase status

- 4a done: scaffold + WebSocket
- 4b: wake word (OpenWakeWord)
- 4c: transcription (faster-whisper)
- 4d: custom "Cletus" wake word training
