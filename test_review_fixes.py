"""Regressions for the ccbc585 review: each test drives the real path Astra probed."""
import contextlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import actions
import bridge
import engine
import planner
import timers
from actions import Failed, Uncertain
from test_engine import Calls, act, fake_plan, make


def answers(**over):
    base = {"category": ("mac_command", 0.9), "compound": (False, 0.9), "target": ("volume", 0.9),
            "app_action": ("none", 0.9), "volume_action": ("mute", 0.9), "volume_scope": ("system", 0.9),
            "volume_level": ("medium", 0.9), "display_action": ("none", 0.9), "media_action": ("none", 0.9),
            "timer_action": ("none", 0.9), "system_action": ("none", 0.9)}
    base.update(over)
    return base


@patch.object(engine.planner, "plan", fake_plan)
class CancelBoundaryTests(unittest.TestCase):
    def test_cancel_during_blocked_resolver_runs_nothing(self):
        gate, c = threading.Event(), Calls()
        eng = make({"slow": act(c, "slow", resolve=lambda a: gate.wait(5) and ("target", {"app": "slow"}))})
        rid = eng.submit("slow", "cli")["id"]
        time.sleep(0.1)
        eng.cancel(rid)
        gate.set()
        r = eng.wait(rid, 5)
        self.assertEqual((r["state"], c.runs), ("cancelled", []))
        self.assertEqual(r["steps"][0]["state"], "skipped")

    def test_cancel_after_completed_step_lists_it(self):
        gate, c = threading.Event(), Calls()
        eng = make({"ok": act(c, "ok"), "slow": act(c, "slow", resolve=lambda a: gate.wait(5) and ("target", {}))})
        rid = eng.submit("ok; slow", "cli")["id"]
        time.sleep(0.1)
        eng.cancel(rid)
        gate.set()
        r = eng.wait(rid, 5)
        self.assertEqual(r["state"], "cancelled")
        self.assertEqual([s["state"] for s in r["steps"]], ["completed", "skipped"])
        self.assertEqual(c.runs, ["ok"])

    def test_post_dispatch_error_is_unknown_not_failed(self):
        c = Calls()
        eng = make({"ok": act(c, "ok"), "iffy": act(c, "iffy", run_exc=Uncertain("no tab identity returned"))})
        r = eng.wait(eng.submit("ok; iffy; ok", "cli")["id"], 5)
        self.assertEqual((r["state"], r["stopped_state"]), ("partial", "unknown"))
        self.assertEqual(r["uncertain_step"], {"index": 1, "state": "unknown"})
        self.assertEqual(r["not_started"], ["ok"])

    def test_broken_event_hook_does_not_break_the_run(self):
        c = Calls()
        eng = engine.Engine(classify=None, policy=lambda: {"open": "auto"}, actions={"ok": act(c, "ok")},
                            on_event=lambda *a: 1 / 0)
        self.assertEqual(eng.wait(eng.submit("ok", "cli")["id"], 5)["state"], "completed")


class PlannerTests(unittest.TestCase):
    def test_every_clause_gets_compound_check(self):
        per = {"mute and lock": answers(compound=(True, 0.9)), "mute": answers()}
        self.assertEqual(planner.plan("mute and lock, then mute", lambda c: per[c]), ("clarify", "compound_unsplit"))

    def test_every_clause_gets_category_check(self):
        per = {"mute": answers(), "blah blah": answers(category=("unclear", 0.9))}
        self.assertEqual(planner.plan("mute then blah blah", lambda c: per[c]), ("clarify", "unclear"))
        per["blah blah"] = answers(category=("mac_command", 0.9), volume_action=("none", 0.9), target=("app", 0.2))
        self.assertEqual(planner.plan("mute then blah blah", lambda c: per[c]), ("clarify", "no_action"))

    def test_single_clause_plan_shape(self):
        self.assertEqual(planner.plan("mute", lambda c: answers()),
                         ("steps", [{"clause": "mute", "action": "volume.mute", "args": {"level": "medium"}}]))

    def test_good_multi_clause_still_plans(self):
        kind, steps = planner.plan("mute then mute", lambda c: answers())
        self.assertEqual((kind, [s["action"] for s in steps]), ("steps", ["volume.mute", "volume.mute"]))

    def test_url_keeps_query_and_fragment(self):
        self.assertEqual(planner.url_span("go to example.com?mode=edit#section"), "example.com?mode=edit#section")
        self.assertEqual(planner.url_span("go to example.com/a?b=1."), "example.com/a?b=1")

    def test_explicit_browser_reaches_execution(self):
        """Real planner -> real resolve_url -> real run_url; only dispatch is mocked."""
        seen = {}

        def fake_open(target, deadline, _run=None, _default_browser_fn=None):
            seen.update(target)
            return None

        def boom(*a, **k):
            raise AssertionError("default lookup must not run with explicit intent")

        eng = engine.Engine(classify=lambda c: answers(target=("website", 0.9)),
                            policy=lambda: {"navigate": "auto"},
                            actions={"url.open": actions.ACTIONS["url.open"]})
        with patch.object(actions.url_adapter, "run_url_open", side_effect=fake_open), \
             patch.object(actions.url_adapter, "default_browser_for_url", side_effect=boom):
            r = eng.wait(eng.submit("go to google.com in Safari", "cli")["id"], 5)
        self.assertEqual(seen.get("browser"), "com.apple.Safari")
        self.assertEqual(seen.get("url"), "https://google.com")
        self.assertEqual(r["steps"][0]["action"], "url.open")

    def test_resolve_url_validates_browser(self):
        kind, got = actions.resolve_url({"url": "google.com", "browser": "com.apple.Safari"})
        self.assertEqual((kind, got["browser"]), ("target", "com.apple.Safari"))
        self.assertEqual(actions.resolve_url({"url": "google.com", "browser": "org.mozilla.firefox"}),
                         ("none", "unsupported browser"))


