"""Every macOS permission Hey Jev needs: read its status without asking, or ask for it on the user's request.

Statuses are only what macOS reports right now: granted | denied | not_asked | restricted | not_running (an
Automation target that isn't open, so macOS can't say) | unknown. Nothing here guesses a state or suggests a
restart on its own. Reads never show a prompt; request() is only called from a button in Settings.

The raw_* and ask_* functions are the only places that touch macOS, so tests replace them.
"""
import ctypes

SETTINGS_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_"

# key, name, why, System Settings anchor. Order is the order of the rows.
PERMISSIONS = (
    ("microphone", "Microphone", "Hears your commands.", "Microphone"),
    ("dictation", "Dictation", "Apple on-device transcription.", "SpeechRecognition"),
    ("accessibility", "Accessibility", "Sees and presses buttons and fields.", "Accessibility"),
    ("screen", "Screen Recording", "Reads text on screen.", "ScreenCapture"),
)
# The apps named in NSAppleEventsUsageDescription (setup.py): one Automation row each.
AUTOMATION = (
    ("com.apple.systemevents", "System Events", "Volume, dark mode, keystrokes."),
    ("com.spotify.client", "Spotify", "Plays and controls music."),
    ("com.apple.Safari", "Safari", "Opens and reads tabs."),
    ("com.google.Chrome", "Google Chrome", "Opens and reads tabs."),
)
LABELS = {"granted": "Granted", "denied": "Not granted", "not_asked": "Not asked yet", "restricted": "Restricted",
          "not_running": "Not running", "unknown": "Unknown"}


def rows():
    """(key, name, why, anchor) for every row, Automation targets keyed 'automation:<bundle id>'."""
    return list(PERMISSIONS) + [(f"automation:{b}", f"Automation: {n}", why, "Automation") for b, n, why in AUTOMATION]


def settings_url(key):
    return SETTINGS_URL + next(r[3] for r in rows() if r[0] == key)


# Status mapping, one per API.

def av_status(raw):
    """AVAuthorizationStatus: 0 notDetermined, 1 restricted, 2 denied, 3 authorized."""
    return {0: "not_asked", 1: "restricted", 2: "denied", 3: "granted"}.get(raw, "unknown")


def speech_status(raw):
    """SFSpeechRecognizerAuthorizationStatus: 0 notDetermined, 1 denied, 2 restricted, 3 authorized."""
    return {0: "not_asked", 1: "denied", 2: "restricted", 3: "granted"}.get(raw, "unknown")


def trusted_status(raw):
    """AXIsProcessTrusted / CGPreflightScreenCaptureAccess: a yes or no. macOS doesn't say whether it has asked."""
    return "granted" if raw is True else "denied" if raw is False else "unknown"


def ae_status(raw):
    """AEDeterminePermissionToAutomateTarget: noErr, errAEEventNotPermitted (-1743),
    errAEEventWouldRequireUserConsent (-1744), procNotFound (-600)."""
    return {0: "granted", -1743: "denied", -1744: "not_asked", -600: "not_running"}.get(raw, "unknown")


# macOS. Reads never prompt.

def raw_microphone():
    import AVFoundation
    return AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(AVFoundation.AVMediaTypeAudio)


def raw_dictation():
    import Speech
    return Speech.SFSpeechRecognizer.authorizationStatus()


def raw_accessibility():
    import ApplicationServices
    return bool(ApplicationServices.AXIsProcessTrusted())


def raw_screen():
    import Quartz
    return bool(Quartz.CGPreflightScreenCaptureAccess())


class _AEDesc(ctypes.Structure):
    _fields_ = [("descriptorType", ctypes.c_uint32), ("dataHandle", ctypes.c_void_p)]


def _fourcc(s):
    return int.from_bytes(s.encode(), "big")


_CS = None


def _coreservices():
    global _CS
    if _CS is None:
        cs = ctypes.CDLL("/System/Library/Frameworks/CoreServices.framework/CoreServices")
        cs.AECreateDesc.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_long, ctypes.POINTER(_AEDesc)]
        cs.AECreateDesc.restype = ctypes.c_int16
        cs.AEDeterminePermissionToAutomateTarget.argtypes = [ctypes.POINTER(_AEDesc), ctypes.c_uint32,
                                                             ctypes.c_uint32, ctypes.c_bool]
        cs.AEDeterminePermissionToAutomateTarget.restype = ctypes.c_int32
        cs.AEDisposeDesc.argtypes = [ctypes.POINTER(_AEDesc)]
        _CS = cs
    return _CS


def raw_automation(bundle, ask=False):
    """OSStatus for sending Apple events to this app. ask=True may show the prompt and blocks until it's answered:
    never on the main thread."""
    cs, desc, data = _coreservices(), _AEDesc(), bundle.encode()
    if cs.AECreateDesc(_fourcc("bund"), data, len(data), ctypes.byref(desc)):
        return None
    try:
        return cs.AEDeterminePermissionToAutomateTarget(ctypes.byref(desc), _fourcc("****"), _fourcc("****"), ask)
    finally:
        cs.AEDisposeDesc(ctypes.byref(desc))


def running(bundle):
    from AppKit import NSRunningApplication
    return len(NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle)) > 0


def status(key):
    """One row's status from what macOS reports now. Any error reads as unknown, never as granted."""
    try:
        if key.startswith("automation:"):
            return ae_status(raw_automation(key.split(":", 1)[1]))
        return {"microphone": lambda: av_status(raw_microphone()),
                "dictation": lambda: speech_status(raw_dictation()),
                "accessibility": lambda: trusted_status(raw_accessibility()),
                "screen": lambda: trusted_status(raw_screen())}[key]()
    except Exception:
        return "unknown"


def snapshot():
    """{key: status} for every row. Blocking; call off the main thread."""
    return {key: status(key) for key, *_ in rows()}


# Asking. Each calls done() once the request has returned or been answered.

def ask_microphone(done):
    import AVFoundation
    AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(AVFoundation.AVMediaTypeAudio,
                                                                              lambda _granted: done())


def ask_dictation(done):
    import speech_apple
    speech_apple.request_access(lambda _status: done())


def ask_accessibility(done):
    import ApplicationServices
    ApplicationServices.AXIsProcessTrustedWithOptions({ApplicationServices.kAXTrustedCheckOptionPrompt: True})
    done()


def ask_screen(done):
    import Quartz
    Quartz.CGRequestScreenCaptureAccess()
    done()


def ask_automation(bundle, done):
    raw_automation(bundle, ask=True)
    done()


def request(key, done):
    """Show macOS's prompt for this permission. Automation targets must already be running: this never opens
    them. Returns False (and doesn't ask) when the target isn't running. Blocking for Automation: call off the
    main thread."""
    if key.startswith("automation:"):
        bundle = key.split(":", 1)[1]
        if not running(bundle):
            return False
        ask_automation(bundle, done)
        return True
    {"microphone": ask_microphone, "dictation": ask_dictation, "accessibility": ask_accessibility,
     "screen": ask_screen}[key](done)
    return True
