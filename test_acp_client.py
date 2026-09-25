"""ACP client against the scripted fake agent (test_fixtures/fake_acp.py). No real agent, network or windows."""
import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

import acp_client
import engine

FAKE = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_fixtures", "fake_acp.py")]


def session():
    s = acp_client.Session("claude", os.path.expanduser("~"), command=FAKE)
    return s


class AcpClientTests(unittest.TestCase):
    def setUp(self):
        self.s = session()
        self.addCleanup(self.s.kill)

    def test_turn_streams_text_and_tool_activity(self):
        seen = []
        reply, stop = self.s.prompt("hello", on_update=lambda kind, u: seen.append((kind, u.get("title"))))
        self.assertEqual((reply, stop), ("Fake answer part one. Fake answer part two.", "end_turn"))
        self.assertIn(("tool_call", "ls"), seen)

    def test_session_is_reused_across_turns(self):
        self.s.prompt("one")
        pid, sid = self.s.proc.pid, self.s.session_id
        self.s.prompt("two")
        self.assertEqual((self.s.proc.pid, self.s.session_id), (pid, sid))

    def test_permission_goes_to_the_user(self):
        asked = []

        def allow(tool, options):
            asked.append(tool.get("title"))
            return next(o["optionId"] for o in options if o["kind"] == "allow_once")
        reply, stop = self.s.prompt("permission please", permission=allow)
        self.assertEqual(stop, "end_turn")
        self.assertEqual(len(asked), 1)
        self.assertIn("allow", reply)

    def test_no_answer_means_the_offered_reject(self):
        reply, stop = self.s.prompt("permission please", permission=lambda tool, options: None)
        self.assertIn("reject", reply)
        reply, stop = self.s.prompt("permission please", permission=lambda tool, options: "made-up-id")
        self.assertIn("reject", reply)  # an id the agent didn't offer is never sent

    def test_crash_fails_the_turn_and_the_next_one_starts_fresh(self):
        self.s.prompt("warm")
        with self.assertRaises(RuntimeError):
            self.s.prompt("crash now")
        reply, stop = self.s.prompt("after")
        self.assertEqual(stop, "end_turn")  # fresh adapter, and the crashed prompt was not replayed

    def test_stop_cancels_then_kills_a_wedged_adapter(self):
        self.s.prompt("warm")
        out = []
        th = threading.Thread(target=lambda: out.append(self.s.prompt("hang forever")))
        th.start()
        time.sleep(0.5)
        t = time.monotonic()
        with patch.object(acp_client, "CANCEL_GRACE", 0.5):
            self.s.cancel()
            th.join(5)
        self.assertFalse(th.is_alive())
        self.assertLess(time.monotonic() - t, 4)
        self.assertEqual(out[0][1], "cancelled")
        self.assertFalse(self.s.alive())

    def test_missing_adapter_is_unavailable(self):
        with patch.object(acp_client, "find_adapter", lambda name: None):
            s = acp_client.Session("codex", os.path.expanduser("~"))
            with self.assertRaises(acp_client.Unavailable):
                s.start()


class ConfirmOutsideTests(unittest.TestCase):
    def test_agent_request_uses_the_popdown_and_stop_declines_it(self):
        shown = []
        eng = engine.Engine(classify=None, ask=lambda p: shown.append(p))
        got = []
        th = threading.Thread(target=lambda: got.append(eng.confirm_outside("Claude Code: Run ls", source="claude")))
        th.start()
        for _ in range(50):
            if shown:
                break
            time.sleep(0.02)
        self.assertEqual(shown[0]["source"], "claude")
        self.assertTrue(eng.decide(shown[0]["token"], True))
        th.join(2)
        self.assertEqual(got, ["confirmed"])
        self.assertIsNone(shown[-1])  # pop-down closed
        th = threading.Thread(target=lambda: got.append(eng.confirm_outside("Codex: edit file", source="codex")))
        th.start()
        time.sleep(0.2)
        eng.cancel_outside()
        th.join(2)
        self.assertEqual(got[-1], "cancelled")

    def test_busy_when_another_confirmation_is_open(self):
        eng = engine.Engine(classify=None, ask=lambda p: None)
        eng.pending = {"token": "x", "id": "r", "decision": None}
        self.assertEqual(eng.confirm_outside("Claude Code: x"), "busy")


class SpokenSummaryTests(unittest.TestCase):
    def test_say_line_wins_then_last_paragraph(self):
        import siri
        self.assertEqual(siri.spoken_summary("Long **work** log.\n\nSay: Fixed the **bug**."), "Fixed the bug.")
        self.assertEqual(siri.spoken_summary("Warning: skills.\n\nDid A. Did B. Did C."), "Did A. Did B.")
        self.assertEqual(siri.spoken_summary(""), "Done.")


if __name__ == "__main__":
    unittest.main()
