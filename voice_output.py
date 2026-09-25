"""Native playback with live voice gain, independent of system volume."""
import json
import re
import threading
import time
from AppKit import NSSound
from model_settings import PREFS

_lock = threading.RLock()
_play_lock = threading.Lock()
_sound = None
_owner = None  # who started the current sound, so a sample's Stop never cuts off Jev's own speech


# Performance cues each voice provider understands, as written in reply lines ("[chuckling] There you go").
CUES = {"fish": ["chuckling", "laughing", "sighing", "cheerful", "clear throat"]}
CUE_MODES = ("all", "some", "none")
# Every bracketed performance tag a provider's voice acts on, including ones our lines don't use. Only these are
# cues: any other bracketed text ("use [optional] here") is dialogue and is always spoken as written.
PERFORMANCE_TAGS = {"fish": set(CUES["fish"]) | {
    "whispering", "shouting", "screaming", "soft tone", "in a hurry tone", "sobbing", "crying loudly", "panting",
    "groaning", "crowd laughing", "background laughter", "audience laughing", "excited", "sad", "angry",
    "surprised", "delighted", "joyful", "nervous", "worried", "confused", "curious", "proud", "relaxed"}}
_TAG = re.compile(r"\[([a-z][a-z ]*)\]\s*")


def cues(provider="fish"):
    """{"mode": "all" | "some" | "none", "on": [cue, ...]}: which cues this provider's voice performs.
    Default all (the lines as written). "some" performs only the cues listed in "on"."""
    try:
        raw = json.loads(PREFS.stringForKey_("voice_cues") or "{}").get(provider) or {}
        mode = raw.get("mode") if raw.get("mode") in CUE_MODES else "all"
        on = [c for c in raw.get("on", []) if c in CUES.get(provider, [])]
    except (ValueError, TypeError, AttributeError):
        mode, on = "all", []
    return {"mode": mode, "on": on if mode == "some" else list(CUES.get(provider, [])) if mode == "all" else []}


def set_cues(provider, mode, on=()):
    """Save one provider's choice; other providers' choices are kept."""
    if mode not in CUE_MODES:
        raise ValueError(f"mode must be one of {CUE_MODES}")
    known = CUES.get(provider, [])
    try:
        allp = json.loads(PREFS.stringForKey_("voice_cues") or "{}")
        allp = allp if isinstance(allp, dict) else {}
    except ValueError:
        allp = {}
    allp[provider] = {"mode": mode, "on": [c for c in on if c in known]}
    PREFS.setObject_forKey_(json.dumps(allp), "voice_cues")


def with_cues(text, provider="fish"):
    """The line as this provider should hear it: cues switched off are removed. "all" leaves it untouched.
    A line that was only switched-off cues comes back empty, and callers speak nothing for it."""
    c = cues(provider)
    if c["mode"] == "all":
        return text
    keep, tags = set(c["on"]), PERFORMANCE_TAGS.get(provider, set())
    out = _TAG.sub(lambda m: "" if m.group(1) in tags and m.group(1) not in keep else m.group(0), text)
    return re.sub(r"\s{2,}", " ", out).strip()


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


def stop(owner=None):
    """Stop the current sound (only if `owner` started it, when given); play() then returns."""
    with _lock:
        if _sound and (owner is None or owner is _owner):
            _sound.stop()


def play(path, owner=None, cancelled=None):
    """Play to the end. `cancelled` (an Event) is checked at the real start, under the same lock stop() takes,
    so a cancel either prevents playback or stops it: never a late start. -> False when it didn't play."""
    global _sound, _owner
    with _play_lock:
        with _lock:
            if muted() or volume() == 0 or (cancelled is not None and cancelled.is_set()):
                return False
            sound = NSSound.alloc().initWithContentsOfFile_byReference_(str(path), True)
            if sound is None:
                raise RuntimeError("Could not open voice audio.")
            sound.setVolume_(volume())
            _sound, _owner = sound, owner
            if not sound.play():
                _sound = _owner = None
                raise RuntimeError("Could not start voice playback.")
        try:
            while sound.isPlaying():
                time.sleep(0.02)
        finally:
            with _lock:
                _sound = _owner = None
        return True
