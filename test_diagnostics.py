"""Diagnostic log: bounded, private, sanitized, never raises, and one request is traceable end to end by its id."""
import json
import os
import stat
import tempfile
import unittest
from unittest.mock import patch

import diagnostics
import engine
from test_engine import Calls, act, fake_plan


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        diagnostics.reset()
        self.dir = os.path.join(tempfile.mkdtemp(), "logs")

    def tearDown(self):
        diagnostics.reset()

    def lines(self):
        out = []
        for name in sorted(os.listdir(self.dir)):
            with open(os.path.join(self.dir, name)) as f:
                out += [json.loads(l) for l in f if l.strip()]
        return out

    def test_private_permissions_and_idempotent_init(self):
        p = diagnostics.init("i1", log_dir=self.dir)
        self.assertEqual(diagnostics.init("i2", log_dir="/elsewhere"), p)
        diagnostics.record("r", "plan", "steps")
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        [e] = self.lines()
        self.assertEqual((e["instance"], e["rid"], e["stage"], e["outcome"]), ("i1", "r", "plan", "steps"))
        self.assertIn("ts", e)

    def test_rotation_is_bounded_and_rotated_files_private(self):
        diagnostics.init("i", log_dir=self.dir, max_bytes=2000, backups=2)
        for n in range(200):
            diagnostics.record(f"r{n}", "plan", "steps", text="x" * 100)
        names = os.listdir(self.dir)
        self.assertLessEqual(len(names), 3)
        for name in names:
            path = os.path.join(self.dir, name)
            self.assertLessEqual(os.path.getsize(path), 2400)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_secrets_are_redacted_and_strings_bounded(self):
        diagnostics.init("i", log_dir=self.dir)
        key = "sk-or-v1-" + "a1B2c3D4" * 6
        mixed = "Ab3" * 12
        diagnostics.record("r", "answer", "error", headers={"Authorization": "Bearer abc"}, api_key="x",
                           nested={"token": "t", "ok": "fine"}, error=f"401 for Bearer {mixed} using {key}",
                           text="y" * 900, path="/Users/j/REPOS/74d22bbe06b4ca0468da56ccc8159826814b792a4769fafd0b0ed66f4c615031--hey-jev")
        [e] = self.lines()
        raw = json.dumps(e)
        for secret in (mixed, key, "abc"):
            self.assertNotIn(secret, raw)
        self.assertEqual(e["headers"], {"Authorization": "[redacted]"})
        self.assertEqual(e["api_key"], "[redacted]")
        self.assertEqual(e["nested"], {"token": "[redacted]", "ok": "fine"})
        self.assertLessEqual(len(e["text"]), diagnostics.MAX_STR + 1)
        self.assertIn("74d22bbe06b4ca0468da56ccc8159826814b792a4769fafd0b0ed66f4c615031", e["path"])  # hex stays

    def test_never_raises(self):
        diagnostics.record("r", "plan", "steps")  # before init: silently nothing
        blocked = os.path.join(tempfile.mkdtemp(), "file")
        open(blocked, "w").close()
        self.assertIsNone(diagnostics.init("i", log_dir=os.path.join(blocked, "sub")))  # unwritable
        diagnostics.record("r", "plan", "steps", weird=object(), nan=float("nan"))
        diagnostics.reset()
        diagnostics.init("i", log_dir=self.dir)
        with patch.object(diagnostics.json, "dumps", side_effect=RuntimeError("boom")):
            diagnostics.record("r", "plan", "steps")

    @patch.object(engine.planner, "plan", fake_plan)
    def test_one_request_traced_end_to_end(self):
        diagnostics.init("i", log_dir=self.dir)
        c = Calls()
        eng = engine.Engine(classify=None, policy=lambda: {"open": "auto"},
                            actions={"ok": act(c, "ok"), "bad": act(c, "bad", verify="failed")})
        rid = eng.submit("ok; bad", "cli")["id"]
        eng.wait(rid, 5)
        for _ in range(50):  # the done record is written just after the terminal state
            stages = [e["stage"] for e in self.lines() if e["rid"] == rid]
            if "done" in stages:
                break
            import time
            time.sleep(0.02)
        self.assertEqual(stages, ["submit", "plan", "resolve", "dispatch", "verify", "resolve", "dispatch", "verify", "done"])
        done = [e for e in self.lines() if e["rid"] == rid][-1]
        self.assertEqual((done["outcome"], done["stopped_state"]), ("partial", "failed"))
        self.assertIsInstance(done["duration_ms"], int)


if __name__ == "__main__":
    unittest.main()
