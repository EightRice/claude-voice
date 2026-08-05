#!/usr/bin/env python3
"""
secret_alarm — an optional tripwire wired into the voice service.

Scans Claude transcript content for any real secret VALUE loaded from a
`.env`-style file. The NAME of a secret appearing is fine; the VALUE appearing
is the alarm — it means a secret was handled raw instead of by name.

OPT-IN. Point CLAUDE_VOICE_SECRETS at a KEY=VALUE file to enable it:

    set CLAUDE_VOICE_SECRETS=C:\\path\\to\\secrets.env

With no such file configured this degrades silently to "no scan", so the voice
service behaves exactly as if the tripwire did not exist.

Design notes:
  * Values are loaded into memory ONLY to compare against. They are NEVER
    returned, logged or persisted — a hit reports the secret NAME, never the
    value. (Reporting the value would itself be the leak we're detecting.)
  * Short values are skipped to avoid false positives (e.g. a username).
  * This module is import-safe and never raises into the caller; on any error
    it degrades to "no scan" rather than disturbing the voice service.
"""

import os
import json
import threading

# Opt-in secret source: a KEY=VALUE file. Unset by default -> tripwire is off.
DEFAULT_ENV = os.environ.get("CLAUDE_VOICE_SECRETS", "")
_MIN_LEN = 6  # don't scan for trivially short values

# Durable record of fired alarms — survives voice-service restarts so an alarm
# persists until a human explicitly acknowledges it. Stores NAMES + context
# only; NEVER the secret value (writing the value here would be the leak).
_DATA_DIR = os.environ.get(
    "CLAUDE_PLUGIN_DATA",
    os.path.join(os.path.expanduser("~"), ".claude", "plugins", "data", "claude-voice"),
)
ALARM_STORE = os.path.join(_DATA_DIR, "secret_alarms.json")
_store_lock = threading.Lock()


def _make_id(names, instance_name, when):
    """Stable per-incident ID, index-free so the overlay can reference an
    incident without array positions that shift when others are cleared."""
    import hashlib
    basis = f"{instance_name}|{','.join(names)}|{when}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def _read_store():
    try:
        with open(ALARM_STORE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_store(data):
    try:
        os.makedirs(os.path.dirname(ALARM_STORE), exist_ok=True)
        with open(ALARM_STORE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


def record_alarm(names, instance_name, when=None):
    """Append an unacknowledged alarm to the durable store. NAMES only.

    `when` is a unix timestamp supplied by the caller. Returns the incident id.
    """
    names = list(names)
    entry = {
        "id": _make_id(names, instance_name, when),
        "names": names,
        "instance": instance_name,
        "when": when,
        "acknowledged": False,
    }
    with _store_lock:
        data = _read_store()
        data.append(entry)
        _write_store(data)
    return entry["id"]


def active_alarms():
    """All unacknowledged incidents, newest first. NAMES only, never values."""
    with _store_lock:
        data = _read_store()
    active = [e for e in data if not e.get("acknowledged")]
    active.sort(key=lambda e: e.get("when") or 0, reverse=True)
    return active


def clear_alarm(incident_id):
    """Acknowledge ONE incident by id. Returns True if it was found."""
    found = False
    with _store_lock:
        data = _read_store()
        for e in data:
            if e.get("id") == incident_id and not e.get("acknowledged"):
                e["acknowledged"] = True
                found = True
        if found:
            _write_store(data)
    return found


def clear_alarms():
    """Acknowledge ALL outstanding incidents. Returns how many were cleared."""
    with _store_lock:
        data = _read_store()
        n = 0
        for e in data:
            if not e.get("acknowledged"):
                e["acknowledged"] = True
                n += 1
        if n:
            _write_store(data)
    return n


class SecretAlarm:
    """Scans text for known secret VALUES. Reports NAMES only."""

    def __init__(self, env_path=None):
        self.env_path = env_path or DEFAULT_ENV
        self._secrets = {}
        self._mtime = 0

    def _load(self):
        """(Re)load secret values if the source file changed. Best-effort.

        No configured source -> stays empty -> every scan is a no-op.
        """
        if not self.env_path:
            return
        try:
            mtime = os.stat(self.env_path).st_mtime
        except OSError:
            return  # no source on disk -> nothing to scan for
        if mtime == self._mtime and self._secrets:
            return
        secrets = {}
        try:
            with open(self.env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if v and len(v) >= _MIN_LEN:
                        secrets.setdefault(k, v)
        except OSError:
            return
        if secrets:
            self._secrets = secrets
            self._mtime = mtime

    def scan_text(self, text):
        """Return secret NAMES whose VALUE appears in text. Never the value."""
        if not text:
            return []
        self._load()
        hits = []
        for name, value in self._secrets.items():
            if value in text:
                hits.append(name)
        return hits

    def scan_transcript_tail(self, path, from_offset=0):
        """Scan a transcript file from a byte offset to EOF.

        Returns (hit_names, new_offset). Scanning a delta (not the whole file)
        keeps this cheap on the hot path and avoids re-alarming old content.
        """
        try:
            size = os.path.getsize(path)
        except OSError:
            return [], from_offset
        if size <= from_offset:
            return [], size  # truncated or nothing new
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(from_offset)
                chunk = f.read()
        except OSError:
            return [], from_offset
        return self.scan_text(chunk), size
