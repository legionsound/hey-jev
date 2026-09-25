"""Ideas taken from the fka.dev Jev computer-use write-up: Jev's second look at a press's consequences, on-device
writing into fields, and the Jev cursor. Offline: fake screen, fake Jev, fake writer."""
import json
import math
import unittest
from unittest.mock import patch

import actions
import diagnostics
import engine
import planner
import screen
from engine import Engine
from test_screen import FakeScreen, TypeTests, item, snap


class ConsequenceTests(unittest.TestCase):
    """The press path the app takes: planner step -> Engine._step -> confirmation gate -> press."""

    def setUp(self):
        p = patch.object(diagnostics, "record", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)
        self.calls, self.asked = [], []

    def run_press(self, label, p_yes=0.9, policy=None, decide=None):
        def consequence(clause, action, target):
            self.calls.append((clause, action, target["label"]))
            if isinstance(p_yes, Exception):
                raise p_yes
            return p_yes

        def ask(pending):
            if pending:
                self.asked.append(pending["text"])
                eng.decide(pending["token"], decide)
        step = {"clause": f"click {label}", "action": "screen.press", "args": {"label": label}}
        with patch.object(planner, "plan", lambda *a, **k: ("steps", [step])):
            eng = Engine(lambda _: {}, policy=lambda: {**actions.DEFAULT_POLICY, "click": "auto", **(policy or {})},
                         ask=ask if decide is not None else None)
            eng.consequence = consequence
            return eng.wait(eng.submit(f"click {label}", "cli")["id"], 10)

    def test_a_press_jev_calls_consequential_asks_first_even_with_clicks_automatic(self):
        fake = FakeScreen(self, [item(1, "Archive")])  # no rule word: only Jev's look can catch it
        v = self.run_press("Archive", p_yes=0.9)
        self.assertEqual((v["state"], v["steps"][0]["detail"], fake.presses), ("declined", "no_confirmation_ui", []))
        self.assertEqual(self.calls, [("click Archive", "screen.press", "Archive")])
        self.assertEqual(v["steps"][0]["facts"]["consequence"], {"p_yes": 0.9, "gate": engine.CONSEQUENCE_GATE})

    def test_confirmed_consequential_press_goes_ahead(self):
        fake = FakeScreen(self, [item(1, "Archive")])
        v = self.run_press("Archive", p_yes=0.9, decide=True)
        self.assertEqual((v["state"], len(fake.presses), len(self.asked)), ("completed", 1, 1))

    def test_a_press_jev_calls_harmless_runs_without_asking(self):
        fake = FakeScreen(self, [item(1, "Archive")])
        v = self.run_press("Archive", p_yes=0.1)
        self.assertEqual((v["state"], len(fake.presses)), ("completed", 1))

    def test_a_failed_or_malformed_check_asks_it_never_means_no(self):
        for bad in (RuntimeError("Jev down"), float("nan"), 1.5, True, "yes"):
            fake = FakeScreen(self, [item(1, "Archive")])
            v = self.run_press("Archive", p_yes=bad)
            self.assertEqual((v["state"], fake.presses), ("declined", []), bad)
            self.assertIn("error", v["steps"][0]["facts"]["consequence"])

    def test_not_asked_when_the_press_asks_anyway_or_risky_buttons_are_automatic(self):
        FakeScreen(self, [item(1, "Archive")])
        self.run_press("Archive", policy={"click": "ask"})  # asks already: no extra Jev call
        FakeScreen(self, [item(1, "Delete it")])
        self.run_press("Delete it")  # the label rule already asks
        fake = FakeScreen(self, [item(1, "Archive")])
        v = self.run_press("Archive", policy={"risky": "auto"})  # Johnny turned risky asks off: honoured
        self.assertEqual((self.calls, v["state"], len(fake.presses)), ([], "completed", 1))

    def test_a_slow_check_is_bounded_and_asks(self):
        import time
        fake = FakeScreen(self, [item(1, "Archive")])
        slow = lambda *a: (time.sleep(2), 0.0)[1]
        with patch.object(engine, "CONSEQUENCE_TIMEOUT", 0.2):
            step = {"clause": "click Archive", "action": "screen.press", "args": {"label": "Archive"}}
            with patch.object(planner, "plan", lambda *a, **k: ("steps", [step])):
                eng = Engine(lambda _: {}, policy=lambda: {**actions.DEFAULT_POLICY, "click": "auto"})
                eng.consequence = slow
                t = time.monotonic()
                v = eng.wait(eng.submit("click Archive", "cli")["id"], 10)
        self.assertLess(time.monotonic() - t, 1.0)  # not the 2 s the call takes
        self.assertEqual((v["state"], fake.presses), ("declined", []))
        self.assertIn("timeout", v["steps"][0]["facts"]["consequence"]["error"])

    def test_stop_during_the_check_ends_the_step_at_once(self):
        import threading, time
        fake = FakeScreen(self, [item(1, "Archive")])
        started = threading.Event()
        step = {"clause": "click Archive", "action": "screen.press", "args": {"label": "Archive"}}
        with patch.object(planner, "plan", lambda *a, **k: ("steps", [step])):
            eng = Engine(lambda _: {}, policy=lambda: {**actions.DEFAULT_POLICY, "click": "auto"})
            eng.consequence = lambda *a: (started.set(), time.sleep(2), 0.0)[2]
            rid = eng.submit("click Archive", "cli")["id"]
            started.wait(5)
            t = time.monotonic()
            eng.cancel(rid)
            v = eng.wait(rid, 10)
        self.assertLess(time.monotonic() - t, 0.5)
        self.assertEqual((v["steps"][0]["state"], fake.presses), ("skipped", []))

    def test_only_presses_and_return_get_the_check(self):
        self.assertEqual(engine.CONSEQUENCE_ACTIONS, ("screen.press", "screen.pick", "screen.submit"))


