#!/usr/bin/env python3
"""
Voice Service — standalone audio service for Claude Code instances.

Runs independently; Claude Code instances connect via hooks.
Provides TTS, STT, push-to-talk, tool tones, and an overlay UI.

Usage:
    python voice_service.py
    python voice_service.py --backend edge --key "page down"

Multiple Claude instances register by sending a UDP message with their
name, PID, and window handle.  Only one instance is unmuted at a time;
PTT input is routed to the unmuted instance's terminal window.
"""

import json
import sys
import os
import re
import io
import wave
import time
import socket
import tempfile
import argparse
import threading
import queue

# ── Platform guard ────────────────────────────────────────────
if sys.platform != "win32":
    print("claude-voice currently only supports Windows.")
    print("Linux and macOS support is planned.")
    sys.exit(1)

import numpy as np
import sounddevice as sd
import requests

# Import audio utilities from our own package
from audio_utils import (
    MIXER_SR, make_startup_chime, make_beep, make_tool_tone, make_result_chime,
)

# Optional secret tripwire. Inert unless CLAUDE_VOICE_SECRETS points at a
# KEY=VALUE file (see secret_alarm.py), so it costs nothing by default.
from secret_alarm import SecretAlarm
import secret_alarm as secret_alarm_store


# ── Data directory ────────────────────────────────────────────
# Use CLAUDE_PLUGIN_DATA if available (set by Claude Code plugin system),
# otherwise fall back to a sensible default.
PLUGIN_DATA = os.environ.get(
    "CLAUDE_PLUGIN_DATA",
    os.path.join(os.path.expanduser("~"), ".claude", "plugins", "data", "claude-voice"),
)
os.makedirs(PLUGIN_DATA, exist_ok=True)

MODELS_DIR = os.path.join(PLUGIN_DATA, "models")


# ── Constants ─────────────────────────────────────────────────

DEFAULT_PORT = 9111
SENTENCE_END = re.compile(r'(?<=[.!?])\s')


# ── Markdown / path cleaning for TTS ─────────────────────────

