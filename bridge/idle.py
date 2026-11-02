"""Is the user at the PC? Windows' last keyboard/mouse input, so the band only buzzes when they are away."""
import ctypes
import os

AWAY_AFTER = float(os.environ.get("BAND_BRIDGE_AWAY_AFTER", 60))  # seconds without input


class _LastInput(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def idle_seconds() -> float:
    info = _LastInput(ctypes.sizeof(_LastInput))
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0  # unknown: treat as present, the quieter choice
    return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000


def away(threshold: float = AWAY_AFTER) -> bool:
    return idle_seconds() >= threshold