class TaskConsequenceTests(unittest.TestCase):
    """Inside a task whose one OK covers each click, Jev's look still stops a consequential press."""

    def setUp(self):
        from test_task import Screen, item as titem, snap as tsnap
        p = patch.object(diagnostics, "record", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)
        self.Screen, self.titem, self.tsnap = Screen, titem, tsnap
        self.sent, self.asked, self.checked = [], [], []

    def test_covered_task_click_still_asks_when_jev_says_consequential(self):
        b = self.titem(1, "Merge")
        self.Screen(self, [self.tsnap([b]), self.tsnap([b])])
        answers = iter([{"kind": ("press_item", 0.9), "item": ("i0", 0.9)}, {"kind": ("done", 0.9)}])

        def ask(pending):
            if pending:
                self.asked.append(pending["text"])
                eng.decide(pending["token"], True)
        plan = [{"clause": "take over: merge it", "action": "task.run", "args": {"goal": "merge it"}}]
        with patch.object(planner, "plan", lambda *a, **k: ("steps", plan)):
            eng = Engine(lambda _: {}, policy=lambda: {**actions.DEFAULT_POLICY, "task": "auto"}, ask=ask)
            eng.task_jev = lambda state, q: next(answers)
            eng.consequence = lambda clause, action, target: (self.checked.append(target["label"]), 0.95)[1]
            eng.wait(eng.submit("take over: merge it", "cli")["id"], 20)
        self.assertEqual(self.checked, ["Merge"])
        self.assertEqual(len(self.asked), 1)  # task start was automatic; the Merge click asked


    def test_app_switch_during_the_cursor_glide_stops_the_task_press(self):
        b = self.titem(1, "Next")
        fake = self.Screen(self, [self.tsnap([b]), self.tsnap([b])])
        answers = iter([{"kind": ("press_item", 0.9), "item": ("i0", 0.9)}, {"kind": ("done", 0.9)}])
        plan = [{"clause": "take over: go on", "action": "task.run", "args": {"goal": "go on"}}]
        with patch.object(planner, "plan", lambda *a, **k: ("steps", plan)):
            eng = Engine(lambda _: {}, policy=lambda: {**actions.DEFAULT_POLICY, "task": "auto"})
            eng.task_jev = lambda state, q: next(answers)
            eng.consequence = lambda *a: 0.0
            eng.point = lambda target, wait: setattr(fake, "front", 999)  # another app came forward mid-glide
            v = eng.wait(eng.submit("take over: go on", "cli")["id"], 20)
        self.assertEqual(fake.presses, [])
        self.assertNotEqual(v["steps"][-1]["state"], "completed")


