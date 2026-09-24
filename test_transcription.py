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
    mod.hints = []

    def transcriber(locale, contextual_strings=()):
        mod.hints.append(list(contextual_strings))
        return types.SimpleNamespace(transcribe=lambda audio, prompt=None: ("hi", 5))
    mod.AppleTranscriber = transcriber
    return mod


class BackendTests(unittest.TestCase):
    def test_apple_ready_is_used_with_the_current_wake_hints(self):
        mod = fake_speech("ready")
        with patch.dict(sys.modules, {"speech_apple": mod}):
            fn, blocked = siri.load_transcriber("apple", None)
            self.assertIsNone(blocked)
            self.assertEqual(fn(None, None), ("hi", 5))
            with patch.object(siri, "WAKE", siri.wake.Wake("Okay Zorblat", ["OK Zorblat"])):
                fn(None, None)  # same loaded backend, new phrase: hinted from the next utterance
        self.assertEqual(mod.hints, [["Hey Jev"], ["Okay Zorblat", "OK Zorblat"]])

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
        class Prefs(dict):  # never touch the real app preferences
            stringForKey_ = dict.get

            def setObject_forKey_(self, value, key):
                self[key] = value
        with patch.object(model_settings, "PREFS", Prefs()):
            with self.assertRaises(ValueError):
                model_settings.save_transcription_backend("cloud")
            model_settings.save_transcription_backend("apple")
            self.assertEqual(model_settings.transcription_backend(), "apple")
            model_settings.PREFS.setObject_forKey_("bogus", "transcription_backend")
            self.assertEqual(model_settings.transcription_backend(), "whisper")


class FakeRecorder:
    """Stands in for the microphone: records the stream state, returns one second of audio from a recording."""
    def __init__(self):
        import queue
        self.enabled, self.epoch, self.on, self.wake, self.paused, self.isolated = True, 0, False, False, False, False
        self.segments = queue.Queue()
        self.stream = types.SimpleNamespace(running=True)
        self.stream.start = lambda: setattr(self.stream, "running", True)
        self.stream.stop = lambda: setattr(self.stream, "running", False)

    def invalidate(self):
        self.epoch += 1
        self.on = False

    def _reset_segment(self):
        pass

    def start(self):
        self.on = True

    def stop(self):
        import numpy as np
        self.on = False
        return np.ones(16000, dtype="float32")


class LoopHarness(unittest.TestCase):
    """Runs the real run_voice_assistant loop with the mic, engine and socket faked."""

    def setUp(self):
        import queue
        import threading
        self.controls, self.events, self.logged = queue.Queue(), [], []
        self.apple_ready = False
        self.submitted, self.prompts, self.heard, self.gate, self.entered = [], [], None, None, threading.Event()

        def load(backend, notify):
            if backend == "apple" and not self.apple_ready:
                return None, "Apple dictation isn't ready: not_determined."
            if backend == "broken":
                def fail(audio, prompt):
                    raise RuntimeError("recognition failed: Code=1110")
                return fail, None
            def fn(audio, prompt):
                self.prompts.append(prompt)
                if self.gate is not None:
                    self.entered.set()
                    self.gate.wait(5)
                return (self.heard or f"{backend} heard {len(audio)}", 7)
            return fn, None

        def submit(*a, **k):
            self.submitted.append(a)
            raise RuntimeError("test engine: stop here")  # the submission itself is what these tests count
        eng = types.SimpleNamespace(instance="t", shutdown=lambda: None, submit=submit)
        test_self = self

        class Rec(FakeRecorder):
            def __init__(self):
                super().__init__()
                test_self.rec = self
        self.patches = [patch.object(siri, "load_transcriber", load), patch.object(siri, "Recorder", Rec),
                        patch.object(siri, "make_engine", lambda *a, **k: eng),
                        patch.object(siri, "start_bridge", lambda *a: types.SimpleNamespace(stop=lambda: None)),
                        patch.object(siri, "transcription_backend", lambda: "apple"),
                        patch.object(siri, "wake_settings", lambda: ("Hey Jev", [])),
                        patch.object(siri, "warm_cache", lambda: None),
                        patch.object(siri.timers, "start_loop", lambda cb: None),
                        patch.object(siri.diagnostics, "init", lambda *a, **k: None),
                        patch.object(siri.diagnostics, "record", lambda *a, **k: self.logged.append((a, k))),
                        patch.object(siri, "MIC_TEST_SECONDS", getattr(self, "seconds", 0))]
        for p in self.patches:
            p.start()
        siri.STT.update(backend=None, blocked=None, switching=False)  # module state from an earlier test
        self.thread = threading.Thread(target=siri.run_voice_assistant,
                                       args=(lambda s, d="": self.events.append((s, d)), self.controls, "ptt", True),
                                       daemon=True)
        self.thread.start()
        self.wait(lambda: siri.STT["backend"] == "apple" and not siri.STT["switching"] and hasattr(self, "rec"))

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

    def mic_test(self):  # noqa: E301
        import queue
        got = queue.Queue()
        self.controls.put(("mic_test", got.put))
        return got.get(timeout=5)



