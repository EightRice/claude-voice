#!/usr/bin/env python3
"""
Audio utilities for claude-voice — startup chime, beep, and tone generation.

All audio is generated as numpy float32 arrays at 44100 Hz, suitable for
direct playback via the AudioMixer.
"""

import numpy as np

MIXER_SR = 44100


def _gen_tone(freq, dur=0.09, vol=0.25, sr=MIXER_SR):
    """Generate a single tone with attack/release envelope."""
    n = int(sr * dur)
    t = np.linspace(0, dur, n, dtype=np.float32)
    tone = vol * np.sin(2 * np.pi * freq * t)
    att = min(int(0.005 * sr), n)
    rel = min(int(0.015 * sr), n)
    tone[:att] *= np.linspace(0, 1, att, dtype=np.float32)
    tone[-rel:] *= np.linspace(1, 0, rel, dtype=np.float32)
    return tone


def _silence(dur=0.01, sr=MIXER_SR):
    """Generate silence of given duration."""
    return np.zeros(int(sr * dur), dtype=np.float32)


def make_startup_chime(sr=MIXER_SR):
    """Generate C-E-G ascending chime (startup sound)."""
    c5 = _gen_tone(523, dur=0.055, vol=0.45, sr=sr)
    e5 = _gen_tone(659, dur=0.055, vol=0.45, sr=sr)
    g5 = _gen_tone(784, dur=0.080, vol=0.50, sr=sr)
    gap = _silence(0.01, sr)
    return np.concatenate([c5, gap, e5, gap, g5])


def make_beep(sr=MIXER_SR):
    """Generate a two-tone alert beep."""
    tonic = _gen_tone(800, dur=0.07, vol=0.5, sr=sr)
    sixth = _gen_tone(800 * 5 / 3, dur=0.07, vol=0.5, sr=sr)
    gap = _silence(0.015, sr)
    return np.concatenate([sixth, gap, tonic])


def make_tool_tone(tool_name, sr=MIXER_SR):
    """Generate a tone sequence for a tool type."""
    TOOL_SOUND_MAP = {
        "Bash": "execute", "Read": "read", "Edit": "edit", "Write": "write",
        "Grep": "search", "Glob": "search", "Task": "agent",
        "WebFetch": "web", "WebSearch": "web", "TodoWrite": "todo",
        "NotebookEdit": "edit",
    }
    TOOL_FREQS = {
        "execute": [440, 554],
        "read":    [659],
        "edit":    [440, 659],
        "write":   [554, 440],
        "search":  [880, 659],
        "agent":   [440, 554, 659],
        "web":     [330, 440],
        "todo":    [554, 659],
        "default": [440],
    }
    cat = TOOL_SOUND_MAP.get(tool_name, "default")
    freqs = TOOL_FREQS.get(cat, TOOL_FREQS["default"])
    gap = _silence(0.015, sr)
    parts = []
    for f in freqs:
        parts.append(_gen_tone(f, sr=sr))
        parts.append(gap)
    return np.concatenate(parts)


def make_result_chime(sr=MIXER_SR):
    """Generate a two-note completion chime (C5 + G5)."""
    c5 = _gen_tone(523, dur=0.12, vol=0.18, sr=sr)
    g5 = _gen_tone(784, dur=0.15, vol=0.15, sr=sr)
    gap = _silence(0.04, sr)
    return np.concatenate([c5, gap, g5])