class ComposeWordsTests(unittest.TestCase):
    def test_writing_openings(self):
        cases = {"write a reply saying I'll be late": {"compose": "a reply saying I'll be late"},
                 "please write me a haiku about rain": {"compose": "a haiku about rain"},
                 "draft a thank-you note to Sam in the message field": {"compose": "a thank-you note to Sam",
                                                                        "field": "message"},
                 "reply to Sam saying on my way": {"compose": "reply to Sam saying on my way"},
                 "respond with a thumbs up": {"compose": "respond with a thumbs up"}}
        for said, args in cases.items():
            self.assertEqual(planner.direct(said), ("screen.type", args), said)

    def test_literal_typing_and_other_commands_are_untouched(self):
        for said in ("write a reply saying hi into", "write a reply saying hi into the", "draft a note in",
                     "write hello", 'write "a note" into the Name field', "type a quick note", "reply all",
                     "click reply", "draft it", "answer with yes"):
            self.assertIsNone(planner.compose_args(said), said)
        self.assertEqual(planner.type_args("write hello"), {"text": "hello"})


class ComposeTests(unittest.TestCase):
    """From the words, through the real planner and engine, to the field."""

    def setUp(self):
        TypeTests.setUp(self)
        self.wrote = []
        self.reply = "Running about ten minutes late, sorry!"
        p = patch.object(actions, "COMPOSE", self.write)
        p.start()
        self.addCleanup(p.stop)

    insert = TypeTests.insert

    def write(self, request, context, timeout):
        self.wrote.append((request, context))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def say(self, words, policy=None, decide=None):
        asked = []

        def ask(pending):
            if pending:
                asked.append(pending["text"])
                eng.decide(pending["token"], decide)
        eng = Engine(lambda _: {}, policy=lambda: policy or {**actions.DEFAULT_POLICY, "type": "auto"},
                     ask=ask if decide is not None else None)
        v = eng.wait(eng.submit(words, "cli")["id"], 10)
        return v, asked

    def test_written_text_is_typed_and_checked_like_typed_words(self):
        v, _ = self.say("write a reply saying I'll be late")
        self.assertEqual((v["state"], self.inserts), ("completed", [self.reply]))
        request, context = self.wrote[0]
        self.assertEqual(request, "a reply saying I'll be late")
        self.assertEqual((context["app"], context["field"]), ("Pad", "the selected field"))

    def test_the_confirmation_shows_the_draft_and_exactly_that_draft_is_typed(self):
        self.reply = "word " * 399 + "END"  # near the 2,000 limit: all of it must be reviewable
        pend = []
        eng = Engine(lambda _: {}, policy=lambda: dict(actions.DEFAULT_POLICY),
                     ask=lambda p: p and (pend.append(p), eng.decide(p["token"], True)))
        v = eng.wait(eng.submit("write a reply saying I'll be late", "cli")["id"], 10)
        self.assertEqual(len(self.wrote), 1)  # the re-check after OK reuses the draft: never a second, unseen one
        self.assertEqual((pend[0]["draft"], pend[0]["text"]), (self.reply, "Type this into the selected field in Pad"))
        self.assertEqual((v["state"], self.inserts), ("completed", [self.reply]))

    def test_no_writer_or_a_failing_one_stops_and_types_nothing(self):
        self.reply = RuntimeError("Apple's on-device model can't write right now: Apple Intelligence is off.")
        v, _ = self.say("write a reply saying I'll be late")
        self.assertEqual((v["state"], self.inserts), ("failed", []))
        self.assertIn("Apple Intelligence is off", v["steps"][0]["detail"])
        for empty in ("", "   ", "x" * 2001, None):
            self.reply = empty
            v, _ = self.say("write a reply saying I'll be late")
            self.assertEqual((v["state"], self.inserts), ("failed", []), empty)
        with patch.object(actions, "COMPOSE", None):
            v, _ = self.say("write a reply saying I'll be late")
        self.assertEqual((v["state"], self.inserts), ("failed", []))

    def test_field_contents_and_passwords_never_reach_the_writer(self):
        self.snap = snap([item(1, "Name", role="AXTextField", pressable=False, ref=self.field),
                          item(2, "draft: my secret plan", role="AXTextArea", pressable=False, from_value=True),
                          item(3, "hunter2", role="AXTextField", pressable=False, secure=True),
                          item(4, "See you at 5?", role="AXStaticText", pressable=False, from_value=True),
                          item(5, "Send")])
        self.facts["window"] = self.snap.window_token
        self.say("write a reply saying I'll be late")
        seen = json.dumps(self.wrote[0][1])
        self.assertNotIn("secret plan", seen)
        self.assertNotIn("hunter2", seen)
        self.assertIn("See you at 5?", seen)  # the message being answered is shown text: the writer may use it
        self.assertEqual(self.wrote[0][1]["on_screen"], ["See you at 5?", "Send"])

    def test_draft_and_request_stay_out_of_the_log(self):
        logged = []
        with patch.object(diagnostics, "record", lambda *a, **k: logged.append((a, k))):
            self.say("write a reply saying I'll be late")
        self.assertNotIn("ten minutes", repr(logged))
        self.assertNotIn("I'll be late", repr([k.get("args") for _a, k in logged]))

    def test_named_field_and_password_field(self):
        v, _ = self.say("draft a thank-you note to Sam in the Name field")
        self.assertEqual((v["state"], self.wrote[0][1]["field"]), ("completed", "Name"))
        self.wrote.clear()
        self.facts["secure"] = True
        v, _ = self.say("write a reply saying I'll be late")
        self.assertEqual((v["steps"][0]["detail"], self.wrote, self.inserts[1:]), ("password_field", [], []))


