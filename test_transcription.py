"""Backend choice: Apple is used only when ready; otherwise listening is blocked with a reason, never a fallback."""
import sys
import types
import unittest
from unittest.mock import patch

import model_settings
import siri


def fake_speech(state, reason=""):
    mod = types.ModuleType("speech_apple")
    mod.status = lambda locale="en-US": (state, reason)
    mod.AppleTranscriber = lambda locale: types.SimpleNamespace(transcribe=lambda audio, prompt=None: ("hi", 5))
    return mod


class BackendTests(unittest.TestCase):
    def test_apple_ready_is_used(self):
        with patch.dict(sys.modules, {"speech_apple": fake_speech("ready")}):
            fn, blocked = siri.load_transcriber("apple", None)
        self.assertIsNone(blocked)
        self.assertEqual(fn(None, None), ("hi", 5))

    def test_apple_not_ready_blocks_without_loading_whisper(self):
        with patch.dict(sys.modules, {"speech_apple": fake_speech("denied", "Permission denied."),
                                      "faster_whisper": None}):
            fn, blocked = siri.load_transcriber("apple", None)
        self.assertIsNone(fn)
        self.assertIn("Permission denied.", blocked)

    def test_apple_missing_bindings_blocks(self):
        with patch.dict(sys.modules, {"speech_apple": None}):
            fn, blocked = siri.load_transcriber("apple", None)
        self.assertIsNone(fn)
        self.assertIn("isn't installed", blocked)

    def test_backend_pref_validates(self):
        saved = model_settings.PREFS.stringForKey_("transcription_backend")
        try:
            with self.assertRaises(ValueError):
                model_settings.save_transcription_backend("cloud")
            model_settings.save_transcription_backend("apple")
            self.assertEqual(model_settings.transcription_backend(), "apple")
            model_settings.PREFS.setObject_forKey_("bogus", "transcription_backend")
            self.assertEqual(model_settings.transcription_backend(), "whisper")
        finally:
            if saved:
                model_settings.PREFS.setObject_forKey_(saved, "transcription_backend")
            else:
                model_settings.PREFS.removeObjectForKey_("transcription_backend")


class FakeRecorder:
    """Stands in for the microphone: records the stream state, returns one second of audio from a recording."""
    def __init__(self):
        import queue
        self.enabled, self.epoch, self.on, self.wake, self.paused = True, 0, False, False, False
        self.segments = queue.Queue()
        self.stream = types.SimpleNamespace(running=True)
        self.stream.start = lambda: setattr(self.stream, "running", True)
        self.stream.stop = lambda: setattr(self.stream, "running", False)

    def invalidate(self):
        self.epoch += 1
        self.on = False

    def start(self):
        self.on = True

    def stop(self):
        import numpy as np
        self.on = False
        return np.ones(16000, dtype="float32")


class LiveSwitchTests(unittest.TestCase):
    """Runs the real run_voice_assistant loop with the mic, engine and socket faked."""

    def setUp(self):
        import queue
        import threading
        self.controls, self.events, self.logged = queue.Queue(), [], []
        self.apple_ready = False
        self.submitted = []

        def load(backend, notify):
            if backend == "apple" and not self.apple_ready:
                return None, "Apple dictation isn't ready: not_determined."
            if backend == "broken":
                def fail(audio, prompt):
                    raise RuntimeError("recognition failed: Code=1110")
                return fail, None
            return (lambda audio, prompt: (f"{backend} heard {len(audio)}", 7)), None

        eng = types.SimpleNamespace(instance="t", shutdown=lambda: None,
                                    submit=lambda *a, **k: self.submitted.append(a))
        self.patches = [patch.object(siri, "load_transcriber", load), patch.object(siri, "Recorder", FakeRecorder),
                        patch.object(siri, "make_engine", lambda *a, **k: eng),
                        patch.object(siri, "start_bridge", lambda *a: types.SimpleNamespace(stop=lambda: None)),
                        patch.object(siri, "transcription_backend", lambda: "apple"),
                        patch.object(siri, "warm_cache", lambda: None),
                        patch.object(siri.timers, "start_loop", lambda cb: None),
                        patch.object(siri.diagnostics, "init", lambda *a, **k: None),
                        patch.object(siri.diagnostics, "record", lambda *a, **k: self.logged.append((a, k))),
                        patch.object(siri, "MIC_TEST_SECONDS", 0)]
        for p in self.patches:
            p.start()
        self.thread = threading.Thread(target=siri.run_voice_assistant,
                                       args=(lambda s, d="": self.events.append((s, d)), self.controls, "ptt", True),
                                       daemon=True)
        self.thread.start()
        self.wait(lambda: siri.STT["backend"] == "apple" and not siri.STT["switching"])

    def tearDown(self):
        self.controls.put("quit")
        self.thread.join(5)
        for p in self.patches:
            p.stop()

    def wait(self, cond, timeout=5):
        import time
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            time.sleep(0.01)
        self.fail("condition not reached")

    def mic_test(self):
        import queue
        got = queue.Queue()
        self.controls.put(("mic_test", got.put))
        return got.get(timeout=5)

    def test_blocked_then_permission_granted_recovers_without_restart(self):
        self.assertIn("not_determined", siri.STT["blocked"])
        self.assertIn("not_determined", self.mic_test()["error"])
        self.controls.put(("listening", True))  # resume is refused while blocked
        self.wait(lambda: any(s == "Dictation unavailable" for s, _ in self.events))
        self.apple_ready = True  # user clicked Allow; Settings sends a reload
        self.controls.put(("transcription", "apple"))
        self.wait(lambda: siri.STT["blocked"] is None and not siri.STT["switching"])
        result = self.mic_test()
        self.assertEqual((result["text"], result["backend"]), ("apple heard 16000", "apple"))
        self.assertEqual(self.submitted, [])  # transcript only: nothing reached the engine

    def test_live_switch_and_failure_is_logged(self):
        self.controls.put(("transcription", "whisper"))
        self.wait(lambda: siri.STT["backend"] == "whisper" and not siri.STT["switching"])
        self.assertEqual(self.mic_test()["text"], "whisper heard 16000")
        self.controls.put(("transcription", "broken"))
        self.wait(lambda: siri.STT["backend"] == "broken" and not siri.STT["switching"])
        self.assertIn("Code=1110", self.mic_test()["error"])
        failed = [k for a, k in self.logged if a[1:3] == ("transcribe", "failed")]
        self.assertEqual(failed[0]["backend"], "broken")
        self.assertIn("Code=1110", failed[0]["error"])
        self.assertNotIn("audio", {k for k in failed[0] if k != "audio_s"})


if __name__ == "__main__":
    unittest.main()
