"""jevctl trials: the diagnostics log as readable attempts."""
import json
import os
import tempfile
import unittest

import trials


def rec(ts, stage, outcome, rid="r1", **kw):
    return json.dumps({"ts": ts, "instance": "i1", "rid": rid, "stage": stage, "outcome": outcome, **kw})


class TrialsTests(unittest.TestCase):
    def test_one_block_per_request_with_the_useful_parts(self):
        lines = [json.dumps({"ts": "2026-09-25T00:55:34+00:00", "instance": "i1", "rid": None, "stage": "startup",
                             "outcome": "starting", "revision": "b090623", "dirty": False, "transcription": "apple"}),
                 rec("2026-09-25T00:56:00+00:00", "submit", "queued", text="scroll down", source="voice"),
                 rec("2026-09-25T00:56:00+00:00", "recognize", "queued", stt_ms=210),
                 rec("2026-09-25T00:56:00+00:00", "classify", "ok", duration_ms=300, clause="scroll down",
                     answers={"category": ["mac_command", 0.99], "target": ["screen", 0.9],
                              "screen_action": ["none", 0.5], "media_action": ["none", 0.3]}),
                 rec("2026-09-25T00:56:00+00:00", "plan", "clarify", detail="no_action"),
                 rec("2026-09-25T00:56:00+00:00", "done", "needs_clarification", duration_ms=340, detail="no_action"),
                 rec("2026-09-25T00:56:00+00:00", "speak", "needs_clarification", line="Sorry, say that again?")]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write("\n".join(lines) + "\nnot json\n")
        self.addCleanup(os.unlink, f.name)
        text = trials.render(trials.load(f.name))
        self.assertIn("build b090623", text)
        self.assertIn("“scroll down”  [voice, stt 210 ms]  → needs_clarification (no_action)", text)
        self.assertIn("screen_action", text)       # the branch that mattered, below the gate
        self.assertNotIn("media_action", text)     # an unrelated branch isn't noise here
        self.assertIn("said: Sorry, say that again?", text)

    def test_missing_log_is_empty(self):
        self.assertEqual(trials.load("/nonexistent/log.jsonl"), [])


if __name__ == "__main__":
    unittest.main()
