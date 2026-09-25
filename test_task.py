"""Multi-step tasks through the real engine, with the screen and Jev faked: gates, stops and what Jev is shown."""
import json
import unittest
from unittest.mock import patch

import actions
import diagnostics
import planner
import screen
import task
from engine import Engine


def item(n, label, source="ax", role="AXButton", frame=(10, 10, 80, 30), pressable=True, **kw):
    return screen.Item(n, source, role, label, frame, pressable, ref=kw.pop("ref", None) or object(), **kw)


WIN = object()


def snap(items, window=WIN, pid=7, started="s"):
    return screen.Snapshot(pid, "Pad", "com.pad", "Pad", (0, 0, 400, 300), items, window_ref=window, started=started)


class Screen:
    """A scripted screen: each observe returns the next snapshot (the last one repeats). Presses flip a value."""

    def __init__(self, test, snaps):
        self.snaps, self.i, self.presses, self.state = list(snaps), 0, [], {"exists": "yes", "AXValue": "0"}
        fns = {"observe": self.observe, "signature": lambda pid, d: {"n": self.i},
               "element_state": lambda ref, d: dict(self.state), "press": self.press,
               "focused_field": lambda pid, d: None, "process_start": lambda pid, d: "s"}
        for name, fn in fns.items():
            p = patch.object(screen, name, fn)
            p.start()
            test.addCleanup(p.stop)

    def observe(self, pid=None, ocr=True, deadline=None):
        s = self.snaps[min(self.i, len(self.snaps) - 1)]
        return s

    def press(self, ref, deadline):
        self.presses.append(ref)
        self.state = {**self.state, "AXValue": str(len(self.presses))}
        self.i += 1  # the next observation shows the next screen
        return 0


