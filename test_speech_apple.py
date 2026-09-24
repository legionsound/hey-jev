"""Offline unit tests for speech_apple.py. Frameworks mocked; no mic, no permission prompt."""

import sys
import types
import unittest

import numpy as np

import speech_apple


def _install_fake(auth=3, on_device=True, available=True, supported_locale=True, final_text="open safari"):
    speech = types.ModuleType("Speech")
    av = types.ModuleType("AVFoundation")
    fnd = types.ModuleType("Foundation")

    class FakeResult:
        def isFinal(self):
            return True

        def bestTranscription(self):
            class B:
                def formattedString(self):
                    return final_text

            return B()

    class FakeRecognizer:
        last = None

        def __init__(self):
            FakeRecognizer.last = self
            self.queue = None

        @classmethod
        def alloc(cls):
            return cls()

        def initWithLocale_(self, loc):
            if not supported_locale:
                return None
            return self

        def supportsOnDeviceRecognition(self):
            return on_device

        def isAvailable(self):
            return available

        def setQueue_(self, q):
            self.queue = q

        def recognitionTaskWithRequest_resultHandler_(self, req, handler):
            req._handler = handler
            return object()

    auth_box = {"v": auth}

    class SFSpeechRecognizerNS:
        @staticmethod
        def authorizationStatus():
            return auth_box["v"]

        @staticmethod
        def requestAuthorization_(done):
            done(3)

    # Merge class + statics onto one name the module uses
    class Rec(FakeRecognizer):
        pass

    Rec.authorizationStatus = SFSpeechRecognizerNS.authorizationStatus
    Rec.requestAuthorization_ = SFSpeechRecognizerNS.requestAuthorization_
    speech.SFSpeechRecognizer = Rec

    class FakeReq:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            self.flags = {}
            return self

        def setRequiresOnDeviceRecognition_(self, v):
            self.flags["on_device"] = bool(v)

        def setShouldReportPartialResults_(self, v):
            self.flags["partial"] = bool(v)

        def appendAudioPCMBuffer_(self, buf):
            self.buf = buf

        def endAudio(self):
            assert self.flags.get("on_device") is True, "on-device flag must be True"
            assert self.flags.get("partial") is False, "partial results must be off"
            self._handler(FakeResult(), None)

    speech.SFSpeechAudioBufferRecognitionRequest = FakeReq
    speech._fake_req = FakeReq

    av.AVAudioCommonFormatFloat32 = 1

    class FakeFmt:
        @classmethod
        def alloc(cls):
            return cls()

        def initWithCommonFormat_sampleRate_channels_interleaved_(self, *a):
            return self

    class FakeBuf:
        @classmethod
        def alloc(cls):
            return cls()

        def initWithPCMFormat_frameCapacity_(self, fmt, n):
            self.n = int(n)
            self._store = np.zeros(self.n, dtype=np.float32)
            return self

        def setFrameLength_(self, n):
            pass

        def floatChannelData(self):
            import ctypes

            return [ctypes.addressof(self._store.ctypes.data_as(ctypes.POINTER(ctypes.c_float)).contents)
                    if False else self._store.ctypes.data]

    av.AVAudioFormat = FakeFmt
    av.AVAudioPCMBuffer = FakeBuf

    class FakeLocale:
        @classmethod
        def alloc(cls):
            return cls()

        def initWithLocaleIdentifier_(self, s):
            return s

    class FakeQueue:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

    fnd.NSLocale = FakeLocale
    fnd.NSOperationQueue = FakeQueue

    sys.modules["Speech"] = speech
    sys.modules["AVFoundation"] = av
    sys.modules["Foundation"] = fnd
    return speech


class StatusTests(unittest.TestCase):
    def tearDown(self):
        for m in ("Speech", "AVFoundation", "Foundation"):
            sys.modules.pop(m, None)

    def test_ready(self):
        _install_fake(auth=3)
        self.assertEqual(speech_apple.status()[0], "ready")

    def test_not_determined_never_prompts(self):
        mod = _install_fake(auth=0)
        state, _ = speech_apple.status()
        self.assertEqual(state, "not_determined")

    def test_denied_restricted(self):
        _install_fake(auth=1)
        self.assertEqual(speech_apple.status()[0], "denied")
        _install_fake(auth=2)
        self.assertEqual(speech_apple.status()[0], "restricted")

    def test_no_on_device(self):
        _install_fake(auth=3, on_device=False)
        self.assertEqual(speech_apple.status()[0], "no_on_device")

    def test_unsupported_locale(self):
        _install_fake(auth=3, supported_locale=False)
        self.assertEqual(speech_apple.status()[0], "unsupported_locale")

    def test_unavailable(self):
        _install_fake(auth=3, available=False)
        self.assertEqual(speech_apple.status()[0], "unavailable")

    def test_missing_bindings(self):
        for m in ("Speech", "AVFoundation", "Foundation"):
            sys.modules.pop(m, None)
        # force import failure
        import builtins

        real = builtins.__import__

        def fake(name, *a, **k):
            if name in ("Speech", "AVFoundation", "Foundation"):
                raise ImportError("no " + name)
            return real(name, *a, **k)

        builtins.__import__ = fake
        try:
            self.assertEqual(speech_apple.status()[0], "missing_bindings")
        finally:
            builtins.__import__ = real

    def test_request_access_is_only_prompter(self):
        mod = _install_fake(auth=0)
        got = []
        speech_apple.request_access(got.append)
        self.assertEqual(got, [3])


class TranscribeTests(unittest.TestCase):
    def tearDown(self):
        for m in ("Speech", "AVFoundation", "Foundation"):
            sys.modules.pop(m, None)

    def test_final_only_bounded(self):
        _install_fake(auth=3, final_text="open safari")
        tr = speech_apple.AppleTranscriber(timeout_s=5)
        audio = np.zeros(16000, dtype=np.float32)
        text, ms = tr.transcribe(audio)
        self.assertEqual(text, "open safari")
        self.assertGreaterEqual(ms, 0)

    def test_refuses_when_not_ready(self):
        _install_fake(auth=1)
        tr = speech_apple.AppleTranscriber()
        with self.assertRaisesRegex(RuntimeError, "denied"):
            tr.transcribe(np.zeros(100, dtype=np.float32))

    def test_empty_audio_raises(self):
        _install_fake(auth=3)
        tr = speech_apple.AppleTranscriber()
        with self.assertRaisesRegex(RuntimeError, "empty"):
            tr.transcribe(np.zeros(0, dtype=np.float32))


if __name__ == "__main__":
    unittest.main()