class EffectUncertaintyTests(unittest.TestCase):
    def test_native_effect_error_is_uncertain_read_error_is_failed(self):
        end = time.monotonic() + 5
        with self.assertRaises(Uncertain):
            actions.sh(["false"], end, effect=True)
        with self.assertRaises(Failed):
            actions.sh(["false"], end)

    def test_url_lookup_failure_is_failed(self):
        t = {"url": "https://example.com/"}
        with patch.object(actions.url_adapter, "default_browser_for_url", side_effect=RuntimeError("no handler")):
            with self.assertRaises(Failed):
                actions.run_url(t, time.monotonic() + 5)

    def test_url_error_after_dispatch_is_uncertain(self):
        t = {"url": "https://example.com/"}
        done = MagicMock(returncode=0, stdout="", stderr="")  # Chrome made the tab but returned no id
        with patch.object(actions.url_adapter, "default_browser_for_url", return_value="com.google.Chrome"), \
             patch.object(actions.url_adapter, "_osascript_argv", return_value=done):
            with self.assertRaises(Uncertain):
                actions.run_url(t, time.monotonic() + 5)

    def test_bad_url_is_failed_before_dispatch(self):
        with patch.object(actions.url_adapter, "default_browser_for_url") as lookup:
            with self.assertRaises(Failed):
                actions.run_url({"url": "ftp://x"}, time.monotonic() + 5)
            lookup.assert_not_called()


