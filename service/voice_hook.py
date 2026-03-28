#!/usr/bin/env python3
"""
Voice hook script — bridge between Claude Code instances and the voice service.

Reads hook event JSON from stdin, enriches with instance metadata, and
forwards to the voice service via UDP. No-op if service isn't running.

Used as a Claude Code hook command for PreToolUse / Stop.
"""

import sys
import os
import json
import socket

DEFAULT_PORT = 9111


def _get_port():
    return int(os.environ.get("VOICE_MODE_PORT", str(DEFAULT_PORT)))


def _service_alive(port):
    """Quick check if something is listening on the service port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("127.0.0.1", port))
        s.close()
        return False  # We could bind, so nothing is listening
    except OSError:
        s.close()
        return True  # Port in use — service is running


def _send(data_bytes, port=None):
    port = port or _get_port()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(data_bytes, ("127.0.0.1", port))
        s.close()
    except Exception:
        pass


def _get_hwnd():
    """Get the console window handle (Windows only)."""
    if sys.platform != "win32":
        return 0
    try:
        import ctypes
        return ctypes.windll.kernel32.GetConsoleWindow()
    except Exception:
        return 0


def forward():
    """Read hook event JSON from stdin, enrich with metadata, forward to service."""
    port = _get_port()

    # Fast exit if service isn't running — don't slow down Claude
    if not _service_alive(port):
        sys.exit(0)

    data = b""
    try:
        data = sys.stdin.buffer.read()
    except Exception:
        pass

    if data:
        try:
            event = json.loads(data)
            name = os.environ.get("CLAUDE_NAME", "")
            if name:
                event["_instance_name"] = name
            hwnd = _get_hwnd()
            if hwnd:
                event["_instance_hwnd"] = hwnd
            ppid = os.getppid() if hasattr(os, "getppid") else os.getpid()
            event["_instance_pid"] = ppid
            data = json.dumps(event).encode("utf-8")
        except (json.JSONDecodeError, Exception):
            pass  # forward raw if enrichment fails
        _send(data, port)


if __name__ == "__main__":
    forward()
