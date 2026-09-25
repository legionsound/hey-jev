"""jevctl trials: the diagnostics log as readable attempts."""
import datetime
import json
import os
import tempfile
import unittest

import trials


def rec(ts, stage, outcome, rid="r1", **kw):
    return json.dumps({"ts": ts, "instance": "i1", "rid": rid, "stage": stage, "outcome": outcome, **kw})


def start(ts, instance, revision):
    return json.dumps({"ts": ts, "instance": instance, "rid": None, "stage": "startup", "outcome": "starting",
                       "revision": revision, "dirty": False, "transcription": "apple"})


def ago(minutes):
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes)).isoformat()


def req(ts, instance, rid, text):
    return [json.dumps({"ts": ts, "instance": instance, "rid": rid, "stage": "submit", "outcome": "queued",
                        "text": text, "source": "cli"}),
            json.dumps({"ts": ts, "instance": instance, "rid": rid, "stage": "done", "outcome": "completed",
                        "duration_ms": 100})]


class TrialsTests(unittest.TestCase):
    def load(self, lines, **kw):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write("\n".join(lines) + "\n")
        self.addCleanup(os.unlink, f.name)
        return trials.load(f.name, **kw)

    def builds(self, events):
        """-> [(heading revision, request text, request's own build)] per request."""
        out, head = [], None
        for e in events:
            if e["kind"] == "startup":
                head = e["revision"]
            else:
                s = trials.summarize(e)
                out.append((head, s.get("heard"), s.get("build")))
        return out

    def test_reused_id_across_a_restart_stays_two_attempts(self):
        events = self.load([start("2026-09-25T01:00:00+00:00", "A", "aaa1111"),
                            *req("2026-09-25T01:00:05+00:00", "A", "same", "first"),
                            start("2026-09-25T01:01:00+00:00", "B", "bbb2222"),
                            *req("2026-09-25T01:01:05+00:00", "B", "same", "second")])
        self.assertEqual(self.builds(events), [("aaa1111", "first", "aaa1111"), ("bbb2222", "second", "bbb2222")])
        self.assertEqual([(s["rid"], s["instance"]) for s in map(trials.summarize, [e for e in events
                          if e["kind"] == "request"])], [("same", "A"), ("same", "B")])

    def test_since_keeps_whole_requests_and_their_startup(self):
        split = [json.dumps({"ts": ago(12), "instance": "A", "rid": "old", "stage": "submit", "outcome": "queued",
                             "text": "began before", "source": "cli"}),
                 json.dumps({"ts": ago(8), "instance": "A", "rid": "old", "stage": "done", "outcome": "completed",
                             "duration_ms": 4000})]
        events = self.load([start(ago(60), "A", "aaa1111"), *split, *req(ago(5), "A", "new", "inside")],
                           since_minutes=10)
        self.assertEqual(self.builds(events), [("aaa1111", "inside", "aaa1111")])
        self.assertEqual(len(events[-1]["records"]), 2)

    def test_last_two_across_two_startups_keeps_both_builds(self):
        events = self.load([start("2026-09-25T01:00:00+00:00", "A", "aaa1111"),
                            *req("2026-09-25T01:00:05+00:00", "A", "r1", "on A"),
                            start("2026-09-25T01:01:00+00:00", "B", "bbb2222"),
                            *req("2026-09-25T01:01:05+00:00", "B", "r2", "on B")], last=2)
        self.assertEqual(self.builds(events), [("aaa1111", "on A", "aaa1111"), ("bbb2222", "on B", "bbb2222")])

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
