# claude-voice

Voice integration plugin for Claude Code. Adds text-to-speech narration, push-to-talk speech input, multi-instance audio management, and an always-on-top overlay UI.

**Windows only** (Linux/macOS support planned).

## Features

- **TTS narration** — Claude's responses and intermediate text are read aloud
- **Push-to-talk STT** — Hold a key to speak, transcribed via Whisper and sent to Claude
- **Tool tones** — Distinct audio tones for different tool types (read, edit, search, etc.)
- **Multi-instance** — Multiple Claude sessions share one audio service; click the overlay to switch
- **Overlay UI** — Always-on-top window showing state, instances, device selection
- **Three TTS backends** — Edge (default, free), Kokoro (local, high quality), ElevenLabs (premium API)

## Install

```
claude plugin install github.com/<username>/claude-voice
```

Then install Python dependencies:

```
pip install -r <plugin-path>/requirements.txt
```

## Usage

### Start the voice service

```
/voice-start
```

Checks if the service is running, starts it if needed, and registers this Claude instance.

### Stop the voice service

```
/voice-stop
```

Deregisters this instance. Optionally shuts down the service entirely.

### Check status

```
/voice-status
```

### Setup Kokoro (optional, higher quality local TTS)

```
/voice-setup-kokoro
```

Downloads ~350MB of model files (no authentication required).

## Architecture

```
Claude Instance 1 ─┐
Claude Instance 2 ─┤── hooks (UDP) ──→ voice_service.py ──→ Audio Output
Claude Instance 3 ─┘                       │
                                            ├── AudioMixer (3 channels)
                                            ├── PushToTalkRecorder (Whisper STT)
                                            ├── EventHandler (TTS queue)
                                            ├── InstanceRegistry
                                            └── voice_overlay.py (tkinter UI)
```

The voice service runs as a standalone process. Claude instances connect via UDP hooks. Only one instance is unmuted at a time — PTT input is routed to it.

## TTS Backends

| Backend | Quality | Latency | Cost | Setup |
|---------|---------|---------|------|-------|
| **Edge** (default) | Good | ~1-2s | Free | None |
| **Kokoro** | Best local | ~0.5s | Free | `/voice-setup-kokoro` |
| **ElevenLabs** | Premium | ~1s | Paid API | Set `ELEVENLABS_API_KEY` |

## Hotkeys

| Key | Action |
|-----|--------|
| Page Down / Insert | Push-to-talk |
| Page Up | Toggle mute |
| Scroll Lock | Pause/resume TTS |
| Ctrl+End | Exit service |
| Ctrl+\\ | Relaunch overlay UI |

All keys are configurable via plugin settings or CLI args.

## Configuration

Plugin settings (prompted on install):

- **ttsBackend** — `edge`, `kokoro`, or `elevenlabs`
- **elevenlabsApiKey** — API key for ElevenLabs (stored securely)
- **pttKey** — Push-to-talk key(s)
- **muteKey** — Mute toggle key

## Requirements

- Windows 10/11
- Python 3.10+
- PortAudio (bundled with sounddevice)
- CUDA (optional, for GPU-accelerated Whisper)
