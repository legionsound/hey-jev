"""Apple answers through a scripted fake helper: protocol, errors, deadline, Stop. No model inference, no network."""
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import apple_fm
import engine

FAKE = r'''
import json, sys, time
for line in sys.stdin:
    req = json.loads(line)
    if req["op"] == "quit":
        break
    if req["op"] == "models":
        out = {{"op": "models", "models": [
            {{"id": "on_device", "name": "On this Mac (Core 3)", "remote": False, "available": True, "reason": None}},
            {{"id": "private_cloud", "name": "Apple Private Cloud Compute", "remote": True, "available": False,
             "reason": "This app isn't entitled to Private Cloud Compute."}},
            {{"id": "mystery", "name": "x", "remote": False, "available": True, "reason": None}}]}}
    else:
        p = req["prompt"]
        if p == "hang":
            time.sleep(60)
        if p == "crash":
            sys.exit(3)
        status = {{"off": "unavailable", "bad": "failed"}}.get(p, "finished")
        out = {{"op": "ask", "id": req["id"], "status": status, "text": "echo " + p if status == "finished" else "",
               "error": "Apple Intelligence is off" if p == "off" else ("broke" if p == "bad" else None),
               "latency_ms": 1}}
    print(json.dumps(out), flush=True)
'''


class AppleHelperTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        with open(os.path.join(self.dir, "fake.py"), "w") as f:
            f.write(FAKE.format())
        path = os.path.join(self.dir, "heyjev-fm")  # a shell wrapper: the venv path has a space, which a shebang can't take
        with open(path, "w") as f:
            f.write(f'#!/bin/sh\nexec "{sys.executable}" "{self.dir}/fake.py"\n')
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        p = patch.object(apple_fm, "helper_path", lambda: path)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(apple_fm.shutdown)

    def test_models_keeps_known_ids_with_reasons(self):
        got = apple_fm.models()
        self.assertEqual([m["id"] for m in got], ["on_device", "private_cloud"])
        self.assertFalse(got[1]["available"])
        self.assertIn("entitled", got[1]["reason"])

    def test_answer_and_warm_reuse(self):
        self.assertEqual(apple_fm.ask("on_device", "be brief", "hi"), "echo hi")
        pid = apple_fm._proc.pid
        self.assertEqual(apple_fm.ask("on_device", "be brief", "again"), "echo again")
        self.assertEqual(apple_fm._proc.pid, pid)

    def test_unavailable_and_failed_are_distinct(self):
        with self.assertRaises(apple_fm.Unavailable) as e:
            apple_fm.ask("on_device", "", "off")
        self.assertIn("Apple Intelligence is off", str(e.exception))
        with self.assertRaises(RuntimeError):
            apple_fm.ask("on_device", "", "bad")
        with self.assertRaises(apple_fm.Unavailable):
            apple_fm.ask("gpt", "", "hi")

    def test_deadline_kills_and_reaps_then_next_call_is_fresh(self):
        apple_fm.ask("on_device", "", "warm")
        old = apple_fm._proc
        t = time.monotonic()
        with self.assertRaises(TimeoutError):
            apple_fm.ask("on_device", "", "hang", timeout=0.5)
        self.assertLess(time.monotonic() - t, 3)
        self.assertIsNotNone(old.returncode)  # reaped
        self.assertEqual(apple_fm.ask("on_device", "", "after"), "echo after")  # nothing replayed
        self.assertIsNot(apple_fm._proc, old)

    def test_crash_fails_the_question_without_replay(self):
        with self.assertRaises(RuntimeError):
            apple_fm.ask("on_device", "", "crash")
        self.assertEqual(apple_fm.ask("on_device", "", "next"), "echo next")

    def test_interrupt_unblocks_a_waiting_answer(self):
        errors = []
        th = threading.Thread(target=lambda: self._catch(errors, lambda: apple_fm.ask("on_device", "", "hang")))
        th.start()
        time.sleep(0.5)
        t = time.monotonic()
        apple_fm.interrupt()
        th.join(5)
        self.assertFalse(th.is_alive())
        self.assertLess(time.monotonic() - t, 3)
        self.assertEqual(len(errors), 1)

    @staticmethod
    def _catch(errors, fn):
        try:
            fn()
        except Exception as exc:
            errors.append(exc)

    def test_missing_helper_is_unavailable(self):
        apple_fm.shutdown()
        with patch.object(apple_fm, "helper_path", lambda: None):
            with self.assertRaises(apple_fm.Unavailable):
                apple_fm.models()


@patch.object(engine.planner, "plan", lambda text, classify, can_answer=False: ("answer", None))
class StopDuringAnswerTests(unittest.TestCase):
    def test_stop_interrupts_and_drops_the_late_answer(self):
        started, release, interrupted = threading.Event(), threading.Event(), []

        def answer(text):
            started.set()
            release.wait(5)
            return "late answer"
        eng = engine.Engine(classify=None, answer=answer)
        eng.interrupt_answer = lambda: (interrupted.append(1), release.set())
        rid = eng.submit("what is up", "cli")["id"]
        self.assertTrue(started.wait(5))
        eng.cancel(rid)
        r = eng.wait(rid, 5)
        self.assertEqual(r["state"], "cancelled")
        self.assertEqual(interrupted, [1])
        self.assertNotEqual(r.get("say"), "late answer")

    def test_stop_while_failing_reports_cancelled(self):
        started = threading.Event()
        gate = threading.Event()

        def answer(text):
            started.set()
            gate.wait(5)
            raise RuntimeError("Apple model helper stopped.")
        eng = engine.Engine(classify=None, answer=answer)
        eng.interrupt_answer = gate.set
        rid = eng.submit("what is up", "cli")["id"]
        started.wait(5)
        eng.cancel(rid)
        self.assertEqual(eng.wait(rid, 5)["state"], "cancelled")


class SettingTests(unittest.TestCase):
    def test_apple_is_a_valid_answer_provider(self):
        import secrets_store
        self.assertIn("apple", secrets_store.SETTINGS["ANSWER_PROVIDER"])
        with patch.object(secrets_store, "get_secret", lambda n: "apple" if n == "ANSWER_PROVIDER" else None):
            self.assertEqual(secrets_store.get_setting("ANSWER_PROVIDER"), "apple")
            self.assertEqual(secrets_store.missing_secrets("openrouter", "apple"), ["FISH_AUDIO_API_KEY",
                                                                                  "JEV_OPENROUTER_API_KEY"])

    def test_apple_model_pref_validates(self):
        import model_settings
        with self.assertRaises(ValueError):
            model_settings.save_apple_model("gpt")


if __name__ == "__main__":
    unittest.main()
