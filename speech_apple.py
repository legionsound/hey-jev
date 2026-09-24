"""Apple on-device transcription via SFSpeechRecognizer (PyObjC).

Input adapter beside faster-whisper. Audio shape matches siri.py Recorder:
float32 mono 16 kHz numpy. Only the final transcript is returned; partial
hypotheses are never executed. Every request sets
requiresOnDeviceRecognition = True; no cloud fallback.
"""

import threading
import time

SAMPLE_RATE = 16000
DEFAULT_TIMEOUT_S = 60.0


def _frameworks():
    try:
        import Speech  # type: ignore
        import AVFoundation  # type: ignore
        import Foundation  # type: ignore
    except ImportError as exc:
        return None, exc
    return (Speech, AVFoundation, Foundation), None


def status(locale="en-US"):
    """Return (state, reason). Never triggers the OS permission prompt."""
    mods, exc = _frameworks()
    if mods is None:
        return ("missing_bindings", "pyobjc-framework-Speech/AVFoundation not installed: %s" % exc)
    Speech, _AV, Foundation = mods
    loc = Foundation.NSLocale.alloc().initWithLocaleIdentifier_(locale)
    rec = Speech.SFSpeechRecognizer.alloc().initWithLocale_(loc)
    if rec is None:
        return ("unsupported_locale", "no recognizer for locale %s" % locale)
    auth = Speech.SFSpeechRecognizer.authorizationStatus()
    # SFSpeechRecognizerAuthorizationStatus: 0 not-determined, 1 denied,
    # 2 restricted, 3 authorized
    if auth == 1:
        return ("denied", "Speech recognition denied in System Settings > Privacy & Security")
    if auth == 2:
        return ("restricted", "Speech recognition restricted on this Mac")
    if not rec.supportsOnDeviceRecognition():
        return ("no_on_device", "locale %s has no on-device support" % locale)
    if auth == 0:
        return ("not_determined", "authorization not requested yet; call request_access() from Settings")
    if not rec.isAvailable():
        return ("unavailable", "recognizer temporarily unavailable")
    return ("ready", "on-device recognition available for %s" % locale)


def request_access(done):
    """Trigger the OS Speech permission prompt. Only call from a Settings button."""
    mods, exc = _frameworks()
    if mods is None:
        raise RuntimeError("missing_bindings: %s" % exc)
    Speech = mods[0]
    Speech.SFSpeechRecognizer.requestAuthorization_(done)


class AppleTranscriber:
    def __init__(self, locale="en-US", timeout_s=DEFAULT_TIMEOUT_S):
        self.locale = locale
        self.timeout_s = timeout_s

    def transcribe(self, audio, prompt=None):
        """Transcribe float32 mono 16kHz numpy audio. Returns (text, ms)."""
        del prompt  # Apple requests take no prompt; kept for backend parity
        state, reason = status(self.locale)
        if state != "ready":
            raise RuntimeError("%s: %s" % (state, reason))
        import numpy as np

        import AVFoundation
        import Foundation
        import Speech

        t0 = time.time()
        data = np.ascontiguousarray(audio, dtype=np.float32).reshape(-1)
        if data.size == 0:
            raise RuntimeError("empty audio")

        fmt = AVFoundation.AVAudioFormat.alloc().initWithCommonFormat_sampleRate_channels_interleaved_(
            AVFoundation.AVAudioPCMFormatFloat32, SAMPLE_RATE, 1, False
        )
        buf = AVFoundation.AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(fmt, int(data.size))
        buf.setFrameLength_(int(data.size))
        # floatChannelData() is a tuple of objc.varlist; as_buffer gives a writable view of channel 0
        np.frombuffer(buf.floatChannelData()[0].as_buffer(int(data.size)), dtype=np.float32)[:] = data

        req = Speech.SFSpeechAudioBufferRecognitionRequest.alloc().init()
        req.setRequiresOnDeviceRecognition_(True)
        req.setShouldReportPartialResults_(False)

        loc = Foundation.NSLocale.alloc().initWithLocaleIdentifier_(self.locale)
        rec = Speech.SFSpeechRecognizer.alloc().initWithLocale_(loc)
        queue = Foundation.NSOperationQueue.alloc().init()

        done_ev = threading.Event()
        out = {}

        def handler(result, error):
            if error is not None:
                out["error"] = error
            elif result is not None and result.isFinal():
                out["text"] = str(result.bestTranscription().formattedString())
            if result is not None and result.isFinal():
                done_ev.set()
            if error is not None:
                done_ev.set()

        rec.setQueue_(queue)
        task = rec.recognitionTaskWithRequest_resultHandler_(req, handler)  # held until done
        req.appendAudioPCMBuffer_(buf)
        req.endAudio()

        if not done_ev.wait(self.timeout_s):
            task.cancel()
            raise RuntimeError("timeout after %.0fs waiting for on-device result" % self.timeout_s)
        if "error" in out:
            raise RuntimeError("recognition failed: %s" % out["error"])
        ms = int((time.time() - t0) * 1000)
        return (out.get("text", "").strip(), ms)