class DraftBoxTests(unittest.TestCase):
    def test_the_whole_draft_is_in_the_box(self):
        import assistant_ui
        text = "line " * 399 + "END"
        box = assistant_ui.draft_box(text, assistant_ui.NSMakeRect(0, 0, 290, assistant_ui.DRAFT_H))
        self.assertEqual(box.documentView().string(), text)
        self.assertFalse(box.documentView().isEditable())


class CursorTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(diagnostics, "record", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)

    def run_press(self, point):
        order = []
        fake = FakeScreen(self, [item(1, "Add one", frame=(100, 200, 80, 30))])
        real = fake.press
        patch.object(screen, "press", lambda ref, d: (order.append("press"), real(ref, d))[1]).start()
        self.addCleanup(patch.stopall)
        step = {"clause": "click Add one", "action": "screen.press", "args": {"label": "Add one"}}
        with patch.object(planner, "plan", lambda *a, **k: ("steps", [step])):
            eng = Engine(lambda _: {}, policy=lambda: {**actions.DEFAULT_POLICY, "click": "auto"})
            eng.consequence = lambda *a: 0.0
            eng.point = lambda target, wait: (order.append(("point", target["frame"], wait)), point(target, wait))
            v = eng.wait(eng.submit("click Add one", "cli")["id"], 10)
        return v, order

    def test_cursor_reaches_the_target_before_the_press(self):
        v, order = self.run_press(lambda t, w: None)
        self.assertEqual(v["state"], "completed")
        self.assertEqual(order, [("point", [100, 200, 80, 30], engine.POINT_WAIT), "press"])

    def test_a_cursor_that_fails_never_stops_the_action(self):
        def broken(t, w):
            raise RuntimeError("no window server")
        v, order = self.run_press(broken)
        self.assertEqual((v["state"], order[-1]), ("completed", "press"))

    def test_not_for_actions_without_a_place_on_screen(self):
        calls = []
        step = {"clause": "ok", "action": "ok", "args": {}}
        acts = {"ok": {"effect": "open", "resolve": lambda a: ("target", {"app": "x"}), "run": lambda t, d: None,
                       "verify": lambda t, d: ("done", {}), "proves": "", "timeout": 1}}
        with patch.object(planner, "plan", lambda *a, **k: ("steps", [step])):
            eng = Engine(lambda _: {}, policy=lambda: {"open": "auto"}, actions=acts)
            eng.point = lambda t, w: calls.append(t)
            eng.wait(eng.submit("ok", "cli")["id"], 5)
        self.assertEqual(calls, [])

    def test_arrow_tip_lands_on_the_control_centre(self):
        import assistant_ui
        top = assistant_ui.AppKit.NSScreen.screens()[0].frame().size.height
        x, y = assistant_ui.cursor_origin([100, 200, 80, 30])
        tip = (x + 6, y + assistant_ui.CURSOR_SIZE - 4)  # the arrow's tip, top-left of the window
        self.assertEqual(tip, (140, top - 215))
        self.assertTrue(math.isfinite(x) and math.isfinite(y))


if __name__ == "__main__":
    unittest.main()