class LiveSwitchTests(LoopHarness):
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


class MicTestIsolationTests(LoopHarness):
    """A real test window (0.4 s) so other input can land inside it. Apple starts ready here."""
    seconds = 0.4

    def setUp(self):
        super().setUp()
        self.apple_ready = True
        self.controls.put(("transcription", "apple"))
        self.wait(lambda: siri.STT["blocked"] is None and not siri.STT["switching"])

    def start_test(self):
        import queue
        got = queue.Queue()
        self.controls.put(("mic_test", got.put))
        self.wait(lambda: self.rec.on)  # the test holds the recording
        return got

    def settle(self):
        import time
        time.sleep(0.3)

    def test_release_during_test_submits_nothing(self):
        got = self.start_test()
        self.controls.put("release")  # a stray key-up must not take the test's recording
        result = got.get(timeout=5)
        self.settle()
        self.assertEqual(result["text"], "apple heard 16000")
        self.assertEqual(self.submitted, [])

    def test_wake_mode_test_audio_never_becomes_a_command(self):
        self.controls.put(("mode", "wake"))
        self.wait(lambda: self.rec.wake)
        got = self.start_test()
        self.assertTrue(self.rec.isolated)  # wake segmentation is off for the test's audio
        import numpy as np
        self.rec.segments.put(np.ones(16000, dtype="float32"))  # a phrase that slipped into the queue anyway
        got.get(timeout=5)
        self.settle()
        self.assertFalse(self.rec.isolated)
        self.assertTrue(self.rec.segments.empty())
        self.assertEqual(self.submitted, [])

    def test_switch_then_new_ptt_is_not_taken_by_the_old_test(self):
        got = self.start_test()
        self.controls.put(("transcription", "whisper"))  # drops the test's recording
        self.wait(lambda: siri.STT["backend"] == "whisper" and not siri.STT["switching"])
        self.controls.put("press")  # a new push-to-talk recording starts while the old test is still sleeping
        self.wait(lambda: self.rec.on)
        self.assertIn("interrupted", got.get(timeout=5)["error"])
        self.assertTrue(self.rec.on)  # the stale test did not stop it
        self.controls.put("release")
        self.wait(lambda: self.submitted)
        self.assertEqual(self.submitted, [("whisper heard 16000", "voice")])

    def test_second_test_during_first_leaves_its_isolation_alone(self):
        first = self.start_test()
        second = self.mic_test()  # Settings closed and reopened: another request lands mid-test
        self.assertIn("Busy", second["error"])
        self.assertTrue(self.rec.on and self.rec.isolated)  # the first test still owns an isolated capture
        self.assertEqual(first.get(timeout=5)["text"], "apple heard 16000")
        self.assertFalse(self.rec.isolated)

    def test_stale_test_does_not_clear_a_newer_tests_isolation(self):
        import queue
        import time
        old = self.start_test()
        self.controls.put(("transcription", "apple"))  # reload drops the old test's capture
        self.wait(lambda: not self.rec.on and not siri.STT["switching"])
        time.sleep(0.15)  # the new test starts late enough that the old one finishes while it is still recording
        new = queue.Queue()
        self.controls.put(("mic_test", new.put))
        self.wait(lambda: self.rec.on)
        self.assertIn("interrupted", old.get(timeout=5)["error"])
        self.assertTrue(self.rec.on and self.rec.isolated)  # the stale finish touched nothing
        self.assertEqual(new.get(timeout=5)["text"], "apple heard 16000")
        self.assertFalse(self.rec.isolated)
        self.assertEqual(self.submitted, [])