def _strip_markdown(text):
    """Remove markdown formatting so TTS reads natural prose, not syntax."""
    text = re.sub(r'```[\s\S]*?```', ' ', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\*{1,3}([^*]+)\*{1,3}', r'\1', text)
    text = re.sub(r'_{1,3}([^_]+)_{1,3}', r'\1', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'!\[([^\]]*)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\|', ' ', text)
    text = re.sub(r'^[\s\-:]+$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*[-*+]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^[-*_]{3,}\s*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'[A-Z]:\\(?:[^\\\s]+\\)*([^\\\s]+)', r'\1', text)
    text = re.sub(r'(?<!\w)/(?:[^/\s]+/)+([^/\s]+)', r'\1', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _basename(path):
    name = os.path.basename(path)
    root, ext = os.path.splitext(name)
    return root if ext else name


# ==============================================================
#  Device lookup
# ==============================================================

def find_device(name, kind=None):
    for i, d in enumerate(sd.query_devices()):
        if name.lower() in d['name'].lower():
            if kind == 'input' and d['max_input_channels'] == 0:
                continue
            if kind == 'output' and d['max_output_channels'] == 0:
                continue
            return i
    return None


def _get_device_list():
    all_devs = list(enumerate(sd.query_devices()))
    default_out = sd.default.device[1]
    default_in = sd.default.device[0]

    outputs = []
    for i, d in all_devs:
        if d['max_output_channels'] > 0:
            outputs.append({"id": i, "name": d['name'], "default": i == default_out})

    inputs = []
    for i, d in all_devs:
        if d['max_input_channels'] > 0:
            inputs.append({"id": i, "name": d['name'], "default": i == default_in})

    return outputs, inputs


# ==============================================================
#  Resampling utility
# ==============================================================

def _resample(audio, src_rate, dst_rate):
    if src_rate == dst_rate:
        return audio
    n_out = int(len(audio) * dst_rate / src_rate)
    return np.interp(
        np.linspace(0, len(audio) - 1, n_out),
        np.arange(len(audio)),
        audio,
    ).astype(np.float32)


# ==============================================================
#  Audio channel & mixer
# ==============================================================

class AudioChannel:
    def __init__(self, volume=1.0):
        self.volume = volume
        self._chunks: list[np.ndarray] = []
        self._chunk_offset = 0
        self._total = 0
        self._lock = threading.Lock()
        self._fade_vol = 1.0
        self._fade_rate = 0.0
        self.paused = False

    def write(self, audio_f32):
        with self._lock:
            self._chunks.append(audio_f32)
            self._total += len(audio_f32)

    def read(self, n):
        if self.paused:
            return None
        with self._lock:
            if self._total == 0:
                return None
            out_parts = []
            remaining = n
            while remaining > 0 and self._chunks:
                chunk = self._chunks[0]
                avail = len(chunk) - self._chunk_offset
                take = min(avail, remaining)
                out_parts.append(chunk[self._chunk_offset:self._chunk_offset + take])
                self._chunk_offset += take
                remaining -= take
                self._total -= take
                if self._chunk_offset >= len(chunk):
                    self._chunks.pop(0)
                    self._chunk_offset = 0
            if not out_parts:
                return None
            out = np.concatenate(out_parts) if len(out_parts) > 1 else out_parts[0].copy()
            if len(out) < n:
                out = np.pad(out, (0, n - len(out)))

        if self._fade_rate != 0.0:
            start = self._fade_vol
            end = max(0.0, min(1.0, start + self._fade_rate * n))
            envelope = np.linspace(start, end, n, dtype=np.float32)
            out *= envelope
            self._fade_vol = end
            if self._fade_vol <= 0.0:
                self.clear()
                self._fade_rate = 0.0
        elif self._fade_vol < 1.0:
            out *= self._fade_vol

        return out

    def fade_out(self, duration_secs, sr=MIXER_SR):
        total = max(1, int(sr * duration_secs))
        self._fade_rate = -self._fade_vol / total

    def fade_reset(self):
        self._fade_rate = 0.0
        self._fade_vol = 1.0

    def has_data(self):
        with self._lock:
            return self._total > 0

    def clear(self):
        with self._lock:
            self._chunks.clear()
            self._chunk_offset = 0
            self._total = 0


class AudioMixer:
    def __init__(self, sr=MIXER_SR, blocksize=2048, device=None):
        self.sr = sr
        self._blocksize = blocksize
        self._device = device
        self.channels: dict[str, AudioChannel] = {}
        self.master_volume = 1.0
        self._duck_level = 0.25
        self._ducking = False
        self._muting = False

        self._stream = sd.OutputStream(
            samplerate=sr, channels=1, dtype="float32",
            blocksize=blocksize, callback=self._callback, device=device,
        )
        self._stream.start()

    def set_device(self, device):
        self._stream.stop()
        self._stream.close()
        self._device = device
        self._stream = sd.OutputStream(
            samplerate=self.sr, channels=1, dtype="float32",
            blocksize=self._blocksize, callback=self._callback, device=device,
        )
        self._stream.start()

    def _callback(self, outdata, frames, _time_info, _status):
        mixed = np.zeros(frames, dtype=np.float32)
        for ch in self.channels.values():
            chunk = ch.read(frames)
            if chunk is not None:
                mixed += chunk * ch.volume
        if self._muting:
            vol = 0.0
        elif self._ducking:
            vol = self._duck_level
        else:
            vol = self.master_volume
        outdata[:, 0] = mixed * vol

    def add_channel(self, name, volume=1.0):
        self.channels[name] = AudioChannel(volume=volume)

    def play(self, channel, audio_f32, sr=None):
        if sr and sr != self.sr:
            audio_f32 = _resample(audio_f32, sr, self.sr)
        self.channels[channel].write(audio_f32)

    def duck(self):
        self._ducking = True
        self._muting = False

    def unduck(self):
        self._ducking = False

    def mute(self):
        self._muting = True
        self._ducking = False

    def unmute(self):
        self._muting = False

    def pause(self, channel="voice"):
        self.channels[channel].paused = True

    def unpause(self, channel="voice"):
        self.channels[channel].paused = False

    def is_paused(self, channel="voice"):
        return self.channels[channel].paused

    def fade_channel(self, name, duration_secs):
        self.channels[name].fade_out(duration_secs, sr=self.sr)

    def reset_channel(self, name):
        ch = self.channels[name]
        ch.clear()
        ch.fade_reset()

    def stop_channel(self, name):
        self.channels[name].clear()

    def stop_all(self):
        for ch in self.channels.values():
            ch.clear()

    def wait_channel(self, name):
        while self.channels[name].has_data():
            time.sleep(0.03)

    def is_playing(self, name=None):
        if name:
            return self.channels[name].has_data()
        return any(ch.has_data() for ch in self.channels.values())


# ==============================================================
#  TTS backends
# ==============================================================

def generate_elevenlabs(text, voice_id=None, voice_settings=None):
    api_key = os.environ.get("ELEVENLABS_API_KEY", "")
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY not set")
    voice_id = voice_id or os.environ.get("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")
    model_id = os.environ.get("ELEVENLABS_MODEL_ID", "eleven_v3")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=pcm_24000"
    headers = {"xi-api-key": api_key, "Content-Type": "application/json"}
    payload = {"text": text, "model_id": model_id}
    if voice_settings:
        payload["voice_settings"] = voice_settings
    resp = requests.post(url, json=payload, headers=headers, timeout=15)
    resp.raise_for_status()
    pcm = np.frombuffer(resp.content, dtype=np.int16).astype(np.float32) / 32768.0
    return pcm, 24000


_edge_loop = None
_edge_lock = threading.Lock()

def _get_edge_loop():
    global _edge_loop
    if _edge_loop is None:
        with _edge_lock:
            if _edge_loop is None:
                import asyncio
                _edge_loop = asyncio.new_event_loop()
                t = threading.Thread(target=_edge_loop.run_forever, daemon=True)
                t.start()
    return _edge_loop

def generate_edge(text, voice="en-US-JennyNeural"):
    import asyncio
    import edge_tts
    import miniaudio

    async def _synth():
        communicate = edge_tts.Communicate(text, voice)
        mp3_bytes = b""
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                mp3_bytes += chunk["data"]
        return mp3_bytes

    loop = _get_edge_loop()
    future = asyncio.run_coroutine_threadsafe(_synth(), loop)
    mp3_bytes = future.result(timeout=30)
    if not mp3_bytes:
        raise RuntimeError("Edge TTS returned no audio")
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    try:
        tmp.write(mp3_bytes)
        tmp.close()
        decoded = miniaudio.decode_file(
            tmp.name, output_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=1, sample_rate=24000,
        )
        audio = np.frombuffer(decoded.samples, dtype=np.int16).astype(np.float32) / 32768.0
        return audio, 24000
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


_kokoro_model = None
_kokoro_lock = threading.Lock()
KOKORO_ONNX = os.path.join(MODELS_DIR, "kokoro-v1.0.onnx")
KOKORO_VOICES = os.path.join(MODELS_DIR, "voices-v1.0.bin")

def _get_kokoro():
    global _kokoro_model
    if _kokoro_model is None:
        with _kokoro_lock:
            if _kokoro_model is None:
                if not os.path.exists(KOKORO_ONNX):
                    raise RuntimeError(
                        f"Kokoro model not found at {KOKORO_ONNX}. "
                        "Run /voice-setup-kokoro to download it."
                    )
                import kokoro_onnx
                _kokoro_model = kokoro_onnx.Kokoro(KOKORO_ONNX, KOKORO_VOICES)
    return _kokoro_model

def generate_kokoro(text, voice="af_heart"):
    kokoro = _get_kokoro()
    with _kokoro_lock:
        samples, sr = kokoro.create(text, voice=voice, speed=1.0)
    return samples, sr


# ==============================================================
#  STT — push-to-talk
# ==============================================================

class PushToTalkRecorder:
    def __init__(self, keys=None, sr=16000, mixer=None, on_key="duck", device=None,
                 on_record_start=None):
        if keys is None:
            keys = ["page down"]
        self.keys = keys if isinstance(keys, list) else [keys]
        self.sr = sr
        self.mixer = mixer
        self.on_key = on_key
        self.device = device
        self.on_record_start = on_record_start
        self._chunks = []
        self._recording = False
        self._scan_codes = set()

    def _resolve_scan_codes(self):
        """Resolve key names to scan codes."""
        import keyboard
        for k in self.keys:
            try:
                codes = keyboard.key_to_scan_codes(k)
                self._scan_codes.update(codes)
            except ValueError:
                pass

    def _is_ptt_key(self, event):
        return event.scan_code in self._scan_codes

    def _mic_cb(self, indata, _frames, _time, _status):
        if self._recording:
            self._chunks.append(indata.copy())

    def wait_and_record(self):
        import keyboard
        if not self._scan_codes:
            self._resolve_scan_codes()
        self._chunks = []
        pressed = threading.Event()
        released = threading.Event()

        def on_press(e):
            if self._is_ptt_key(e):
                print(f"  [PTT] Key down: {e.name} scan={e.scan_code}", flush=True)
                pressed.set()

        def on_release(e):
            if pressed.is_set() and self._is_ptt_key(e):
                print(f"  [PTT] Key up: {e.name} scan={e.scan_code}", flush=True)
                released.set()

        press_hook = keyboard.on_press(on_press)
        release_hook = keyboard.on_release(on_release)
        pressed.wait()
        keyboard.unhook(press_hook)

        if self.on_record_start:
            self.on_record_start()

        if self.mixer:
            if self.on_key == "duck":
                self.mixer.duck()
            else:
                self.mixer.mute()

        self._recording = True
        stream = sd.InputStream(
            samplerate=self.sr, channels=1, dtype="int16",
            blocksize=1024, callback=self._mic_cb, device=self.device,
        )
        stream.start()

        if not released.wait(timeout=30):
            print("  [PTT] Release timeout (30s) — forcing stop", flush=True)
        keyboard.unhook(release_hook)

        self._recording = False
        stream.stop()
        stream.close()

        if self.mixer:
            if self.on_key == "duck":
                self.mixer.unduck()
            else:
                self.mixer.unmute()

        if not self._chunks:
            return None
        return np.concatenate(self._chunks)


_whisper_model = None
_whisper_lock = threading.Lock()

def _get_whisper():
    global _whisper_model
    if _whisper_model is None:
        with _whisper_lock:
            if _whisper_model is None:
                from faster_whisper import WhisperModel
                try:
                    _whisper_model = WhisperModel("base.en", device="cuda", compute_type="float16")
                except Exception:
                    _whisper_model = WhisperModel("base.en", device="cpu", compute_type="int8")
    return _whisper_model


def transcribe(audio_i16, sr=16000):
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    try:
        with wave.open(tmp, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(audio_i16.tobytes())
        tmp.close()
        model = _get_whisper()
        segments, _ = model.transcribe(tmp.name, language="en", beam_size=3, vad_filter=True)
        return " ".join(seg.text.strip() for seg in segments).strip()
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


# ==============================================================
#  Tool summary / narration helpers
# ==============================================================

def _tool_summary(name, inp):
    if name == "Bash":
        return inp.get("command", "")[:80]
    if name == "Read":
        return inp.get("file_path", "")
    if name in ("Edit", "Write"):
        return inp.get("file_path", "")
    if name == "Grep":
        return inp.get("pattern", "")
    if name == "Glob":
        return inp.get("pattern", "")
    if name == "Task":
        return inp.get("description", "")[:60]
    if name == "WebSearch":
        return inp.get("query", "")[:60]
    if name == "WebFetch":
        return inp.get("url", "")[:60]
    if name == "TodoWrite":
        return f"{len(inp.get('todos', []))} items"
    return json.dumps(inp)[:60]


def _tool_narration(name, inp):
    if name == "Read":
        return f"Reading {_basename(inp.get('file_path', 'file'))}"
    if name == "Edit":
        return f"Editing {_basename(inp.get('file_path', 'file'))}"
    if name == "Write":
        return f"Writing {_basename(inp.get('file_path', 'file'))}"
    if name == "Bash":
        cmd = inp.get("command", "")
        first = cmd.split()[0] if cmd.split() else "command"
        first = os.path.basename(first)
        return f"Running {first}"
    if name == "Grep":
        return f"Searching for {inp.get('pattern', '')[:40]}"
    if name == "Glob":
        return f"Finding files matching {inp.get('pattern', '')[:40]}"
    if name == "Task":
        return f"Launching agent: {inp.get('description', 'task')[:50]}"
    if name == "WebSearch":
        return f"Searching the web for {inp.get('query', '')[:50]}"
    if name == "WebFetch":
        return "Fetching a web page"
    if name == "TodoWrite":
        return f"Updating task list, {len(inp.get('todos', []))} items"
    if name == "NotebookEdit":
        return "Editing notebook"
    return f"Using {name}"


# ==============================================================
#  Instance registry
# ==============================================================

class Instance:
    """A registered Claude Code instance."""

    def __init__(self, session_id, name="", pid=0, hwnd=0):
        self.session_id = session_id
        self.name = name or session_id[:8]
        self.pid = pid
        self.hwnd = hwnd
        self.muted = True
        self.last_seen = time.monotonic()
        self._last_intermediate_text = ""
        self._intermediate_lock = threading.Lock()
        self._last_stop_text = ""
        self._stop_lock = threading.Lock()
        self._transcript_size_at_stop = 0
        self.active = False
        self._primed = False
        # Byte offset up to which the secret tripwire has already scanned the
        # transcript, so each scan only covers new content.
        self._scan_offset = 0


REGISTRY_FILE = os.path.join(PLUGIN_DATA, ".voice_registry.json")


class InstanceRegistry:
    """Thread-safe registry of connected Claude instances."""

    def __init__(self):
        self._instances: dict[str, Instance] = {}
        self._lock = threading.Lock()

    def _save(self):
        """Persist current registry to disk (call while holding _lock)."""
        try:
            data = []
            for sid, inst in self._instances.items():
                data.append({
                    "session_id": sid,
                    "name": inst.name,
                    "pid": inst.pid,
                    "hwnd": inst.hwnd,
                    "muted": inst.muted,
                })
            with open(REGISTRY_FILE, "w") as f:
                json.dump(data, f)
        except Exception:
            pass

    def load_persisted(self):
        """Load instances from disk, keeping only those with live PIDs."""
        print(f"  [Registry] Looking for {REGISTRY_FILE}")
        if not os.path.exists(REGISTRY_FILE):
            print(f"  [Registry] File not found")
            return
        try:
            with open(REGISTRY_FILE, "r") as f:
                data = json.load(f)
        except Exception as e:
            print(f"  [Registry] Parse error: {e}")
            return
        print(f"  [Registry] Found {len(data)} entries in file")
        with self._lock:
            for entry in data:
                pid = entry.get("pid", 0)
                hwnd = entry.get("hwnd", 0)
                name = entry.get("name", "")
                hwnd_valid = self._hwnd_alive(hwnd) if hwnd else False
                pid_alive = self._pid_alive(pid) if pid else False
                print(f"  [Registry]   {name} pid={pid}(alive={pid_alive}) hwnd={hwnd}(valid={hwnd_valid})")
                if not hwnd_valid and not pid_alive:
                    continue
                sid = entry.get("session_id", "")
                if not sid or sid in self._instances:
                    continue
                inst = Instance(sid,
                                name=entry.get("name", ""),
                                pid=pid,
                                hwnd=entry.get("hwnd", 0))
                inst.muted = entry.get("muted", True)
                self._instances[sid] = inst
            if self._instances:
                print(f"  Restored {len(self._instances)} instance(s) from registry")

    @staticmethod
    def _pid_alive(pid):
        if not pid:
            return True
        if sys.platform == "win32":
            import ctypes
            import ctypes.wintypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            try:
                # OpenProcess alone is NOT enough: it still succeeds for a
                # *zombie* — a process that has exited but whose PID entry is
                # kept alive by an outstanding handle. Such entries would
                # otherwise linger in the instance list forever. Only a
                # STILL_ACTIVE exit code means genuinely running.
                code = ctypes.wintypes.DWORD()
                if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return True  # can't tell — assume alive, don't drop it
                return code.value == STILL_ACTIVE
            finally:
                k32.CloseHandle(h)
        else:
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False

    @staticmethod
    def _hwnd_alive(hwnd):
        """Check if a window handle is still valid (Windows only)."""
        if not hwnd:
            return False
        if sys.platform == "win32":
            import ctypes
            return bool(ctypes.windll.user32.IsWindow(hwnd))
        return False

    def register(self, session_id, name="", pid=0, hwnd=0):
        with self._lock:
            if session_id in self._instances:
                inst = self._instances[session_id]
                inst.name = name or inst.name
                inst.pid = pid or inst.pid
                inst.hwnd = hwnd or inst.hwnd
                inst.last_seen = time.monotonic()
            else:
                inst = Instance(session_id, name, pid, hwnd)
                self._instances[session_id] = inst
                if len(self._instances) == 1:
                    inst.muted = False
            self._save()
            return inst

    def deregister_by_pid(self, pid):
        with self._lock:
            to_remove = [sid for sid, i in self._instances.items() if i.pid == pid]
            for sid in to_remove:
                del self._instances[sid]

    def deregister(self, session_id):
        with self._lock:
            self._instances.pop(session_id, None)

    def get(self, session_id) -> Instance | None:
        with self._lock:
            return self._instances.get(session_id)

    def get_or_register(self, session_id, name="", pid=0, hwnd=0) -> Instance:
        """Get instance or auto-register it.

        Migrates a pre-registered instance (keyed by "pid-X") to the real
        session_id from the first hook event.
        """
        with self._lock:
            if session_id not in self._instances:
                pre_key = None
                if name:
                    for sid, inst in self._instances.items():
                        if sid.startswith("pid-") and inst.name == name:
                            pre_key = sid
                            break
                if not pre_key and pid:
                    pid_key = f"pid-{pid}"
                    if pid_key in self._instances:
                        pre_key = pid_key

                if pre_key:
                    inst = self._instances.pop(pre_key)
                    inst.session_id = session_id
                    inst.last_seen = time.monotonic()
                    if name:
                        inst.name = name
                    if pid and not inst.pid:
                        inst.pid = pid
                    if hwnd and not inst.hwnd:
                        inst.hwnd = hwnd
                    self._instances[session_id] = inst
                    self._save()
                    return inst

                inst = Instance(session_id, name=name, pid=pid, hwnd=hwnd)
                self._instances[session_id] = inst
                if len(self._instances) == 1:
                    inst.muted = False
                self._save()
                return inst
            inst = self._instances[session_id]
            inst.last_seen = time.monotonic()
            changed = False
            if name and inst.name == session_id[:8]:
                inst.name = name
                changed = True
            if pid and not inst.pid:
                inst.pid = pid
                changed = True
            if hwnd and not inst.hwnd:
                inst.hwnd = hwnd
                changed = True
            if changed:
                self._save()
            return inst

    def get_unmuted(self) -> Instance | None:
        with self._lock:
            for inst in self._instances.values():
                if not inst.muted:
                    return inst
            return None

    def unmute(self, session_id):
        """Unmute one instance, mute all others."""
        with self._lock:
            for sid, inst in self._instances.items():
                inst.muted = (sid != session_id)
            self._save()

    def mute(self, session_id):
        with self._lock:
            inst = self._instances.get(session_id)
            if inst:
                inst.muted = True
                self._save()

    def mute_all(self):
        with self._lock:
            for inst in self._instances.values():
                inst.muted = True
            self._save()

    def toggle_unmuted(self):
        """Toggle mute on the currently unmuted instance."""
        with self._lock:
            for inst in self._instances.values():
                if not inst.muted:
                    inst.muted = True
                    return True
            return None

    def all_instances(self) -> list[Instance]:
        with self._lock:
            return list(self._instances.values())

    def prune_dead(self):
        """Remove instances whose PID and HWND are both dead."""
        with self._lock:
            dead = []
            for sid, inst in self._instances.items():
                pid_ok = self._pid_alive(inst.pid) if inst.pid else False
                hwnd_ok = self._hwnd_alive(inst.hwnd) if inst.hwnd else False
                if not pid_ok and not hwnd_ok:
                    dead.append(sid)
            for sid in dead:
                del self._instances[sid]
            return dead

    def instance_count(self):
        with self._lock:
            return len(self._instances)


# ==============================================================
#  Clipboard paste helper (Windows only)
# ==============================================================

def _clipboard_paste(text):
    import keyboard
    import ctypes

    CF_UNICODETEXT = 13
    k32 = ctypes.windll.kernel32
    u32 = ctypes.windll.user32

    old_clip = None
    if u32.OpenClipboard(0):
        h = u32.GetClipboardData(CF_UNICODETEXT)
        if h:
            p = k32.GlobalLock(h)
            if p:
                old_clip = ctypes.c_wchar_p(p).value
                k32.GlobalUnlock(h)
        u32.CloseClipboard()

    if u32.OpenClipboard(0):
        u32.EmptyClipboard()
        encoded = text.encode('utf-16-le') + b'\x00\x00'
        h = k32.GlobalAlloc(0x0042, len(encoded))
        p = k32.GlobalLock(h)
        ctypes.memmove(p, encoded, len(encoded))
        k32.GlobalUnlock(h)
        u32.SetClipboardData(CF_UNICODETEXT, h)
        u32.CloseClipboard()

    keyboard.press_and_release('ctrl+v')
    time.sleep(0.05)

    if old_clip is not None:
        if u32.OpenClipboard(0):
            u32.EmptyClipboard()
            encoded = old_clip.encode('utf-16-le') + b'\x00\x00'
            h = k32.GlobalAlloc(0x0042, len(encoded))
            p = k32.GlobalLock(h)
            ctypes.memmove(p, encoded, len(encoded))
            k32.GlobalUnlock(h)
            u32.SetClipboardData(CF_UNICODETEXT, h)
            u32.CloseClipboard()


def _focus_window(hwnd):
    """Bring a window to the foreground by its handle."""
    if not hwnd:
        print(f"  [PTT] No HWND to focus")
        return False
    try:
        import ctypes
        u32 = ctypes.windll.user32
        u32.keybd_event(0x12, 0, 0, 0)
        u32.keybd_event(0x12, 0, 2, 0)
        u32.ShowWindow(hwnd, 9)
        result = u32.SetForegroundWindow(hwnd)
        if not result:
            print(f"  [PTT] SetForegroundWindow({hwnd}) returned 0")
        return bool(result)
    except Exception as exc:
        print(f"  [PTT] Focus error: {exc}")
        return False


# ==============================================================
#  Transcript reading helpers
# ==============================================================

def _extract_last_response(transcript_path):
    """Extract text from the last assistant turn (after final tool result)."""
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    try:
        turn_texts = []
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                ev = json.loads(line)
                if ev.get("type") == "assistant":
                    blocks = ev.get("message", {}).get("content", [])
                    for b in blocks:
                        if isinstance(b, dict) and b.get("type") == "text":
                            turn_texts.append(b.get("text", ""))
                elif ev.get("type") in ("user", "result"):
                    turn_texts = []
        return " ".join(t.strip() for t in turn_texts if t.strip())
    except Exception:
        return ""


def _extract_latest_assistant_text(transcript_path):
    """Extract the most recent assistant text block (for intermediate narration)."""
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    try:
        latest_text = ""
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                ev = json.loads(line)
                if ev.get("type") == "assistant":
                    blocks = ev.get("message", {}).get("content", [])
                    for b in blocks:
                        if isinstance(b, dict) and b.get("type") == "text":
                            t = b.get("text", "").strip()
                            if t:
                                latest_text = t
        return latest_text
    except Exception:
        return ""


# ==============================================================
#  Overlay notifier (UDP sender)
# ==============================================================

class OverlayNotifier:
    def __init__(self, port):
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(self, **kwargs):
        try:
            data = json.dumps(kwargs).encode("utf-8")
            self.sock.sendto(data, ("127.0.0.1", self.port))
        except Exception:
            pass

    def recording(self):
        self.send(state="recording")

    def transcribing(self):
        self.send(state="transcribing")

    def transcribed(self, text):
        self.send(state="transcribed", text=text)

    def speaking(self):
        self.send(state="speaking")

    def speaking_done(self):
        self.send(state="speaking_done")

    def paused(self):
        self.send(state="paused")

    def resumed(self):
        self.send(state="resumed")

    def idle(self):
        self.send(state="idle")

    def muted(self, is_muted):
        self.send(state="muted", muted=is_muted)

    def tool(self, name, summary, instance_name=""):
        self.send(state="tool", name=name, summary=summary, instance=instance_name)

    def error(self, message):
        self.send(state="error", message=message)

    def skipped(self, did_skip=True):
        """Flash a 'skipped' indicator. did_skip=False means nothing was
        playing/queued, so the keypress registered but had nothing to skip."""
        self.send(state="skipped", did_skip=did_skip)

    def narrate(self, is_on):
        """Tool narration turned on/off. Distinct from mute: response speech
        keeps playing either way."""
        self.send(state="narrate", narrate=is_on)

    def alarm_voice(self, is_on):
        """Spoken security alarm turned on/off. The incident panel and the
        durable record are unaffected — only the speech is silenced."""
        self.send(state="alarm_voice", alarm_voice=is_on)

    def alarm_panel(self, incidents):
        """Persistent per-incident security panel. `incidents` is a list of
        {id, names, instance, when} dicts, newest first. Empty list = none.
        Not cleared by other states; only explicit acknowledgement empties it."""
        self.send(state="alarm_panel", incidents=incidents)

    def loading(self, model):
        self.send(state="loading", model=model)

    def ready(self, model):
        self.send(state="ready", model=model)

    def heartbeat(self):
        self.send(state="heartbeat")

    def shutdown(self):
        self.send(state="shutdown")

    def instances(self, instance_list):
        self.send(state="instances", instances=instance_list)

    def init(self, backend, key, devices=None, cmd_port=0, narrate=True,
             alarm_voice=True):
        self.send(state="init", backend=backend, key=key,
                  devices=devices or {}, cmd_port=cmd_port, narrate=narrate,
                  alarm_voice=alarm_voice)


_overlay: OverlayNotifier | None = None

def _get_overlay():
    return _overlay


# ==============================================================
#  Event handler
# ==============================================================

class EventHandler:
    """Handles hook events from all Claude instances."""

    def __init__(self, mixer, registry, backend="edge",
                 narrate_tools=True, voice_enabled=True, alarm_voice=True):
        self.mixer = mixer
        self.registry = registry
        self.backend = backend
        self.narrate_tools = narrate_tools
        self.voice_enabled = voice_enabled
        # Gates ONLY the spoken security alarm. The log line, the durable
        # record and the visual panel are never silenced — muting the speaker
        # must not be able to hide that a secret leaked.
        self.alarm_voice = alarm_voice

        # Secret tripwire: scans transcript deltas for any real secret VALUE.
        # Optional — degrades to "no scan" when no secret source is configured.
        self._secret_alarm = SecretAlarm()
        self._alarm_lock = threading.Lock()

        self._tts_q = queue.Queue()
        self._tts_cancelled = threading.Event()  # cancel whole item + drain
        self._tts_skip = threading.Event()       # skip ONE sentence, keep going
        self._tts_paused = threading.Event()
        self._tts_worker = threading.Thread(target=self._tts_loop, daemon=True)
        self._tts_worker.start()

        self._narrate_q = queue.Queue()
        self._narrate_worker = threading.Thread(target=self._narrate_loop, daemon=True)
        self._narrate_worker.start()

    def set_narrate_tools(self, enabled):
        """Turn tool narration (the announcer voice) on/off at runtime.

        Independent of response speech: the 'voice' channel is untouched, so
        turning narration off leaves responses reading normally. Turning it off
        also drops queued narration and silences the 'tools' channel so an
        in-flight announcement stops immediately rather than finishing.
        """
        self.narrate_tools = bool(enabled)
        if not self.narrate_tools:
            while not self._narrate_q.empty():
                try:
                    self._narrate_q.get_nowait()
                except queue.Empty:
                    break
            try:
                self.mixer.reset_channel("tools")
            except Exception:
                pass
        return self.narrate_tools

    def set_alarm_voice(self, enabled):
        """Turn the SPOKEN security alarm on/off at runtime.

        Only the speech is affected: the stderr log line, the durable alarm
        record and the overlay's incident panel still fire, so a leak is never
        silently missed — it just stops shouting.
        """
        self.alarm_voice = bool(enabled)
        return self.alarm_voice

    def skip_current(self):
        """Skip the currently-playing SENTENCE and advance to the next one
        WITHOUT draining the queue (that's mute/_kill_all_audio).

        Uses a dedicated _tts_skip flag (NOT _tts_cancelled, which would
        abandon the entire remaining response).
        """
        if not (self.mixer.is_playing("voice") or not self._tts_q.empty()):
            return False
        self._tts_skip.set()
        if self._tts_paused.is_set():
            self._tts_paused.clear()
            self.mixer.unpause("voice")
        self._fade_and_clear_voice(fade_secs=0.3)
        return True

    def _fade_and_clear_voice(self, fade_secs=0.5):
        if self.mixer.is_playing("voice"):
            self.mixer.fade_channel("voice", fade_secs)
            time.sleep(fade_secs)
        self.mixer.reset_channel("voice")

    def _generate_tts(self, text):
        if self.backend == "kokoro":
            return generate_kokoro(text)
        elif self.backend == "edge":
            return generate_edge(text)
        elif self.backend == "elevenlabs":
            return generate_elevenlabs(text)
        else:
            return generate_edge(text)  # fallback to edge

    def _wait_if_paused(self):
        while self._tts_paused.is_set() and not self._tts_cancelled.is_set():
            time.sleep(0.05)

    def _wait_channel_pausable(self, name):
        """Like mixer.wait_channel but respects pause, cancel, and skip."""
        while self.mixer.channels[name].has_data():
            if self._tts_cancelled.is_set() or self._tts_skip.is_set():
                return
            time.sleep(0.03)

    def _tts_loop(self):
        while True:
            item = self._tts_q.get()
            if item is None:
                break
            text_or_list, is_final = item
            if not self.voice_enabled:
                self._tts_q.task_done()
                continue
            try:
                self._tts_cancelled.clear()
                self._tts_skip.clear()
                if is_final:
                    sentences = text_or_list if isinstance(text_or_list, list) else [text_or_list]
                    for sentence in sentences:
                        if self._tts_cancelled.is_set():
                            break
                        self._wait_if_paused()
                        if self._tts_cancelled.is_set():
                            break
                        s = sentence.strip()
                        if not s:
                            continue
                        audio, sr = self._generate_tts(s)
                        if self._tts_cancelled.is_set():
                            break
                        self._fade_and_clear_voice(fade_secs=0.5)
                        self.mixer.play("voice", audio, sr=sr)
                        # Returns early if skip OR cancel is set. On skip, clear
                        # the flag and fall through to the next sentence (the
                        # crossfade out was already done by skip_current()).
                        self._wait_channel_pausable("voice")
                        if self._tts_skip.is_set():
                            self._tts_skip.clear()
                else:
                    text = text_or_list if isinstance(text_or_list, str) else " ".join(text_or_list)
                    text = text.strip()
                    if not text:
                        self._tts_q.task_done()
                        continue
                    audio, sr = self._generate_tts(text)
                    if not self._tts_cancelled.is_set():
                        self._fade_and_clear_voice(fade_secs=0.5)
                        self.mixer.play("voice", audio, sr=sr)
            except Exception as exc:
                sys.stderr.write(f"[TTS error: {exc}]\n")
            self._tts_q.task_done()

    def _narrate_loop(self):
        """Tool narration worker — uses kokoro (male voice) if available, else edge."""
        while True:
            narration = self._narrate_q.get()
            # Re-check at dequeue time: narration may have been toggled off
            # while this item sat in the queue or while TTS was generating.
            if not self.narrate_tools:
                continue
            try:
                try:
                    audio, sr = generate_kokoro(narration, voice="am_michael")
                except Exception:
                    audio, sr = generate_edge(narration, voice="en-US-GuyNeural")
                self.mixer.play("tools", audio, sr=sr)
            except Exception:
                pass

    def handle(self, event, broadcast_fn=None):
        """Dispatch a hook event."""
        hook = event.get("hook_event_name", "")
        session_id = event.get("session_id", "")
        inst_name = event.get("_instance_name", "")
        inst_pid = event.get("_instance_pid", 0)
        inst_hwnd = event.get("_instance_hwnd", 0)

        was_new = self.registry.get(session_id) is None
        inst = self.registry.get_or_register(session_id, name=inst_name,
                                              pid=inst_pid, hwnd=inst_hwnd)
        if was_new and broadcast_fn:
            print(f"  ~ Linked: {inst.name} -> session {session_id[:8]} (muted={inst.muted})")
            broadcast_fn()

        # Prime the tripwire's scan offset on first sight of a session so a
        # resumed session's PRIOR transcript isn't retro-scanned and alarmed.
        if not inst._primed:
            tp = event.get("transcript_path", "")
            if tp:
                try:
                    inst._scan_offset = os.path.getsize(tp)
                except OSError:
                    inst._scan_offset = 0
            inst._primed = True

        # Secret tripwire — scan the transcript delta on every event. Runs
        # regardless of mute (a leak matters even on a muted instance).
        self._scan_for_secrets(event, inst)

        if hook == "PreToolUse":
            if not inst.active:
                inst.active = True
                if broadcast_fn:
                    broadcast_fn()
            threading.Thread(target=self._on_tool_use,
                             args=(event, session_id), daemon=True).start()
        elif hook == "Stop":
            inst.active = False
            if broadcast_fn:
                broadcast_fn()
            threading.Thread(target=self._on_stop,
                             args=(event, session_id), daemon=True).start()

    def _on_tool_use(self, event, session_id):
        tool_name = event.get("tool_name", "?")
        tool_input = event.get("tool_input", {})
        transcript_path = event.get("transcript_path", "")

        inst = self.registry.get_or_register(session_id)

        if inst.muted:
            return

        # Play tone
        self.mixer.play("effects", make_tool_tone(tool_name))

        # Notify overlay
        overlay = _get_overlay()
        short = _tool_summary(tool_name, tool_input)
        if overlay:
            overlay.tool(tool_name, short, instance_name=inst.name)

        # Speak intermediate assistant text
        if self.voice_enabled and transcript_path:
            try:
                cur_size = os.path.getsize(transcript_path)
            except OSError:
                cur_size = 0
            if cur_size > inst._transcript_size_at_stop:
                raw = _extract_latest_assistant_text(transcript_path)
                if raw:
                    with inst._intermediate_lock:
                        if raw != inst._last_intermediate_text:
                            inst._last_intermediate_text = raw
                            text = _strip_markdown(raw)
                            if text:
                                print(f"  [INT] Queueing: {text[:80]}")
                                self._tts_q.put((text, False))
                        else:
                            print(f"  [INT] Dedup skip: {raw[:60]}")
            else:
                print(f"  [INT] Stale skip: size={cur_size} stop={inst._transcript_size_at_stop}")

        # Narrate tool call
        if self.narrate_tools:
            narration = _tool_narration(tool_name, tool_input)
            while not self._narrate_q.empty():
                try:
                    self._narrate_q.get_nowait()
                except queue.Empty:
                    break
            self._narrate_q.put(narration)

    def _on_stop(self, event, session_id):
        transcript_path = event.get("transcript_path", "")
        overlay = _get_overlay()

        if self._tts_paused.is_set():
            self._tts_paused.clear()
            self.mixer.unpause("voice")

        inst = self.registry.get_or_register(session_id)

        if transcript_path:
            try:
                inst._transcript_size_at_stop = os.path.getsize(transcript_path)
            except OSError:
                pass
            latest = _extract_latest_assistant_text(transcript_path)
            if latest:
                inst._last_intermediate_text = latest

        if inst.muted:
            # Play a beep for muted instances
            self.mixer.play("effects", make_beep())
            return

        raw = _extract_last_response(transcript_path)
        print(f"  [Stop] Response for {inst.name}: {(raw or '(none)')[:80]}")

        with inst._stop_lock:
            if raw and raw == inst._last_stop_text:
                print(f"  [Stop] Skipping stale response for {inst.name}")
                self.mixer.play("effects", make_result_chime())
                return
            if raw:
                inst._last_stop_text = raw

        text = raw
        if text and self.voice_enabled:
            text = _strip_markdown(text)
            if not text:
                self.mixer.play("effects", make_result_chime())
                return
            if overlay:
                overlay.speaking()

            parts = SENTENCE_END.split(text)
            sentences = [s.strip() for s in parts if s.strip()]
            if sentences:
                self._tts_q.put((sentences, True))
                self._tts_q.join()
            if overlay:
                overlay.speaking_done()

        self.mixer.play("effects", make_result_chime())

    def _scan_for_secrets(self, event, inst):
        """Scan the transcript delta for leaked secret VALUES; alarm on a hit.

        Runs on every handled event. Cheap: scans only bytes added since the
        last scan. A hit means a secret was emitted raw (not by name).

        Opt-in: with no secret source configured this is a no-op, so the
        plugin behaves exactly as before for users who don't use it.
        """
        path = event.get("transcript_path", "")
        if not path:
            return
        try:
            with self._alarm_lock:
                hits, new_off = self._secret_alarm.scan_transcript_tail(
                    path, inst._scan_offset)
                inst._scan_offset = new_off
            if hits:
                self._fire_secret_alarm(hits, inst)
        except Exception as exc:
            # Never let the tripwire disturb the service.
            sys.stderr.write(f"[secret-alarm scan error: {exc}]\n")

    def _fire_secret_alarm(self, names, inst):
        """A secret VALUE was found in the transcript. Alert by UI + voice.

        Reports NAMES only, never values. Bypasses mute so the alert is heard
        regardless of which instance is unmuted.
        """
        joined = ", ".join(names)
        # 1) Hard log line (deterministic record).
        sys.stderr.write(
            f"\n*** SECRET ALARM *** value(s) for [{joined}] found in "
            f"transcript of '{inst.name}' — rotate immediately ***\n")
        # 2) Durable record — survives restart, persists until acknowledged.
        try:
            secret_alarm_store.record_alarm(names, inst.name, when=time.time())
        except Exception:
            pass
        # 3) Visual: push the full per-incident panel.
        overlay = _get_overlay()
        if overlay:
            self._push_alarm_panel(overlay)
        # 4) Audible — suppressible. The incident is already logged, recorded
        #    and shown above, so silencing the speaker loses no evidence.
        if not self.alarm_voice:
            return
        try:
            n = len(names)
            spoken = (f"Security alarm. {n} secret value"
                      f"{'s' if n != 1 else ''} exposed in transcript. "
                      f"{joined.replace('_', ' ').lower()}.")
            audio, sr = self._generate_tts(spoken)
            self.mixer.play("voice", audio, sr=sr)
        except Exception as exc:
            sys.stderr.write(f"[secret-alarm voice error: {exc}]\n")
            self.mixer.play("effects", make_beep())

    def _push_alarm_panel(self, overlay):
        """Send the full per-incident alarm list to the overlay (newest first).

        Sends NAMES + instance + timestamp + stable id — never values.
        """
        try:
            active = secret_alarm_store.active_alarms()
        except Exception:
            active = []
        incidents = [{
            "id": e.get("id", ""),
            "names": e.get("names", []),
            "instance": e.get("instance", ""),
            "when": e.get("when", 0),
        } for e in active]
        try:
            overlay.alarm_panel(incidents)
        except Exception:
            pass


# ==============================================================
#  Main
# ==============================================================

def main():
    ap = argparse.ArgumentParser(description="Voice Service for Claude Code")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"UDP port for hook events (default: {DEFAULT_PORT})")
    ap.add_argument("--key", action="append", default=None,
                    help="Push-to-talk key (repeatable, default: page down + insert)")
    ap.add_argument("--backend", default="edge",
                    choices=["kokoro", "edge", "elevenlabs"],
                    help="TTS backend (default: edge)")
    ap.add_argument("--on-key", default="duck", choices=["duck", "mute"],
                    help="Audio behaviour while PTT is held")
    ap.add_argument("--mute-key", default="page up",
                    help="Key to toggle mute (default: page up)")
    ap.add_argument("--pause-key", default="scroll lock",
                    help="Key to pause/resume voice playback (default: scroll lock)")
    ap.add_argument("--skip-key", default="ctrl+shift+.",
                    help="Key to skip the current sentence and advance to the "
                         "next (default: ctrl+shift+.)")
    ap.add_argument("--narrate-key", default="ctrl+shift+n",
                    help="Key to toggle tool narration on/off without "
                         "affecting response speech (default: ctrl+shift+n)")
    ap.add_argument("--alarm-key", default="ctrl+shift+j",
                    help="Key to toggle the SPOKEN security alarm. The log "
                         "and visual panel always fire (default: ctrl+shift+j)")
    ap.add_argument("--exit-key", default="ctrl+end",
                    help="Key to exit (default: ctrl+end)")
    ap.add_argument("--no-voice", action="store_true",
                    help="Disable TTS (tones only)")
    ap.add_argument("--voice-out", action="store_true",
                    help="Listen-only: TTS output, NO mic input — skips ASR "
                         "load and push-to-talk entirely (implies --no-ptt)")
    ap.add_argument("--no-ptt", action="store_true",
                    help="Disable push-to-talk input (and skip ASR preload)")
    ap.add_argument("--no-narrate", action="store_true",
                    help="Start with tool narration off (toggle: --narrate-key)")
    ap.add_argument("--no-alarm-voice", action="store_true",
                    help="Start with the spoken security alarm off. Incidents "
                         "are still logged and shown on the panel.")
    ap.add_argument("--voice-vol", type=float, default=1.0)
    ap.add_argument("--tools-vol", type=float, default=0.55)
    ap.add_argument("--effects-vol", type=float, default=0.35)
    ap.add_argument("--output-device", default=None)
    ap.add_argument("--input-device", default=None)
    ap.add_argument("--no-overlay", action="store_true")
    args = ap.parse_args()

    if args.key is None:
        args.key = ["page down", "insert"]

    # --voice-out is listen-only: it implies no push-to-talk (which in turn
    # skips the ASR model load entirely).
    if args.voice_out:
        args.no_ptt = True

    # ── Device selection ─────────────────────────────────────
    out_dev = None
    if args.output_device is not None:
        try:
            out_dev = int(args.output_device)
        except ValueError:
            out_dev = find_device(args.output_device, kind='output')
            if out_dev is None:
                print(f"Error: no output device matching '{args.output_device}'")
                sys.exit(1)

    in_dev = None
    if args.input_device is not None:
        try:
            in_dev = int(args.input_device)
        except ValueError:
            in_dev = find_device(args.input_device, kind='input')
            if in_dev is None:
                print(f"Error: no input device matching '{args.input_device}'")
                sys.exit(1)

    # ── Build mixer ──────────────────────────────────────────
    mixer = AudioMixer(sr=MIXER_SR, device=out_dev)
    mixer.add_channel("voice",   volume=args.voice_vol)
    mixer.add_channel("tools",   volume=args.tools_vol)
    mixer.add_channel("effects", volume=args.effects_vol)

    # ── Instance registry ────────────────────────────────────
    registry = InstanceRegistry()
    registry.load_persisted()

    # ── Event handler ────────────────────────────────────────
    handler = EventHandler(
        mixer, registry,
        backend=args.backend,
        narrate_tools=not args.no_narrate,
        voice_enabled=not args.no_voice,
        alarm_voice=not args.no_alarm_voice,
    )

    # ── Recorder ─────────────────────────────────────────────
    def _on_record_start():
        _kill_all_audio()
        overlay = _get_overlay()
        if overlay:
            overlay.recording()

    recorder = PushToTalkRecorder(
        keys=args.key, mixer=mixer, on_key="mute", device=in_dev,
        on_record_start=_on_record_start,
    )

    # ── Startup display ──────────────────────────────────────
    print("=" * 42)
    print("     Claude Voice Service")
    print("=" * 42)
    print()
    print(f"  Port        : {args.port}")
    print(f"  Backend     : {args.backend}")
    if args.no_ptt:
        print(f"  Push-to-talk: disabled (listen-only)")
    else:
        ptt_keys_str = " / ".join(f"[{k.upper()}]" for k in args.key)
        print(f"  Push-to-talk: {ptt_keys_str} ({args.on_key})")
    print(f"  Mute toggle : [{args.mute_key.upper()}]")
    print(f"  Pause/Resume: [{args.pause_key.upper()}]")
    print(f"  Skip        : [{args.skip_key.upper()}]")
    print(f"  Tool narrate: [{args.narrate_key.upper()}]  "
          f"(currently {'OFF' if args.no_narrate else 'ON'})")
    print(f"  Alarm voice : [{args.alarm_key.upper()}]  "
          f"(currently {'OFF' if args.no_alarm_voice else 'ON'}; "
          f"log + panel always on)")
    print(f"  Exit        : [{args.exit_key.upper()}]")
    print(f"  Relaunch UI : [CTRL+\\]")
    print(f"  Volumes     : voice={args.voice_vol}  tools={args.tools_vol}  fx={args.effects_vol}")
    print(f"  Data dir    : {PLUGIN_DATA}")
    print()

    # Play startup chime through mixer
    mixer.play("effects", make_startup_chime())

    # ── Launch overlay UI ────────────────────────────────────
    global _overlay

    cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cmd_sock.bind(("127.0.0.1", 0))
    cmd_port = cmd_sock.getsockname()[1]

    def _kill_all_audio():
        """Stop all playback and drain TTS/narration queues."""
        handler._tts_cancelled.set()
        handler._tts_paused.clear()
        mixer.stop_all()
        for ch in mixer.channels.values():
            ch.fade_reset()
            ch.paused = False
        while not handler._tts_q.empty():
            try:
                handler._tts_q.get_nowait()
                handler._tts_q.task_done()
            except queue.Empty:
                break
        while not handler._narrate_q.empty():
            try:
                handler._narrate_q.get_nowait()
            except queue.Empty:
                break

    def command_listener():
        while True:
            try:
                data, _ = cmd_sock.recvfrom(65535)
                cmd = json.loads(data.decode("utf-8", errors="replace"))
                action = cmd.get("action", "")

                if action == "set_output_device":
                    mixer.set_device(cmd.get("device_id"))
                elif action == "set_input_device":
                    recorder.device = cmd.get("device_id")
                elif action == "unmute":
                    registry.unmute(cmd.get("session_id", ""))
                    _kill_all_audio()
                    _broadcast_instances()
                elif action == "mute":
                    registry.mute(cmd.get("session_id", ""))
                    _broadcast_instances()
                elif action == "mute_all":
                    registry.mute_all()
                    _kill_all_audio()
                    _broadcast_instances()
                elif action == "set_narrate":
                    if "enabled" in cmd:
                        on = handler.set_narrate_tools(cmd.get("enabled"))
                        print(f"  [Narrate] Tool announcements "
                              f"{'ON' if on else 'OFF'}", flush=True)
                        ov = _get_overlay()
                        if ov:
                            try:
                                ov.narrate(on)
                            except Exception:
                                pass
                    else:
                        _toggle_narrate()
                elif action == "set_alarm_voice":
                    if "enabled" in cmd:
                        on = handler.set_alarm_voice(cmd.get("enabled"))
                        print(f"  [Alarm] Spoken security alarm "
                              f"{'ON' if on else 'OFF'}", flush=True)
                        ov = _get_overlay()
                        if ov:
                            try:
                                ov.alarm_voice(on)
                            except Exception:
                                pass
                    else:
                        _toggle_alarm_voice()
                elif action == "clear_alarm":
                    cid = cmd.get("id", "")
                    ok = secret_alarm_store.clear_alarm(cid)
                    print(f"  [Security] Clear incident {cid}: "
                          f"{'ok' if ok else 'not found'}")
                    ov = _get_overlay()
                    if ov:
                        handler._push_alarm_panel(ov)
                elif action == "clear_alarms":
                    n = secret_alarm_store.clear_alarms()
                    print(f"  [Security] Cleared {n} acknowledged alarm(s).")
                    ov = _get_overlay()
                    if ov:
                        handler._push_alarm_panel(ov)
            except Exception:
                pass

    threading.Thread(target=command_listener, daemon=True).start()

    def _broadcast_instances():
        """Send current instance list to the overlay."""
        overlay = _get_overlay()
        if overlay:
            instances = []
            for inst in registry.all_instances():
                instances.append({
                    "session_id": inst.session_id,
                    "name": inst.name,
                    "muted": inst.muted,
                    "active": inst.active,
                })
            overlay.instances(instances)

    if not args.no_overlay:
        import subprocess
        overlay_script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "voice_overlay.py")
        output_devs, input_devs = _get_device_list()
        _init_payload = dict(
            backend=args.backend, key=args.key,
            devices={"outputs": output_devs, "inputs": input_devs,
                     "current_output": out_dev, "current_input": in_dev},
            cmd_port=cmd_port,
        )

        def _launch_overlay():
            """Launch (or relaunch) the overlay UI process."""
            global _overlay
            _tmp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            _tmp_sock.bind(("127.0.0.1", 0))
            overlay_port = _tmp_sock.getsockname()[1]
            _tmp_sock.close()

            _overlay = OverlayNotifier(overlay_port)
            subprocess.Popen(
                [sys.executable, overlay_script, str(overlay_port)],
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                              | subprocess.DETACHED_PROCESS,
            )
            print(f"  Overlay     : port {overlay_port}")

            def _send_init_retries():
                for _ in range(5):
                    time.sleep(0.5)
                    # Read toggle state live rather than from startup flags, so
                    # an overlay relaunch after toggling reflects reality.
                    _overlay.init(narrate=handler.narrate_tools,
                                  alarm_voice=handler.alarm_voice,
                                  **_init_payload)
                _broadcast_instances()
                # Re-assert the alarm panel so incidents persist across
                # service AND overlay restarts until explicitly cleared.
                try:
                    handler._push_alarm_panel(_overlay)
                except Exception:
                    pass
            threading.Thread(target=_send_init_retries, daemon=True).start()

        _launch_overlay()

    # ── Pre-load models ──────────────────────────────────────
    def _preload_whisper():
        overlay = _get_overlay()
        if overlay:
            overlay.loading("whisper")
        _get_whisper()
        if overlay:
            overlay.ready("whisper")

    # Listen-only (--voice-out / --no-ptt) never records, so don't pay the
    # STT model load at all.
    if not args.no_ptt:
        threading.Thread(target=_preload_whisper, daemon=True).start()

    # ── Heartbeat ────────────────────────────────────────────
    def heartbeat_thread():
        while True:
            time.sleep(10)
            if _overlay:
                _overlay.heartbeat()

    threading.Thread(target=heartbeat_thread, daemon=True).start()

    # ── Prune dead instances periodically ────────────────────
    def prune_thread():
        while True:
            time.sleep(15)
            try:
                dead = registry.prune_dead()
                if dead:
                    _broadcast_instances()
            except Exception:
                pass

    threading.Thread(target=prune_thread, daemon=True).start()

    # ── Hotkey threads ───────────────────────────────────────
    def exit_key_thread():
        import keyboard
        triggered = threading.Event()

        def _on_exit():
            triggered.set()

        keyboard.add_hotkey(args.exit_key, _on_exit, suppress=False)
        triggered.wait()
        print(f"\n  [{args.exit_key.upper()}] pressed — shutting down voice service.")
        if _overlay:
            _overlay.shutdown()
            time.sleep(0.1)
        os._exit(0)

    def mute_toggle_thread():
        import keyboard
        while True:
            keyboard.wait(args.mute_key, suppress=False)
            registry.toggle_unmuted()
            mixer.stop_all()
            _broadcast_instances()
            time.sleep(0.3)

    def pause_toggle_thread():
        import keyboard
        while True:
            keyboard.wait(args.pause_key, suppress=False)
            overlay = _get_overlay()
            if handler._tts_paused.is_set():
                handler._tts_paused.clear()
                mixer.unpause("voice")
                print("  [Pause] Resumed", flush=True)
                if overlay:
                    overlay.resumed()
            else:
                if mixer.is_playing("voice") or not handler._tts_q.empty():
                    handler._tts_paused.set()
                    mixer.pause("voice")
                    print("  [Pause] Paused", flush=True)
                    if overlay:
                        overlay.paused()
            time.sleep(0.3)

    def skip_key_thread():
        import keyboard
        while True:
            keyboard.wait(args.skip_key, suppress=False)
            did = handler.skip_current()
            print(f"  [Skip] {'Skipped' if did else 'nothing to skip'}",
                  flush=True)
            overlay = _get_overlay()
            if overlay:
                overlay.skipped(did_skip=did)
            time.sleep(0.2)  # debounce

    def _toggle_narrate():
        """Flip tool narration. Response speech is unaffected."""
        on = handler.set_narrate_tools(not handler.narrate_tools)
        print(f"  [Narrate] Tool announcements {'ON' if on else 'OFF'}",
              flush=True)
        overlay = _get_overlay()
        if overlay:
            try:
                overlay.narrate(on)
            except Exception:
                pass
        return on

    def narrate_toggle_thread():
        import keyboard
        while True:
            keyboard.wait(args.narrate_key, suppress=False)
            _toggle_narrate()
            time.sleep(0.3)  # debounce

    def _toggle_alarm_voice():
        """Flip the spoken security alarm. Log/record/panel always stay on."""
        on = handler.set_alarm_voice(not handler.alarm_voice)
        print(f"  [Alarm] Spoken security alarm {'ON' if on else 'OFF'} "
              f"(incidents still logged + shown)", flush=True)
        overlay = _get_overlay()
        if overlay:
            try:
                overlay.alarm_voice(on)
            except Exception:
                pass
        return on

    def alarm_toggle_thread():
        import keyboard
        while True:
            keyboard.wait(args.alarm_key, suppress=False)
            _toggle_alarm_voice()
            time.sleep(0.3)  # debounce

    def voice_thread():
        import keyboard as kb
        while True:
            try:
                overlay = _get_overlay()
                if overlay:
                    overlay.idle()
                print("  [PTT] Waiting for key...", flush=True)
                audio = recorder.wait_and_record()

                target = registry.get_unmuted()
                if target is None:
                    print("  [PTT] No unmuted instance, ignoring")
                    continue
                if audio is None or len(audio) < recorder.sr * 0.3:
                    print("  [PTT] Audio too short, ignoring")
                    continue

                overlay = _get_overlay()
                if overlay:
                    overlay.transcribing()
                text = transcribe(audio, sr=recorder.sr)
                if overlay:
                    overlay.transcribed(text or "")
                if text:
                    print(f"  [PTT] Sending to {target.name} (hwnd={target.hwnd}): {text[:60]}")
                    _focus_window(target.hwnd)
                    time.sleep(0.15)
                    _clipboard_paste(text)
                    time.sleep(0.05)
                    kb.press_and_release('enter')
                    time.sleep(0.2)
            except Exception as exc:
                print(f"  [PTT] Error: {exc}")
                if overlay:
                    overlay.error(str(exc))
                time.sleep(0.5)

    def overlay_relaunch_thread():
        import keyboard
        while True:
            keyboard.wait("ctrl+\\", suppress=False)
            if not args.no_overlay:
                print("  [UI] Relaunching overlay...")
                if _overlay:
                    try:
                        _overlay.shutdown()
                        time.sleep(0.2)
                    except Exception:
                        pass
                _launch_overlay()

    threading.Thread(target=exit_key_thread, daemon=True).start()
    threading.Thread(target=mute_toggle_thread, daemon=True).start()
    threading.Thread(target=pause_toggle_thread, daemon=True).start()
    # Output-only toggles — available in every mode, including listen-only.
    threading.Thread(target=skip_key_thread, daemon=True).start()
    threading.Thread(target=narrate_toggle_thread, daemon=True).start()
    threading.Thread(target=alarm_toggle_thread, daemon=True).start()
    if not args.no_overlay:
        threading.Thread(target=overlay_relaunch_thread, daemon=True).start()
    # Push-to-talk listener only when PTT is enabled. In listen-only mode the
    # mic is never recorded and transcribe() is never called (which would
    # otherwise block on loading the STT model).
    if not args.no_ptt:
        threading.Thread(target=voice_thread, daemon=True).start()

    # ── Main UDP listener ────────────────────────────────────
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", args.port))
    print(f"  Listening   : port {args.port}")
    print(f"\n  Waiting for Claude instances...\n")

    while True:
        try:
            data, _ = sock.recvfrom(65535)
            event = json.loads(data.decode("utf-8", errors="replace"))
            msg_type = event.get("type", "")

            if msg_type == "register":
                session_id = event.get("session_id", "")
                if not session_id:
                    session_id = f"pid-{event.get('pid', 0)}"
                name = event.get("name", "")
                pid = event.get("pid", 0)
                hwnd = event.get("hwnd", 0)
                inst = registry.register(session_id, name, pid, hwnd)
                print(f"  + Registered: {inst.name} (pid={pid}, muted={inst.muted})")
                _broadcast_instances()

            elif msg_type == "deregister":
                pid = event.get("pid", 0)
                registry.deregister_by_pid(pid)
                print(f"  - Deregistered: pid={pid}")
                _broadcast_instances()

            elif msg_type == "shutdown":
                print("  Shutdown signal received.")
                if _overlay:
                    _overlay.shutdown()
                    time.sleep(0.1)
                os._exit(0)

            elif event.get("hook_event_name"):
                handler.handle(event, broadcast_fn=_broadcast_instances)

        except Exception:
            pass


if __name__ == "__main__":
    main()