class ConfirmationTargetTests(unittest.TestCase):
    def tearDown(self):
        with timers.LOCK:
            timers.TIMERS.clear()

    def test_prompt_names_level_duration_and_target(self):
        self.assertEqual(actions.describe("volume.set", {"level": "quiet", "value": 25}), "Set the volume to quiet (25%)")
        self.assertEqual(actions.describe("timer.set", {"secs": 300, "label": None}), "Start a 5 minutes timer")
        self.assertEqual(actions.describe("timer.set", {"secs": 600, "label": "call mum"}), "Remind you in 10 minutes to call mum")
        self.assertEqual(actions.describe("app.quit", {"name": "Live", "path": "/Applications/Old/Live.app"}),
                         "Quit Live (in Old)")

    def test_cancel_prompt_and_run_use_the_pinned_timer(self):
        a = timers.add(300)
        kind, t = actions.resolve_timer_cancel({"text": "cancel the timer"})
        self.assertEqual(actions.describe("timer.cancel", t), "Cancel the 5 minutes timer")
        b = timers.add(900)  # arrives while the pop-down is open
        actions.run_timer_cancel(t, time.monotonic() + 1)
        with timers.LOCK:
            self.assertEqual([x["id"] for x in timers.TIMERS], [b["id"]])
        self.assertNotEqual(a["id"], b["id"])
        kind, t = actions.resolve_timer_cancel({"text": "cancel all timers"})
        self.assertEqual(actions.describe("timer.cancel", t), "Cancel all 1 timer")

    def run_ok(self, out):
        return MagicMock(returncode=0, stdout=out, stderr="")

    def test_quit_targets_the_resolved_install_only(self):
        t = {"name": "Live", "bundle_id": "com.ableton.live", "path": "/Applications/Live.app"}
        running = [("/Applications/Old/Live.app", 111), ("/Applications/Live.app", 222)]
        with patch.object(actions, "running", return_value=running), \
             patch.object(actions.subprocess, "run", return_value=self.run_ok("ready\n222 sent\n")) as run:
            actions.run_app_quit(t, time.monotonic() + 5)
        cmd = run.call_args[0][0]
        self.assertEqual((cmd[0], cmd[1], cmd[3:]), (sys.executable, "-c", ["com.ableton.live", "/Applications/Live.app", "222"]))
        self.assertLessEqual(run.call_args.kwargs["timeout"], 5)
        self.assertEqual(t["quit"], {"222": "sent"})
        with patch.object(actions, "running", return_value=running[:1]), patch.object(actions.subprocess, "run") as run:
            with self.assertRaises(Failed):  # only the other install runs: refuse, never quit it
                actions.run_app_quit(t, time.monotonic() + 5)
            run.assert_not_called()

    def test_replaced_process_is_not_terminated(self):
        with patch.object(actions, "running", return_value=[("/Applications/Live.app", 222)]), \
             patch.object(actions.subprocess, "run", return_value=self.run_ok("ready\n222 mismatch\n")):
            with self.assertRaises(Failed):
                actions.run_app_quit({"bundle_id": "b", "path": "/Applications/Live.app"}, time.monotonic() + 5)

    def test_helper_crash_after_start_is_uncertain(self):
        crash = MagicMock(returncode=1, stdout="ready\n", stderr="Traceback")
        with patch.object(actions, "running", return_value=[("/A.app", 1)]), \
             patch.object(actions.subprocess, "run", return_value=crash):
            with self.assertRaises(Uncertain):
                actions.run_app_quit({"bundle_id": "b", "path": "/A.app"}, time.monotonic() + 5)

    def test_quit_helper_rechecks_live_identity(self):
        """Real helper against the real Finder pid, with identities that do not match: never terminated."""
        [(path, pid)] = actions.running("com.apple.finder", time.monotonic() + 5)
        for bid, p in (("com.example.not-finder", path), ("com.apple.finder", "/Applications/Other.app")):
            out = actions.sh([sys.executable, "-c", actions.QUIT_HELPER, bid, p, str(pid)], time.monotonic() + 20)
            self.assertEqual(out, f"ready\n{pid} mismatch")

    def test_quit_is_bounded_by_the_deadline(self):
        with patch.object(actions, "running", return_value=[("/A.app", 1)]):
            with self.assertRaises(actions.Timeout):
                actions.run_app_quit({"bundle_id": "b", "path": "/A.app"}, time.monotonic() - 1)


class VoiceWiringTests(unittest.TestCase):
    def setUp(self):
        import siri
        self.siri = siri
        self.said = []
        self.p = patch.object(siri, "say", lambda line, notify: self.said.append(line))
        self.p.start()
        import actions
        for name in ("CHOOSE", "CLASSIFY_ITEMS", "DESKTOP", "VISIBLE"):  # make_engine wires the app's globals: put them back
            self.addCleanup(setattr, actions, name, getattr(actions, name))

    def tearDown(self):
        self.p.stop()

    def test_silent_volume_prespeech_does_not_crash(self):
        eng = self.siri.make_engine()
        step = {"index": 0, "clause": "volume silent", "action": "volume.set", "state": "running",
                "target": {"level": "silent", "value": 0}, "facts": {}, "detail": None}
        eng.on_event("step", {"id": "x", "source": "voice", "state": "running"}, step)
        self.assertEqual(len(self.said), 1)
        self.assertIn(self.said[0], self.siri.REPLIES["volume.mute"])
        eng.shutdown()

    def test_unverified_never_claims_it_happened(self):
        for line in self.siri.REPLIES["unverified"]:
            self.assertNotRegex(line.lower(), r"\bi did\b|\bdone\b")

    def test_busy_rejection_is_spoken_not_waited(self):
        eng = MagicMock(spoke_first=set())
        eng.submit.return_value = {"v": 1, "id": "x", "state": "busy", "detail": "queue_full"}
        self.siri.turn(eng, "open Safari", None)
        eng.wait.assert_not_called()
        self.assertIn(self.said[-1], self.siri.REPLIES["busy"])

    def test_late_result_is_delivered(self):
        eng = MagicMock(spoke_first=set())
        eng.submit.return_value = {"id": "x", "state": "queued"}
        done = {"id": "x", "state": "failed", "steps": []}
        eng.wait.side_effect = [{"id": "x", "state": "running"}, {"id": "x", "state": "running"}, done]
        held = []

        @contextlib.contextmanager
        def hold():
            held.append(True)
            yield

        self.siri.turn(eng, "open Safari", None, hold=hold)
        for _ in range(100):
            if self.said:
                break
            time.sleep(0.02)
        self.assertEqual(held, [True])
        self.assertIn(self.said[-1], self.siri.REPLIES["failed"])

    def test_second_instance_stops_before_microphone(self):
        tmp = tempfile.mkdtemp()
        owner = bridge.Bridge(MagicMock(), run_dir=tmp)
        owner.start()
        try:
            eng = MagicMock()
            with patch.object(bridge.Bridge.__init__, "__defaults__", (tmp,)):
                with self.assertRaises(RuntimeError):
                    self.siri.start_bridge(eng, None)
            eng.shutdown.assert_called_once()
            fake_whisper = MagicMock()
            with patch.dict(sys.modules, {"faster_whisper": fake_whisper}), patch("diagnostics.init"), \
                 patch.object(self.siri, "start_bridge", side_effect=RuntimeError("owned")), \
                 patch.object(self.siri, "Recorder") as rec:
                with self.assertRaises(RuntimeError):
                    self.siri.run_voice_assistant()
            rec.assert_not_called()
            fake_whisper.WhisperModel.assert_not_called()
        finally:
            owner.stop()
            self.siri.ENGINE.shutdown()


