"""Native playback with live voice gain, independent of system volume."""
import threading
import time
from AppKit import NSSound
from model_settings import PREFS

_lock = threading.RLock()
_play_lock = threading.Lock()
_sound = None


def volume():
    value = PREFS.objectForKey_("voice_volume")
    return max(0.0, min(1.0, float(value))) if value is not None else 1.0


def muted():
    return bool(PREFS.boolForKey_("voice_muted"))


def configure(*, gain=None, mute=None):
    with _lock:
        if gain is not None:
            PREFS.setDouble_forKey_(max(0.0, min(1.0, float(gain))), "voice_volume")
        if mute is not None:
            PREFS.setBool_forKey_(bool(mute), "voice_muted")
        if _sound:
            _sound.setVolume_(0 if muted() else volume())
            if muted():
                _sound.stop()


def stop():
    """Stop whatever is playing now; play() then returns."""
    with _lock:
        if _sound:
            _sound.stop()


def play(path):
    global _sound
    with _play_lock:
        with _lock:
            if muted() or volume() == 0:
                return
            sound = NSSound.alloc().initWithContentsOfFile_byReference_(str(path), True)
            if sound is None:
                raise RuntimeError("Could not open voice audio.")
            sound.setVolume_(volume())
            _sound = sound
            if not sound.play():
                _sound = None
                raise RuntimeError("Could not start voice playback.")
        try:
            while sound.isPlaying():
                time.sleep(0.02)
        finally:
            with _lock:
                _sound = None