class TaskTests(unittest.TestCase):
    def setUp(self):
        for p in (patch.object(diagnostics, "record", lambda *a, **k: None),):
            p.start()
            self.addCleanup(p.stop)
        self.asked, self.sent = [], []

    def run_task(self, goal, answers, policy=None, cancel_after=None):
        plan = [{"clause": f"take over: {goal}", "action": "task.run", "args": {"goal": goal}}]
        replies = iter(answers)

        def jev(state, questions):
            self.sent.append((json.loads(state), questions))
            if cancel_after is not None and len(self.sent) > cancel_after:
                eng.cancel(rid)
            return next(replies)

        def ask(pending):
            if pending:
                self.asked.append(pending["text"])
                eng.decide(pending["token"], True)
        with patch.object(planner, "plan", lambda *a, **k: ("steps", plan)):
            eng = Engine(lambda _: {}, policy=lambda: {**actions.DEFAULT_POLICY, **(policy or {})}, ask=ask)
            eng.task_jev = jev
            rid = eng.submit(f"take over: {goal}", "cli")["id"]
            return eng.wait(rid, 20)

    def test_the_words_that_start_a_task(self):
        self.assertEqual(task.goal_of("take over: find the cheapest flight"), "find the cheapest flight")
        self.assertEqual(task.goal_of("Work on turn on Loud mode."), "turn on Loud mode")
        for said in ["find the cheapest flight", "take over", "open Notes", "work on"]:
            self.assertIsNone(task.goal_of(said), said)
        kind, steps = planner.plan("take over: find a flight, then book it", lambda _: {})
        self.assertEqual((kind, steps[0]["action"], steps[0]["args"]), ("steps", "task.run", {"goal": "find a flight, then book it"}))

    def test_one_ok_for_the_task_then_jev_drives_verified_steps(self):
        loud = item(1, "Loud mode", role="AXCheckBox")
        Screen(self, [snap([loud]), snap([loud])])
        v = self.run_task("turn on Loud mode", [{"kind": ("press_item", 0.95), "item": ("i0", 0.9)},
                                                {"kind": ("done", 0.9)}])
        self.assertEqual(self.asked, ["Work on: turn on Loud mode (up to 15 steps)"])  # one OK; the press was covered
        self.assertEqual([s["state"] for s in v["steps"]], ["unverified", "completed"])
        self.assertEqual((v["state"], v["steps"][0]["detail"]), ("unverified", "Jev judged it done; not checked"))

    def test_done_is_verified_only_against_the_screen(self):
        Screen(self, [snap([item(1, "Saved to Downloads", source="ocr", role="text", pressable=False)])])
        v = self.run_task('save it and show "Saved"', [{"kind": ("done", 0.9)}])
        self.assertEqual((v["state"], v["steps"][0]["detail"]), ("completed", "done, checked on screen"))

    def test_steps_ask_each_time_when_the_task_ok_does_not_cover_them(self):
        a = item(1, "Next")
        Screen(self, [snap([a]), snap([a])])
        self.run_task("go on", [{"kind": ("press_item", 0.9), "item": ("i0", 0.9)}, {"kind": ("done", 0.9)}],
                      policy={"in_task": "ask"})
        self.assertEqual(len(self.asked), 2)  # the task, and the click

    def test_risky_buttons_ask_inside_tasks_unless_turned_off(self):
        buy = item(1, "Buy now")
        Screen(self, [snap([buy]), snap([buy])])
        self.run_task("get it", [{"kind": ("press_item", 0.9), "item": ("i0", 0.9)}, {"kind": ("done", 0.9)}])
        self.assertEqual(len(self.asked), 2)
        self.asked.clear()
        Screen(self, [snap([buy]), snap([buy])])
        self.run_task("get it", [{"kind": ("press_item", 0.9), "item": ("i0", 0.9)}, {"kind": ("done", 0.9)}],
                      policy={"risky": "auto", "task": "auto"})
        self.assertEqual(self.asked, [])  # everything automatic, as Johnny can choose

    def test_a_step_that_is_not_verified_ends_the_task_and_is_never_retried(self):
        b = item(1, "Next")
        s = Screen(self, [snap([b])])
        s.press = lambda ref, d: (s.presses.append(ref), 0)[1]  # nothing about the control changes
        patch.object(screen, "press", s.press).start()
        v = self.run_task("go on", [{"kind": ("press_item", 0.9), "item": ("i0", 0.9)}] * 5)
        self.assertEqual((v["state"], len(s.presses)), ("unverified", 1))
        self.assertIn("couldn't check", v["steps"][0]["detail"])

    def test_unsure_invalid_and_stuck_all_stop(self):
        for answers, why in [([{"kind": ("press_item", 0.4), "item": ("i0", 0.9)}], "not sure what to do next"),
                             ([{"kind": ("press_item", 0.9), "item": ("i9", 0.9)}], "Jev's answer didn't fit this screen"),
                             ([{"kind": ("fly", 0.9)}], "Jev's answer didn't fit this screen"),
                             ([{"kind": ("done", float("inf"))}], "Jev's answer didn't fit this screen"),
                             ([{"kind": ("stuck", 0.9)}], "nothing here helps")]:
            s = Screen(self, [snap([item(1, "Next")])])
            v = self.run_task("go on", answers)
            self.assertEqual((v["steps"][0]["detail"], s.presses), (why, []), answers)

    def test_ocr_text_is_context_never_a_press_target(self):
        Screen(self, [snap([item(1, "Pay", source="ocr", role="text", pressable=False), item(2, "Next")])])
        v = self.run_task("go on", [{"kind": ("press_item", 0.9), "item": ("i0", 0.9)}])
        self.assertEqual(v["steps"][0]["detail"], "Jev's answer didn't fit this screen")
        crit = self.sent[0][1]["item"]["criteria"]
        self.assertEqual(list(crit), ["i1"])

    def test_field_text_read_off_the_pixels_is_never_sent(self):
        field = item(1, "Message", role="AXTextField", pressable=False, frame=(10, 100, 300, 30))
        leak = item(2, "my password is hunter2", source="ocr", role="text", pressable=False, frame=(20, 105, 200, 20))
        Screen(self, [snap([field, leak, item(3, "Next")])])
        self.run_task("go on", [{"kind": ("stuck", 0.9)}])
        self.assertNotIn("hunter2", json.dumps(self.sent[0][0]))

    def test_a_window_the_user_brings_forward_stops_the_task(self):
        s = Screen(self, [snap([item(1, "Next")]), snap([item(1, "Next")], window=object())])
        v = self.run_task("go on", [{"kind": ("press_item", 0.9), "item": ("i0", 0.9)}, {"kind": ("done", 0.9)}])
        self.assertEqual((v["steps"][0]["detail"], len(s.presses)), ("the window changed under me", 1))

    def test_a_window_our_own_verified_step_opened_is_followed(self):
        other = object()
        s = Screen(self, [snap([item(1, "Open")]), snap([item(1, "Close")], window=other)])
        s.sig_window = [WIN]
        patch.object(screen, "signature", lambda pid, d: {"window": WIN if not s.presses else other}).start()
        v = self.run_task("go on", [{"kind": ("press_item", 0.9), "item": ("i0", 0.9)}, {"kind": ("done", 0.9)}])
        self.assertEqual(v["steps"][0]["detail"], "Jev judged it done; not checked")  # it carried on in the new window
        self.assertTrue(v["steps"][1]["facts"]["window_changed"])

    def test_another_app_coming_forward_stops_the_task(self):
        s = Screen(self, [snap([item(1, "Next")]), snap([item(1, "Other")])])
        real = s.observe

        def observe(pid=None, ocr=True, deadline=None):  # task observations after the first step see another app
            got = real(pid, ocr, deadline)
            return snap(got.items, started="other") if ocr and s.presses else got
        with patch.object(screen, "observe", observe):
            v = self.run_task("go on", [{"kind": ("press_item", 0.9), "item": ("i0", 0.9)}, {"kind": ("done", 0.9)}])
        self.assertEqual((v["steps"][0]["detail"], len(s.presses)), ("the app changed under me", 1))

    def test_going_in_circles_stops(self):
        a = item(1, "Toggle")
        s = Screen(self, [snap([a])])
        s.press = lambda ref, d: (s.presses.append(ref), s.state.update(AXValue=str(len(s.presses))), 0)[2]
        patch.object(screen, "press", s.press).start()
        v = self.run_task("go on", [{"kind": ("press_item", 0.9), "item": ("i0", 0.9)}] * 6)
        self.assertIn(v["steps"][0]["detail"], ("going in circles", "stuck: nothing changed"))
        self.assertLess(len(s.presses), 6)

    def test_step_limit(self):
        s = Screen(self, [snap([item(1, f"Next {k}")]) for k in range(1, 40)])
        with patch.object(task, "MAX_STEPS", 3):
            v = self.run_task("go on", [{"kind": ("press_item", 0.9), "item": ("i0", 0.9)}] * 10)
        self.assertEqual((v["steps"][0]["detail"], len(s.presses)), ("step limit", 3))

    def test_stop_mid_task(self):
        s = Screen(self, [snap([item(1, f"Next {k}")]) for k in range(1, 40)])
        v = self.run_task("go on", [{"kind": ("press_item", 0.9), "item": ("i0", 0.9)}] * 10, cancel_after=1)
        self.assertEqual(v["state"], "cancelled")
        self.assertLessEqual(len(s.presses), 1)

    def test_declining_the_task_does_nothing(self):
        s = Screen(self, [snap([item(1, "Next")])])
        plan = [{"clause": "take over: go on", "action": "task.run", "args": {"goal": "go on"}}]
        with patch.object(planner, "plan", lambda *a, **k: ("steps", plan)):
            eng = Engine(lambda _: {}, policy=lambda: dict(actions.DEFAULT_POLICY),
                         ask=lambda p: p and eng.decide(p["token"], False))
            eng.task_jev = lambda *a: self.fail("Jev must not be asked")
            v = eng.wait(eng.submit("take over: go on", "cli")["id"], 10)
        self.assertEqual((v["state"], s.presses), ("declined", []))


if __name__ == "__main__":
    unittest.main()