class FloorTests(unittest.TestCase):
    def setUp(self):
        import siri
        self.siri = siri

    def rec(self):
        r = MagicMock(on=False, paused=False)
        r.start.side_effect = lambda: setattr(r, "on", True)
        r.stop.side_effect = lambda: setattr(r, "on", False) or "audio"
        r.invalidate.side_effect = lambda: setattr(r, "on", False)
        return r

    def test_late_speech_waits_for_push_to_talk_to_end(self):
        rec = self.rec()
        floor = self.siri.Floor(rec)
        token = floor.start_recording()
        self.assertTrue(token)
        spoke = threading.Event()

        def late():
            with floor.hold():
                self.assertFalse(rec.on)  # never speaks into a live recording
                spoke.set()

        threading.Thread(target=late, daemon=True).start()
        self.assertFalse(spoke.wait(0.3))
        self.assertEqual(floor.stop_recording(token), "audio")
        self.assertTrue(spoke.wait(2))

    def test_no_recording_starts_while_jev_speaks(self):
        rec = self.rec()
        floor = self.siri.Floor(rec)
        with patch.object(self.siri.time, "sleep"):
            with floor.hold():
                self.assertFalse(floor.start_recording())
                rec.start.assert_not_called()
            self.assertTrue(floor.start_recording())

    def test_dropped_recording_frees_the_floor(self):
        floor = self.siri.Floor(self.rec())
        floor.start_recording()
        floor.drop_recording()
        self.assertFalse(floor.locked())
        floor.drop_recording()  # no recording: nothing to release
        self.assertFalse(floor.locked())

    def test_recorder_ignores_audio_while_paused(self):
        import numpy as np
        r = object.__new__(self.siri.Recorder)
        r.np, r.enabled, r.on, r.paused, r.wake, r.frames = np, True, True, True, False, []
        r._reset_segment()
        r._cb(np.ones((4, 1), dtype="float32"))
        self.assertEqual(r.frames, [])
        r.paused = False
        r._cb(np.ones((4, 1), dtype="float32"))
        self.assertEqual(len(r.frames), 1)

    def test_failed_startup_leaves_no_bridge_or_engine(self):
        for broken in ("model", "recorder"):
            fake_whisper, b, eng = MagicMock(), MagicMock(), MagicMock()
            if broken == "model":
                fake_whisper.WhisperModel.side_effect = RuntimeError("no model")
            with patch.dict(sys.modules, {"faster_whisper": fake_whisper}), patch("diagnostics.init"), \
                 patch.object(self.siri, "make_engine", return_value=eng), \
                 patch.object(self.siri, "start_bridge", return_value=b), \
                 patch.object(self.siri, "Recorder", side_effect=RuntimeError("no mic")):
                with self.assertRaises(RuntimeError):
                    self.siri.run_voice_assistant()
            b.stop.assert_called_once()
            eng.shutdown.assert_called_once()
            self.assertIsNone(self.siri.ENGINE)


