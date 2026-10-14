# claude-voice

Voice integration plugin for Claude Code. Adds text-to-speech narration, push-to-talk speech input, multi-instance audio management, and an always-on-top overlay UI.

**Windows only** (Linux/macOS support planned).

## Features

- **TTS narration** — Claude's responses and intermediate text are read aloud
- **Push-to-talk STT** (opt-in, `--ptt`) — Hold a key to speak, transcribed via Whisper and sent to Claude.
  Off by default since Claude Code has its own voice input; without it the service never loads an STT model
- **Tool tones** — Distinct audio tones for different tool types (read, edit, search, etc.)
- **Multi-instance** — Multiple Claude sessions share one audio service; click the overlay to switch
- **Overlay UI** — Always-on-top window showing state, instances, device selection
- **Three TTS backends** — Edge (default, free), Kokoro (local, high quality), ElevenLabs (premium API)
- **Separate voices, separately mutable** — responses and tool announcements use
  different voices on different channels, so you can silence the tool announcer
  and keep hearing the replies
- **Skip a sentence** — jump to the next sentence without dropping the rest of the response
- **Listen-only by default** — no mic, no STT model load; pass `--ptt` to turn push-to-talk on
- **Secret tripwire** (opt-in) — watches transcripts for real secret *values* being
  emitted raw, and raises a persistent alarm panel if one leaks

## Controls

| Key | Action |
| --- | --- |
| `Page Down` / `Insert` | Push-to-talk (hold) with `--ptt`; otherwise stops current playback |
| `Page Up` | Mute / unmute |
| `Scroll Lock` | Pause / resume playback |
| `Ctrl+Shift+.` | Skip the current sentence |
| `Ctrl+Shift+N` | Toggle the tool-announcer voice |
| `Ctrl+Shift+J` | Toggle the spoken security alarm |
| `Ctrl+End` | Quit the service |
| `Ctrl+\` | Relaunch the overlay |

The overlay also has 🗣 and 🔔 buttons for the two toggles. All keys are
rebindable — see `--narrate-key`, `--alarm-key`, `--skip-key`, etc.

## Secret tripwire (optional)

Off by default. Point it at a `KEY=VALUE` file to enable:

```
set CLAUDE_VOICE_SECRETS=C:\path\to\secrets.env
```

It then scans each transcript delta for those **values**. A secret's *name*
appearing is fine; the *value* appearing means it was handled raw, and that
fires the alarm: a stderr log line, a durable record, and a red panel in the
overlay naming the agent and the secret.

Only **names** are ever logged, stored, or displayed — never the value itself,
since writing the value down would be the leak it is trying to catch.

`Ctrl+Shift+J` (or the 🔔 button) silences only the *spoken* alarm. The log
line, the durable record, and the visual panel always fire, so muting the
speaker can never hide that a secret leaked.

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
Set `CLAUDE_VOICE_PTT=1` before starting to enable push-to-talk STT (loads Whisper).

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
| Page Down / Insert | Push-to-talk (`--ptt`) / cancel playback |
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