class WakePhraseLiveTests(LoopHarness):
    def setUp(self):
        super().setUp()
        self.apple_ready = True
        self.controls.put(("transcription", "apple"))
        self.wait(lambda: siri.STT["blocked"] is None and not siri.STT["switching"])

    def test_change_applies_live_and_mic_test_reports_the_match(self):
        epoch = self.rec.epoch
        self.controls.put(("wake_phrase", "Okay Zorblat", ["OK Zorblat"]))
        self.wait(lambda: siri.WAKE.phrase == "Okay Zorblat")
        self.assertGreater(self.rec.epoch, epoch)  # queued and in-flight audio invalidated
        self.heard = "OK Zorblat, open Notes"
        r = self.mic_test()
        self.assertEqual((r["wake"], r["wake_matched"], r["command"]), ("Okay Zorblat", True, "open Notes"))
        self.assertIn("Okay Zorblat, open Spotify.", self.prompts[-1])  # Whisper-style prompt follows the phrase
        self.heard = "Hey Jev, open Notes"  # the old phrase no longer wakes it
        r = self.mic_test()
        self.assertEqual((r["wake_matched"], r["command"]), (False, None))
        self.assertEqual(self.submitted, [])

    def test_invalid_phrase_keeps_the_old_one(self):
        self.controls.put(("wake_phrase", "Hi", []))
        self.wait(lambda: any(s == "Wake phrase not changed" for s, _ in self.events))
        self.assertEqual(siri.WAKE.phrase, "Hey Jev")

    def test_change_during_transcription_drops_the_stale_transcript(self):
        import threading
        import numpy as np
        self.controls.put(("mode", "wake"))
        self.wait(lambda: self.rec.wake)
        self.gate, self.heard = threading.Event(), "Okay Zorblat, open Notes"  # would match the new phrase
        self.rec.segments.put(np.ones(16000, dtype="float32"))
        self.assertTrue(self.entered.wait(5))  # the wake worker is mid-transcription with the old phrase
        self.controls.put(("wake_phrase", "Okay Zorblat", []))
        self.wait(lambda: siri.WAKE.phrase == "Okay Zorblat")
        self.gate.set()
        import time
        time.sleep(0.3)
        self.assertEqual(self.submitted, [])

    def test_unicode_phrase_dispatches_only_when_heard_whole(self):
        import numpy as np
        self.controls.put(("wake_phrase", "Hey José", []))
        self.wait(lambda: siri.WAKE.phrase == "Hey José")
        self.controls.put(("mode", "wake"))
        self.wait(lambda: self.rec.wake)
        for heard in ["Hey Jos open Notes", "Hey Jose open Notes", "Jos open Notes"]:
            self.heard = heard
            self.rec.segments.put(np.ones(16000, dtype="float32"))
        import time
        time.sleep(0.5)
        self.assertEqual(self.submitted, [])  # shortened or changed phrases dispatch nothing
        self.heard = "Hey José open Notes"
        self.rec.segments.put(np.ones(16000, dtype="float32"))
        self.wait(lambda: self.submitted)
        self.assertEqual(len(self.submitted), 1)
        self.assertIn("open Notes", repr(self.submitted[0]))


class RecorderIsolationTests(unittest.TestCase):
    def test_stop_clears_wake_leftovers_before_the_floor_frees(self):
        import numpy as np
        rec, seen = FakeRecorder(), []
        floor = siri.Floor(rec)
        inner = floor.lock

        class Lock:  # records the recorder's state at the moment the floor is handed back
            acquire, locked = inner.acquire, inner.locked

            def release(self):
                seen.append((rec.isolated, rec.segments.qsize()))
                inner.release()
        floor.lock = Lock()
        token = floor.start_recording(isolated=True)
        self.assertTrue(rec.isolated)
        rec.segments.put(np.ones(10, dtype="float32"))  # a phrase segmented in the release gap
        floor.stop_recording(token)
        self.assertEqual(seen, [(False, 0)])
        self.assertIsNone(floor.stop_recording(token))  # stale token: no effect

    def test_isolated_capture_skips_wake_segmentation(self):
        import queue
        import numpy as np
        r = object.__new__(siri.Recorder)
        r.np, r.enabled, r.on, r.paused, r.wake, r.frames, r.isolated = np, True, True, False, True, [], True
        r.noise, r.segments = 0.005, queue.Queue()
        r._reset_segment()
        for _ in range(40):
            r._cb(np.full((1600, 1), 0.5, dtype="float32"))
        self.assertEqual(len(r.frames), 40)  # the test still records
        self.assertEqual(r.speech, [])
        self.assertTrue(r.segments.empty())


if __name__ == "__main__":
    unittest.main()