class BridgeResilienceTests(unittest.TestCase):
    def setUp(self):
        self.eng = MagicMock()
        self.eng.status.return_value = {"state": "running"}
        self.b = bridge.Bridge(self.eng, run_dir=os.path.join(tempfile.mkdtemp(), "run"))

    def tearDown(self):
        self.b.stop()

    def test_bad_wait_and_op_types_rejected(self):
        h = lambda raw: self.b.handle(raw.encode())
        for wait in ("NaN", "Infinity", "-Infinity", "true"):
            self.assertEqual(h('{"v":1,"op":"status","id":"a","wait":%s}' % wait)["detail"], "bad_wait", wait)
        self.assertEqual(h('{"v":1,"op":["status"],"id":"a"}')["detail"], "bad_op_or_field")
        self.assertEqual(h('{"v":1,"op":{"a":1},"id":"a"}')["detail"], "bad_op_or_field")
        self.eng.submit.assert_not_called()
        self.eng.wait.assert_not_called()

    def test_overflow_client_that_hung_up_does_not_kill_accept_loop(self):
        self.b.slots = threading.BoundedSemaphore(1)
        self.b.slots.acquire()  # every slot taken: next clients get the overflow reply
        real, calls = bridge.Bridge._reply, []

        def reply(conn, obj):
            calls.append(obj)
            if len(calls) == 1:
                raise BrokenPipeError()
            real(conn, obj)

        with patch.object(bridge.Bridge, "_reply", staticmethod(reply)):
            self.b.start()
            for _ in range(2):
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(2)
                s.connect(self.b.sock_path)
                data = s.recv(65536)
                s.close()
        self.assertEqual(json.loads(data)["detail"], "too_many_clients")
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()


class PercentVolumeTests(unittest.TestCase):
    def plan(self, text, **over):
        return planner.plan(text, lambda c: answers(**{"volume_action": ("set", 0.9), **over}))

    def test_spoken_percent_sets_exact_value(self):
        for text, n in [("set volume to 40 percent", 40), ("set volume to one hundred percent", 100), ("set the volume to 40%", 40), ("volume to seventy-five percent", 75),
                        ("set volume to a hundred percent", 100), ("set volume to 0%", 0)]:
            kind, steps = self.plan(text)
            self.assertEqual((kind, steps[0]["action"], steps[0]["args"]), ("steps", "volume.set", {"level": f"{n}%", "percent": n}), text)
            self.assertEqual(actions.level_value(steps[0]["args"]), n)

    def test_up_to_a_percent_is_a_set_and_up_by_is_relative(self):
        self.assertEqual(self.plan("turn it up to 60%", volume_action=("up", 0.9))[1][0]["action"], "volume.set")
        step = self.plan("turn it up by ten percent", volume_action=("up", 0.9))[1][0]
        self.assertEqual((step["action"], step["args"]), ("volume.up", {"delta": 10}))
        step = self.plan("turn it down by 15%", volume_action=("down", 0.9))[1][0]
        self.assertEqual((step["action"], step["args"]), ("volume.down", {"delta": -15}))

    def test_invalid_percent_clarifies_instead_of_guessing(self):
        for text in ["set volume to -10 percent", "set volume to 12.5 percent", "set volume to two hundred percent",
                     "set volume to one hundred and five percent", "set volume to 150 percent", "set volume to minus ten percent",
                     "set volume to 1,000 percent", "set volume by 10 percent", "set volume to .5 percent",
                     "set volume to one hundred and 5 percent", "set volume to a million percent", "set volume to percent"]:
            self.assertEqual(self.plan(text), ("clarify", "bad_percent"), text)
        self.assertEqual(self.plan("open Spotify, then set volume to 12.5 percent"), ("clarify", "bad_percent"))

    def test_named_levels_unchanged(self):
        self.assertEqual(self.plan("set volume to quiet", volume_level=("quiet", 0.9))[1][0]["args"], {"level": "quiet"})
        self.assertEqual(actions.level_value({"level": "quiet"}), 25)

    def test_spotify_percent_and_describe(self):
        step = self.plan("set spotify to 30 percent", volume_scope=("spotify", 0.9))[1][0]
        self.assertEqual((step["action"], actions.level_value(step["args"])), ("spotify_volume.set", 30))
        self.assertEqual(actions.describe("volume.set", {"level": "30%", "percent": 30, "value": 30}), "Set the volume to 30%")
