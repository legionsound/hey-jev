"""Screen control: the walk, the merge, the planner's words, and press/list through the real engine with a fake screen."""
import time
import unittest
from unittest.mock import patch

import actions
import ax_walk
import diagnostics
import planner
import screen
from engine import Engine


class Node:
    def __init__(self, role, label="", frame=(0, 0, 100, 30), kids=(), acts=("AXPress",)):
        self.role, self.label, self.frame, self.kids, self.acts = role, label, frame, list(kids), acts


def walk(root, area=(0, 0, 1000, 800)):
    x, y, w, h = area
    return ax_walk.walk_actionable(root, lambda n: n.kids, lambda n: ax_walk.AxAttrs(n.role, n.label, n.frame),
                                   lambda n: n.acts, w, h, x0=x, y0=y)


class WalkTests(unittest.TestCase):
    def test_nested_nameless_containers_are_all_walked(self):
        # WebKit: window > group > group > scroll area > web area, all one frame. Keying them dropped the page.
        big = (1622, 30, 1578, 1320)
        page = Node("AXWebArea", frame=big, kids=[Node("AXButton", "Go back", (1732, 38, 24, 28))])
        root = Node("AXWindow", frame=big, kids=[Node("AXGroup", frame=big, kids=[
            Node("AXGroup", frame=big, kids=[Node("AXScrollArea", frame=big, kids=[page])])])])
        found, _, _ = walk(root, big)
        self.assertEqual([n.label for n in found], ["Go back"])

    def test_window_on_a_second_display(self):
        root = Node("AXWindow", frame=(2000, 100, 400, 300), kids=[Node("AXButton", "OK", (2100, 200, 80, 30))])
        found, _, _ = walk(root, (2000, 100, 400, 300))
        self.assertEqual([n.label for n in found], ["OK"])
        found, _, _ = walk(root, (0, 0, 1000, 800))  # the old origin-zero area missed it
        self.assertEqual(found, [])


def item(n, label, source="ax", role="AXButton", frame=(10, 10, 80, 30), pressable=True, ref=None, **kw):
    return screen.Item(n, source, role, label, frame, pressable, ref=ref if ref is not None else object(), **kw)


WINDOWS = {}


def snap(items, pid=7, window="Pad", app="Pad", started="Thu Sep 24 18:00:00 2026"):
    return screen.Snapshot(pid, app, "com.pad", window, (0, 0, 400, 300), items,
                           window_ref=WINDOWS.setdefault((pid, window), object()), started=started)


class MergeTests(unittest.TestCase):
    def test_ocr_on_a_control_marks_it_and_other_text_stays_text(self):
        control = ax_walk.AxNode("AXButton", "Add one", 20, 100, 180, 34, True, None)
        texts = [("Add one", 1.0, (60, 108, 60, 14)), ("Count: 0", 1.0, (20, 60, 52, 12)),
                 ("outside", 1.0, (900, 900, 10, 10))]
        items = screen.merge([control], texts, (0, 0, 400, 300))
        self.assertEqual([(i.n, i.source, i.label) for i in items], [(1, "ocr", "Count: 0"), (2, "ax+ocr", "Add one")])


class PlannerTests(unittest.TestCase):
    def test_press_words(self):
        cases = {"click Share": {"label": "Share"}, "press the New Note button": {"label": "New Note"},
                 "click 12": {"number": 12}, "click number twelve": {"number": 12}, "tap on Send please": {"label": "Send"},
                 "choose Two Pages": {"label": "Two Pages"}, "click the dark mode toggle": {"label": "dark mode"}}
        for said, want in cases.items():
            self.assertEqual(planner.press_args(said), want, said)
        self.assertIsNone(planner.press_args("open Safari"))

    def answers(self, target, **branch):
        ans = {"category": ("mac_command", 0.95), "compound": (False, 0.9), "target": (target, 0.9),
               "app_action": ("none", 0.9), "volume_action": ("up", 0.8), "volume_scope": ("system", 0.9),
               "volume_level": ("loud", 0.8), "display_action": ("toggle", 0.9), "media_action": ("play", 0.9),
               "timer_action": ("none", 0.9), "system_action": ("none", 0.9), "screen_action": ("press", 0.9)}
        ans.update(branch)
        return ans

    def test_click_and_ui_nouns_never_become_other_actions(self):
        # Real Jev heard "click on the Loud mode checkbox" as volume up (0.82): the click must stay a click.
        for said, target in [("click on the Loud mode checkbox", "volume"), ("tap the Play button", "media"),
                             ("click the dark mode toggle", "display"), ("click Mute", "volume")]:
            kind, steps = planner.plan(said, lambda _: self.answers(target))
            self.assertEqual((kind, steps[0]["action"]), ("steps", "screen.press"), said)
        kind, steps = planner.plan("press play", lambda _: self.answers("media"))
        self.assertEqual(steps[0]["action"], "media.play")  # no click word, no UI noun: Jev's target stands
        kind, steps = planner.plan("switch to dark mode", lambda _: self.answers("display", display_action=("dark_on", 0.9)))
        self.assertEqual(steps[0]["action"], "display.dark_on")

    def test_list(self):
        kind, steps = planner.plan("what can I click here", lambda _: self.answers("screen", screen_action=("list", 0.9)))
        self.assertEqual(steps[0]["action"], "screen.list")


class FakeScreen:
    """Patches screen's native reads and the press so the real actions and engine run without touching the Mac."""

    def __init__(self, test, items, shown=None):
        self.current, self.shown, self.presses = snap(items), shown, []
        self.effect = "own"  # own: the pressed control's value flips; other: only unrelated text changes; none
        self.sig, self.state = {"text": "a", "menu_open": False}, {"exists": True, "AXValue": "0"}
        for name, fn in {"observe": self.observe, "last": lambda: self.shown,
                         "signature": lambda pid, deadline: dict(self.sig),
                         "element_state": lambda ref, deadline: dict(self.state), "press": self.press}.items():
            p = patch.object(screen, name, fn)
            p.start()
            test.addCleanup(p.stop)

    def observe(self, pid=None, ocr=True, deadline=None):
        return self.current

    def press(self, ref, deadline):
        self.presses.append(ref)
        if self.effect == "own":
            self.state = {**self.state, "AXValue": "1"}
        elif self.effect == "other":
            self.sig = {**self.sig, "text": "b"}  # a clock ticking, another window updating
        return 0


class ScreenActionTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(diagnostics, "record", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)
        self.chosen = (None, 0.0)
        p = patch.object(actions, "CHOOSE", lambda spoken, labels: self.chosen)
        p.start()
        self.addCleanup(p.stop)

    def run_text(self, text, action, args, policy=None, ask=None):
        def classify(_):
            return {}
        with patch.object(planner, "plan", lambda *a, **k: ("steps", [{"clause": text, "action": action, "args": args}])):
            eng = Engine(classify, policy=lambda: policy or {**actions.DEFAULT_POLICY, "click": "auto"}, ask=ask)
            r = eng.submit(text, "cli")
            return eng.wait(r["id"], 10)

    def test_press_by_name_changes_and_completes(self):
        fake = FakeScreen(self, [item(1, "Add one"), item(2, "Count: 0", "ocr", "text", pressable=False)])
        v = self.run_text("click Add one", "screen.press", {"label": "add ONE"})
        self.assertEqual((v["state"], v["steps"][0]["facts"]["changed"]), ("completed", ["AXValue"]))
        self.assertEqual(len(fake.presses), 1)

    def test_press_with_no_change_is_unverified_not_done(self):
        fake = FakeScreen(self, [item(1, "Add one")])
        fake.effect = "none"
        v = self.run_text("click Add one", "screen.press", {"label": "Add one"})
        self.assertEqual(v["state"], "unverified")

    def test_an_unrelated_change_does_not_complete_a_press(self):
        fake = FakeScreen(self, [item(1, "Add one")])
        fake.effect = "other"
        v = self.run_text("click Add one", "screen.press", {"label": "Add one"})
        self.assertEqual((v["state"], v["steps"][0]["facts"]["delivered"], v["steps"][0]["facts"]["observed"]),
                         ("unverified", True, ["text"]))
        self.assertEqual(len(fake.presses), 1)

    def test_a_menu_opening_from_a_menu_control_completes(self):
        fake = FakeScreen(self, [item(1, "View", role="AXMenuBarItem")])
        fake.effect = "none"
        orig = fake.press
        fake.press = lambda ref, deadline: (orig(ref, deadline), fake.sig.update(menu_open=True))[0]
        patch.object(screen, "press", fake.press).start()
        v = self.run_text("click View", "screen.press", {"label": "View"})
        self.assertEqual((v["state"], v["steps"][0]["facts"]["changed"]), ("completed", ["menu_open"]))

    def test_number_uses_the_list_the_user_saw_and_checks_it_is_still_there(self):
        title, save = item(1, "Title", "ocr", "text", pressable=False), item(2, "Save")
        shown = snap([title, save])
        fake = FakeScreen(self, [title, save], shown)
        self.assertEqual(self.run_text("click 2", "screen.press", {"number": 2})["state"], "completed")
        v = self.run_text("click 1", "screen.press", {"number": 1})  # OCR text is not a control
        self.assertEqual((v["state"], v["steps"][0]["detail"]), ("failed", "not_a_control"))
        stale = {
            "moved": snap([title, item(2, "Save", frame=(50, 50, 80, 30), ref=save.ref)]),
            "replaced at the same spot": snap([title, item(2, "Save")]),  # a new element, same role/label/frame
            "other window, same title": snap([title, save], window="Pad") if False else None,
            "restarted app, same pid": snap([title, save], started="Thu Sep 24 19:00:00 2026"),
            "disabled": snap([title, item(2, "Save", ref=save.ref, enabled=False)]),
        }
        other = screen.Snapshot(7, "Pad", "com.pad", "Pad", (0, 0, 400, 300), [title, save], window_ref=object(),
                                started=shown.started)
        stale["other window, same title"] = other
        for why, current in stale.items():
            fake.current = current
            v = self.run_text("click 2", "screen.press", {"number": 2})
            self.assertEqual((v["state"], v["steps"][0]["detail"]), ("failed", "screen_changed"), why)
        self.assertEqual(len(fake.presses), 1)

    def test_a_control_swapped_between_confirm_and_press_is_never_pressed(self):
        save = item(1, "Save")
        fake = FakeScreen(self, [save])
        asked = []

        def ask(pending):
            if pending:
                asked.append(pending)
                fake.current = snap([item(1, "Save")])  # replaced while the pop-down is open
                eng.decide(pending["token"], True)
        eng = None

        def classify(_):
            return {}
        with patch.object(planner, "plan", lambda *a, **k: ("steps", [{"clause": "click Save", "action": "screen.press",
                                                                        "args": {"label": "Save"}}])):
            eng = Engine(classify, policy=lambda: dict(actions.DEFAULT_POLICY), ask=ask)
            v = eng.wait(eng.submit("click Save", "cli")["id"], 10)
        self.assertEqual((len(asked), v["state"], v["steps"][0]["detail"]), (1, "failed", "target_changed"))
        self.assertEqual(fake.presses, [])

    def test_screen_choices_never_reach_the_duplicate_app_chooser(self):
        fake = FakeScreen(self, [item(1, "Buy", frame=(10, 10, 60, 30)), item(2, "Buy", frame=(300, 250, 60, 30))])
        called = []
        with patch.object(planner, "plan", lambda *a, **k: ("steps", [{"clause": "click Buy", "action": "screen.press",
                                                                        "args": {"label": "Buy"}}])):
            eng = Engine(lambda _: {}, policy=lambda: {**actions.DEFAULT_POLICY, "click": "auto"},
                         tiebreak=lambda clause, choices: called.append(choices) or (0, 0.99), threshold=lambda: 0.5)
            v = eng.wait(eng.submit("click Buy", "cli")["id"], 10)
        self.assertEqual((v["state"], called, fake.presses), ("needs_clarification", [], []))

    def test_identical_labels_ask_which(self):
        fake = FakeScreen(self, [item(1, "Buy", frame=(10, 10, 60, 30)), item(2, "Buy", frame=(300, 250, 60, 30))])
        v = self.run_text("click Buy", "screen.press", {"label": "Buy"})
        self.assertEqual(v["state"], "needs_clarification")
        self.assertEqual([c["name"] for c in v["steps"][0]["facts"]["choices"]], ["Buy (top-left)", "Buy (bottom-right)"])
        self.assertEqual(fake.presses, [])

    def test_jev_picks_only_above_the_gate_and_only_well_formed_answers(self):
        fake = FakeScreen(self, [item(1, "New Note"), item(2, "Delete Note")])
        self.chosen = (0, 0.5)
        self.assertEqual(self.run_text("click compose", "screen.press", {"label": "compose"})["state"], "failed")
        for bad in [(0, float("inf")), (-1, 0.99), (True, 0.99), (5, 0.99), (0, True), (0, 1.5), ("0", 0.9), (0,), None]:
            self.chosen = bad
            v = self.run_text("click compose", "screen.press", {"label": "compose"})
            self.assertEqual(v["state"], "needs_clarification", bad)
        self.assertEqual(fake.presses, [])
        self.chosen = (0, 0.9)
        v = self.run_text("click compose", "screen.press", {"label": "compose"})
        self.assertEqual((v["state"], v["steps"][0]["target"]["label"]), ("completed", "New Note"))

    def test_chooser_never_sees_field_or_document_values(self):
        FakeScreen(self, [item(1, "New Note"), item(2, "Dear Sam, the merger", role="AXCell", from_value=True)])
        seen = []
        with patch.object(actions, "CHOOSE", lambda spoken, labels: seen.append(labels) or (None, 0.0)):
            self.run_text("click compose", "screen.press", {"label": "compose"})
        self.assertEqual(len(seen), 1)
        self.assertNotIn("Dear Sam", repr(seen))  # neither as a choice nor as a neighbour in a card
        self.assertEqual(len(seen[0]), 1)

    def test_risky_labels_always_ask_even_when_clicks_are_automatic(self):
        fake = FakeScreen(self, [item(1, "Delete everything")])
        v = self.run_text("click Delete everything", "screen.press", {"label": "Delete everything"})
        self.assertEqual((v["state"], v["steps"][0]["detail"]), ("declined", "no_confirmation_ui"))
        self.assertEqual(fake.presses, [])

    def test_clicks_ask_first_by_default(self):
        fake = FakeScreen(self, [item(1, "Add one")])
        v = self.run_text("click Add one", "screen.press", {"label": "Add one"}, policy=dict(actions.DEFAULT_POLICY))
        self.assertEqual(v["state"], "declined")
        self.assertEqual(fake.presses, [])

    def test_list_remembers_and_reports_items(self):
        FakeScreen(self, [item(1, "Save"), item(2, "hello", "ocr", "text", pressable=False)])
        v = self.run_text("what can I click", "screen.list", {})
        facts = v["steps"][0]["facts"]
        self.assertEqual((v["state"], facts["count"], [i["label"] for i in facts["items"]]), ("completed", 2, ["Save", "hello"]))
        self.assertEqual(screen.LAST.app, "Pad")

    def test_screen_text_stays_out_of_the_log_and_speech(self):
        import siri
        FakeScreen(self, [item(1, "Private document title")])
        logged = []
        with patch.object(diagnostics, "record", lambda *a, **k: logged.append(k)):
            listed = self.run_text("what can I click", "screen.list", {})
            self.chosen = (0, 0.9)  # the user said "compose"; Jev picked the control, whose text the user never said
            pressed = self.run_text("click compose", "screen.press", {"label": "compose"})
        self.assertEqual(pressed["state"], "completed")
        self.assertNotIn("Private document", repr(logged))
        self.assertTrue(any(k.get("facts", {}).get("items") == 1 for k in logged))
        for v in (listed, pressed):
            self.assertNotIn("Private", siri.line_for(v))


class DeadlineTests(unittest.TestCase):
    def test_bounded_returns_at_the_deadline_even_when_the_call_hangs(self):
        import time
        t = time.monotonic()
        with self.assertRaises(screen.TimedOut):
            screen.bounded(lambda: time.sleep(5), t + 0.3)
        self.assertLess(time.monotonic() - t, 1.0)
        self.assertEqual(screen.bounded(lambda: 4, time.monotonic() + 1), 4)
        with self.assertRaises(ZeroDivisionError):  # the call's own error comes back, not a timeout
            screen.bounded(lambda: 1 / 0, time.monotonic() + 1)

    def test_slow_ocr_keeps_the_controls_and_says_so(self):
        import time
        ctl = ax_walk.AxNode("AXButton", "Save", 10, 10, 80, 30, True, object())
        with patch.object(screen, "trusted", lambda: True), \
                patch.object(screen, "_app_info", lambda pid: ("Pad", "com.pad")), \
                patch.object(screen, "process_start", lambda pid, d: "start"), \
                patch.object(screen, "_read_ax", lambda pid, d: (object(), (0, 0, 400, 300), "Pad", [ctl],
                                                                 {id(ctl): (True, False, False)}, False)), \
                patch.object(screen, "read_text", lambda *a: time.sleep(5)):
            t = time.monotonic()
            got = screen.observe(pid=7, deadline=t + 0.5)
        self.assertLess(time.monotonic() - t, 1.2)
        self.assertEqual((got.ocr, [i.label for i in got.items]), ("timed_out", ["Save"]))

    def test_a_press_the_app_never_answers_is_unknown_not_failed(self):
        with patch.object(diagnostics, "record", lambda *a, **k: None):
            fake = FakeScreen(self, [item(1, "Add one")])

            def hang(ref, deadline):
                fake.presses.append(ref)
                raise screen.TimedOut("AXPress")
            patch.object(screen, "press", hang).start()
            self.addCleanup(patch.stopall)
            with patch.object(planner, "plan", lambda *a, **k: ("steps", [{"clause": "c", "action": "screen.press",
                                                                            "args": {"label": "Add one"}}])):
                eng = Engine(lambda _: {}, policy=lambda: {**actions.DEFAULT_POLICY, "click": "auto"})
                v = eng.wait(eng.submit("click Add one", "cli")["id"], 10)
        self.assertEqual((v["state"], len(fake.presses)), ("unknown", 1))


class BoundaryTests(unittest.TestCase):
    real_press = staticmethod(screen.press)

    def test_tokens_are_never_reused_and_evicted_ones_go_stale(self):
        with patch.object(screen, "_tokens", []), patch.object(screen, "_next_token", [0]), \
                patch.object(screen, "TOKEN_CAP", 50):
            objs = [object() for _ in range(52)]
            toks = [screen.token(o) for o in objs]
            self.assertEqual(len(set(toks)), 52)
            self.assertIsNone(screen.element_for(toks[0]))  # evicted: names nothing, never a newer element
            self.assertIs(screen.element_for(toks[-1]), objs[-1])
            self.assertEqual(screen.token(objs[-1]), toks[-1])

    def test_a_late_press_holds_the_boundary_and_is_never_reported_as_clean(self):
        import threading
        import time
        release = threading.Event()
        calls = []

        class FakeAS:
            def AXUIElementPerformAction(self, ref, action):
                calls.append(ref)
                release.wait(5)
                return 0
        with patch.object(screen, "_AS", lambda: FakeAS()), patch.object(screen, "EFFECT_SETTLE", 0.3), \
                patch.object(screen, "_abandoned", []):
            t0 = time.monotonic()
            with self.assertRaises(screen.TimedOut):
                screen.press("button", time.monotonic() + 0.2)
            self.assertGreaterEqual(time.monotonic() - t0, 0.45)  # waited out the settle window too
            with self.assertRaises(screen.Wedged):  # the next command can't start a native call meanwhile
                screen.press("other", time.monotonic() + 1)
            with self.assertRaises(screen.Wedged):
                screen.element_state("other", time.monotonic() + 1)
            release.set()
            time.sleep(0.1)
            self.assertEqual(screen.press("other", time.monotonic() + 1), 0)  # settled: work resumes
            self.assertEqual(calls, ["button", "other"])

    def test_engine_timeout_then_queued_command_then_late_press(self):
        import threading
        import time
        release, calls = threading.Event(), []

        class FakeAS:
            def AXUIElementPerformAction(self, ref, action):
                calls.append(ref)
                release.wait(5)
                return 0
        real_press = screen.press
        with patch.object(diagnostics, "record", lambda *a, **k: None):
            fake = FakeScreen(self, [item(1, "Add one")])
            for p in (patch.object(screen, "press", real_press), patch.object(screen, "_AS", lambda: FakeAS()),
                      patch.object(screen, "EFFECT_SETTLE", 0.2), patch.object(screen, "_abandoned", []),
                      patch("engine.PENDING_WAIT", 0.5)):
                p.start()
                self.addCleanup(p.stop)
            entry = dict(actions.ACTIONS["screen.press"], timeout=0.3)
            with patch.dict(actions.ACTIONS, {"screen.press": entry}), \
                    patch.object(planner, "plan", lambda *a, **k: ("steps", [{"clause": "c", "action": "screen.press",
                                                                              "args": {"label": "Add one"}}])):
                eng = Engine(lambda _: {}, policy=lambda: {**actions.DEFAULT_POLICY, "click": "auto"},
                             actions=actions.ACTIONS)
                first = eng.wait(eng.submit("click Add one", "cli")["id"], 10)
                second = eng.wait(eng.submit("click Add one again", "cli")["id"], 10)
                release.set()
                time.sleep(0.1)
        self.assertEqual(first["state"], "unknown")  # a press that may have landed is never failed or done
        self.assertEqual(second["state"], "failed")  # the queued command could not dispatch
        self.assertEqual(second["steps"][0]["detail"], "an earlier action hasn't finished")
        self.assertEqual(len(calls), 1)  # the late completion was the first press, and nothing else was pressed

    def test_no_action_family_overtakes_a_pending_press(self):
        import threading
        import time
        release, order = threading.Event(), []

        class FakeAS:
            def AXUIElementPerformAction(self, ref, action):
                release.wait(5)
                order.append("late press")
                return 0

        def run_volume(t, deadline):
            order.append("volume")
        vol = actions.entry("volume", actions.plain, run_volume, None, "nothing", 2)
        with patch.object(diagnostics, "record", lambda *a, **k: None):
            FakeScreen(self, [item(1, "Add one")])
            real_press = BoundaryTests.real_press
            for p in (patch.object(screen, "press", real_press), patch.object(screen, "_AS", lambda: FakeAS()),
                      patch.object(screen, "EFFECT_SETTLE", 0.2), patch.object(screen, "_abandoned", []),
                      patch("engine.PENDING_WAIT", 0.5)):
                p.start()
                self.addCleanup(p.stop)
            plans = {"click Add one": ("screen.press", {"label": "Add one"}), "turn it up": ("volume.up", {})}
            table = {**actions.ACTIONS, "screen.press": dict(actions.ACTIONS["screen.press"], timeout=0.3),
                     "volume.up": vol}
            with patch.object(planner, "plan", lambda text, *a, **k: ("steps", [{"clause": text, "action": plans[text][0],
                                                                                 "args": plans[text][1]}])):
                eng = Engine(lambda _: {}, policy=lambda: {**actions.DEFAULT_POLICY, "click": "auto"}, actions=table)
                first = eng.wait(eng.submit("click Add one", "cli")["id"], 10)
                second = eng.wait(eng.submit("turn it up", "cli")["id"], 10)  # a different family entirely
                release.set()
                time.sleep(0.2)
                third = eng.wait(eng.submit("turn it up", "cli", rid="again")["id"], 10)
        self.assertEqual(first["state"], "unknown")
        self.assertEqual((second["state"], second["steps"][0]["detail"]), ("failed", "an earlier action hasn't finished"))
        self.assertEqual(third["state"], "unverified")  # settled: work resumes
        self.assertEqual(order, ["late press", "volume"])  # the volume never ran before the late press landed

    def test_cancel_while_waiting_on_a_pending_effect_is_cancelled_not_failed(self):
        import time
        runs = []
        app = actions.entry("open", actions.plain, lambda t, d: runs.append(t), None, "nothing", 2)
        with patch.object(diagnostics, "record", lambda *a, **k: None), \
                patch.object(planner, "plan", lambda *a, **k: ("steps", [{"clause": "open Notes", "action": "app.open",
                                                                          "args": {}}])):
            eng = Engine(lambda _: {}, actions={"app.open": app}, pending=lambda: True)
            rid = eng.submit("open Notes", "cli")["id"]
            time.sleep(0.3)  # the step is waiting on the pending effect
            eng.cancel(rid)
            v = eng.wait(rid, 5)
        self.assertEqual((v["state"], v["steps"][0]["state"], runs), ("cancelled", "skipped", []))

    def test_a_press_that_lands_during_settle_is_still_late(self):
        import time

        class SlowAS:
            def AXUIElementPerformAction(self, ref, action):
                time.sleep(0.3)
                return 0
        with patch.object(screen, "_AS", lambda: SlowAS()), patch.object(screen, "_abandoned", []):
            with self.assertRaises(screen.TimedOut):
                screen.press("button", time.monotonic() + 0.1)
            self.assertEqual(screen._outstanding(), [])

    def test_failed_reads_prove_nothing(self):
        import time
        with patch.object(screen, "_read", lambda el, name: ("unknown", None)):
            self.assertFalse(screen.enabled("x"))  # unknown is not enabled: dispatch stops
            state = screen.element_state("x", time.monotonic() + 1)
        self.assertTrue(all(v == screen.UNKNOWN for v in state.values()))
        with patch.object(screen, "_read", lambda el, name: ("absent", None)):
            self.assertTrue(screen.enabled("x"))
        # checkbox read 0 before; after, every read fails with -25204: not done, not "gone"
        t = {"pid": 7, "role": "AXCheckBox"}
        before = ({"text": "a"}, {"exists": "yes", "AXValue": "0", "AXSelected": "None", "AXExpanded": "None"})
        actions._pressed[id(t)] = (before, time.monotonic() - 5, "ref")
        with patch.object(screen, "signature", lambda pid, d: {"text": "a"}), \
                patch.object(screen, "element_state", lambda ref, d: {k: screen.UNKNOWN for k in before[1]}):
            verdict, facts = actions.verify_screen_press(t, time.monotonic() + 1)
        self.assertEqual((verdict, facts["unread"]), ("unverified", ["AXExpanded", "AXSelected", "AXValue", "exists"]))


class PermissionTests(unittest.TestCase):
    def test_missing_accessibility_prompts_once_per_run_and_says_where(self):
        import siri
        prompts = []

        class NoAX:
            kAXTrustedCheckOptionPrompt = "prompt"

            def AXIsProcessTrusted(self):
                return False

            def AXIsProcessTrustedWithOptions(self, opts):
                prompts.append(opts)
        with patch.object(screen, "_AS", lambda: NoAX()), patch.object(screen, "_asked", set()):
            self.assertFalse(screen.trusted())
            self.assertFalse(screen.trusted())
        self.assertEqual(prompts, [{"prompt": True}])
        v = {"state": "failed", "steps": [{"action": "screen.list", "state": "failed", "facts": {}, "clause": "c",
                                           "detail": "can't read the screen: accessibility_permission"}]}
        self.assertIn("Accessibility", siri.line_for(v))


class TypeTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(diagnostics, "record", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)
        self.field = object()
        self.value = {"v": "Hi "}
        self.facts = {"role": "AXTextField", "secure": False, "enabled": True, "frame": [10, 10, 100, 24],
                      "insertable": True}
        self.inserts = []
        self.snap = snap([item(1, "Name", role="AXTextField", pressable=False, ref=self.field)])
        self.facts["window"] = self.snap.window_token
        patches = {"observe": lambda pid=None, ocr=True, deadline=None: self.snap,
                   "field_facts": lambda ref, d: dict(self.facts),
                   "field_value": lambda ref, d: self.value["v"],
                   "focused_field": lambda pid, d: self.field,
                   "process_start": lambda pid, d: self.snap.started,
                   "insert_text": self.insert, "focus": lambda ref, d: 0,
                   "selected_range": lambda ref, d: self.range}
        for name, fn in patches.items():
            p = patch.object(screen, name, fn)
            p.start()
            self.addCleanup(p.stop)
        self.effect = "insert"
        self.range = (len(self.value["v"].encode("utf-16-le")) // 2, 0)  # cursor at the end

    def insert(self, ref, text, deadline):
        """Like the real field: replace the UTF-16 selection with the text."""
        self.inserts.append(text)
        if self.effect == "insert":
            self.value["v"] = screen.expected_after(self.value["v"], self.range, text)
        elif self.effect == "delete_other":
            self.value["v"] = screen.expected_after(self.value["v"], self.range, text)[1:]  # and something else changed
        return 0

    def run_type(self, args, policy=None):
        with patch.object(planner, "plan", lambda *a, **k: ("steps", [{"clause": "t", "action": "screen.type",
                                                                        "args": args}])):
            eng = Engine(lambda _: {}, policy=lambda: policy or {**actions.DEFAULT_POLICY, "type": "auto"})
            return eng.wait(eng.submit("type something", "cli")["id"], 10)

    def test_words(self):
        cases = {"type hello world": {"text": "hello world"},
                 "type hello world into the search field": {"text": "hello world", "field": "search"},
                 "enter my address in the Name box": {"text": "my address", "field": "Name"},
                 'type "see you in Paris." into Notes': {"text": "see you in Paris.", "field": "Notes"},
                 "type see you in Paris.": {"text": "see you in Paris"}}
        for said, want in cases.items():
            self.assertEqual(planner.type_args(said), want, said)
        for bad in ["type in the search field", 'type "hello" into', 'type "hello" nonsense', 'type "hello" in Firefox',
                    "type hello into", "type hello into the"]:
            self.assertIsNone(planner.type_args(bad), bad)  # an unfinished or unknown qualifier: clarify, never drop it

    def test_types_into_the_named_field_and_checks_it(self):
        v = self.run_type({"text": "there", "field": "name"})
        self.assertEqual((v["state"], v["steps"][0]["facts"], self.value["v"]), ("completed", {"typed": 5}, "Hi there"))

    def test_selected_text_is_replaced_exactly(self):
        self.value["v"], self.range = "abcdef", (1, 3)
        v = self.run_type({"text": "X"})
        self.assertEqual((v["state"], self.value["v"]), ("completed", "aXef"))

    def test_an_unrelated_change_is_not_done(self):
        self.value["v"], self.range, self.effect = "abcdef", (6, 0), "delete_other"
        self.assertEqual(self.run_type({"text": "X"})["state"], "unverified")

    def test_utf16_offsets_with_characters_outside_the_bmp(self):
        self.value["v"], self.range = "a😀bcdef", (3, 2)  # AX counts the emoji as 2
        v = self.run_type({"text": "X"})
        self.assertEqual((v["state"], self.value["v"]), ("completed", "a😀Xdef"))
        self.assertIsNone(screen.expected_after("a😀b", (2, 0), "X"))  # splits the emoji: no exact expectation

    def test_without_a_readable_selection_it_is_never_done(self):
        self.range = None
        self.value["v"] = "Hi "
        orig = self.insert
        self.insert = lambda ref, text, d: (self.inserts.append(text), self.value.update(v="Hi " + text))[0] or 0
        patch.object(screen, "insert_text", self.insert).start()
        v = self.run_type({"text": "there"})
        self.assertEqual((v["state"], self.value["v"]), ("unverified", "Hi there"))

    def test_a_focus_that_times_out_is_unknown_and_nothing_is_typed(self):
        def late(ref, d):
            raise screen.TimedOut("focus")
        with patch.object(screen, "focus", late):
            v = self.run_type({"text": "x"})
        self.assertEqual((v["state"], self.inserts), ("unknown", []))

    def test_an_ambiguous_focus_error_is_unknown_and_nothing_is_typed(self):
        with patch.object(screen, "focus", lambda ref, d: -25204):  # cannot complete: it may still have focused
            v = self.run_type({"text": "x"})
        self.assertEqual((v["state"], self.inserts), ("unknown", []))
        with patch.object(screen, "focus", lambda ref, d: -25202):  # the element is gone: a definite refusal
            v = self.run_type({"text": "x"})
        self.assertEqual((v["state"], self.inserts), ("failed", []))

    def test_focused_field_when_none_is_named(self):
        self.assertEqual(self.run_type({"text": "x"})["state"], "completed")

    def test_never_types_into_password_fields(self):
        self.facts["secure"] = True
        v = self.run_type({"text": "hunter2"})
        self.assertEqual((v["state"], v["steps"][0]["detail"], self.inserts), ("failed", "password_field", []))

    def test_an_unreadable_field_kind_counts_as_secure(self):
        with patch.object(screen, "_read", lambda el, name: ("unknown", None)):
            self.assertTrue(screen.is_secure("x"))

    def test_not_a_text_field(self):
        self.facts["insertable"] = False
        v = self.run_type({"text": "x"})
        self.assertEqual((v["steps"][0]["detail"], self.inserts), ("not_a_text_field", []))

    def test_field_that_changed_before_typing_is_left_alone(self):
        orig = screen.field_facts
        calls = []

        def moving(ref, d):
            calls.append(1)
            f = dict(self.facts)
            if len(calls) > 1:
                f["frame"] = [50, 50, 100, 24]  # moved between resolve and run
            return f
        with patch.object(screen, "field_facts", moving):
            v = self.run_type({"text": "x"})
        self.assertEqual((v["state"], self.inserts), ("failed", []))

    def test_text_that_does_not_show_up_is_unverified(self):
        self.effect = "nothing"
        v = self.run_type({"text": "x"})
        self.assertEqual((v["state"], v["steps"][0]["facts"]["delivered"]), ("unverified", True))

    def test_typing_asks_first_by_default(self):
        v = self.run_type({"text": "x"}, policy=dict(actions.DEFAULT_POLICY))
        self.assertEqual((v["state"], self.inserts), ("declined", []))

    def test_typed_text_stays_out_of_the_log(self):
        logged = []
        with patch.object(diagnostics, "record", lambda *a, **k: logged.append(k)):
            self.run_type({"text": "my secret plan", "field": "Name"})
        self.assertNotIn("secret plan", repr(logged))


class SubmitTests(TypeTests):
    def setUp(self):
        super().setUp()
        self.after = {}
        self.confirms = []
        for name, fn in {"can_confirm": lambda ref, d: self.facts.get("confirm", True), "confirm": self.do_confirm,
                         "element_state": lambda ref, d: {"exists": self.after.get("exists", "yes")},
                         "focused_field": lambda pid, d: self.after.get("focus", self.field)}.items():
            p = patch.object(screen, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def do_confirm(self, ref, d):
        self.confirms.append(ref)
        self.after.update(self.effect_after)
        return 0

    def submit(self, policy=None):
        with patch.object(planner, "plan", lambda *a, **k: ("steps", [{"clause": "press enter",
                                                                        "action": "screen.submit", "args": {}}])):
            eng = Engine(lambda _: {}, policy=lambda: policy or {**actions.DEFAULT_POLICY, "submit": "auto"})
            return eng.wait(eng.submit("press enter", "cli")["id"], 10)

    def test_submit_words_are_exact(self):
        for said in ["press enter", "hit return", "press the enter key", "submit", "submit it please"]:
            self.assertTrue(planner.SUBMIT_WORDS.match(said), said)
        for said in ["press play", "submit the form to my boss", "send it"]:
            self.assertFalse(planner.SUBMIT_WORDS.match(said), said)

    def test_done_only_on_a_change_of_the_field(self):
        self.after, self.effect_after = {}, {"exists": "no"}
        v = self.submit()
        self.assertEqual((v["state"], v["steps"][0]["facts"]["changed"]), ("completed", ["field_gone"]))
        self.assertIn("not that anything was accepted", v["steps"][0]["facts"]["means"])

    def test_focus_moving_alone_proves_nothing(self):
        self.after, self.effect_after = {}, {"focus": object()}  # the user clicked elsewhere
        v = self.submit()
        self.assertEqual((v["state"], v["steps"][0]["facts"]["observed"]), ("unverified", ["focus_moved"]))
        self.after, self.effect_after = {}, {}
        self.value["v"] = "sent text"
        orig = self.do_confirm
        self.do_confirm = lambda ref, d: (orig(ref, d), self.value.update(v=""))[0]
        patch.object(screen, "confirm", self.do_confirm).start()
        v = self.submit()
        self.assertEqual(v["steps"][0]["facts"]["changed"], ["field_text_changed"])

    def test_no_field_change_is_unverified(self):
        self.after, self.effect_after = {}, {}
        self.assertEqual(self.submit()["state"], "unverified")

    def test_nothing_to_submit(self):
        self.facts["confirm"] = False
        self.effect_after = {}
        v = self.submit()
        self.assertEqual((v["state"], self.confirms), ("failed", []))

    def test_submit_asks_first_by_default(self):
        self.effect_after = {}
        v = self.submit(policy=dict(actions.DEFAULT_POLICY))
        self.assertEqual((v["state"], self.confirms), ("declined", []))


class FakeApp:
    def __init__(self, pid, activated=None):
        self.pid, self.activated = pid, activated

    def processIdentifier(self):
        return self.pid

    def localizedName(self):
        return f"App{self.pid}"

    def bundleIdentifier(self):
        return f"com.app{self.pid}"

    def isTerminated(self):
        return False

    def activateWithOptions_(self, opts):
        self.activated.append(self.pid)


class Foreground:
    """Fakes NSWorkspace/NSRunningApplication: `front` is the pid the system says is in front."""

    def __init__(self, test, front):
        import AppKit
        import os
        self.front, self.me, self.activated = front, os.getpid(), []
        ws = type("WS", (), {"sharedWorkspace": staticmethod(lambda: ws_inst)})
        ws_inst = type("W", (), {"frontmostApplication": lambda _s: FakeApp(self.front, self.activated)})()
        run = type("RA", (), {"currentApplication": staticmethod(lambda: FakeApp(self.me)),
                              "runningApplicationWithProcessIdentifier_": staticmethod(lambda pid: FakeApp(pid))})
        for p in (patch.object(AppKit, "NSWorkspace", ws), patch.object(AppKit, "NSRunningApplication", run),
                  patch.object(screen, "_handoff", [None, None])):
            p.start()
            test.addCleanup(p.stop)


class FrontmostTests(unittest.TestCase):
    def test_the_real_foreground_app_is_authoritative(self):
        fg = Foreground(self, 333)
        screen.set_handoff(222)  # a stale handoff never overrides an external app in front
        self.assertEqual(screen.frontmost()[0], 333)
        fg.front = fg.me  # our pop-down is in front: the app it took the foreground from
        self.assertEqual(screen.frontmost()[0], 222)
        screen.end_handoff()
        self.assertEqual(screen.frontmost()[0], 222)  # still good just after it closes, for the re-check
        with patch.object(screen, "HANDOFF_GRACE", 0):
            screen.set_handoff(222)
            screen.end_handoff()
            with self.assertRaises(screen.Unavailable):  # expired: never an arbitrary background app
                screen.frontmost()
        screen.set_handoff(None)
        with self.assertRaises(screen.Unavailable):
            screen.frontmost()

    def prompt(self, fg):
        import AppKit
        import types
        import assistant_ui
        d = assistant_ui.AppDelegate.alloc().init()
        d.status_item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(-1)
        self.addCleanup(lambda: AppKit.NSStatusBar.systemStatusBar().removeStatusItem_(d.status_item))
        p = patch.object(assistant_ui, "NSApp", types.SimpleNamespace(
            activateIgnoringOtherApps_=lambda *a: setattr(fg, "front", fg.me)))
        p.start()
        self.addCleanup(p.stop)
        return d

    def test_closing_the_prompt_gives_back_the_app_only_if_we_still_hold_the_foreground(self):
        fg = Foreground(self, 222)
        d = self.prompt(fg)
        d.showConfirm_({"token": "t", "text": "Click Save"})
        self.assertEqual((fg.front, screen.handoff()), (fg.me, 222))
        d.showConfirm_({})
        self.assertEqual(fg.activated, [222])
        fg.front, fg.activated[:] = 222, []
        d.showConfirm_({"token": "t2", "text": "Click Save"})
        fg.front = 333  # the user switched apps while it was open
        d.showConfirm_({})
        self.assertEqual(fg.activated, [])  # their choice stands

    def ask_first(self, switch_to=None):
        """A real engine and the real screen.press resolve, with the foreground faked instead of pinned."""
        import os
        from engine import Engine
        fg = Foreground(self, 222)
        save = item(1, "Save")
        snaps = {222: snap([save], pid=222), 333: snap([item(1, "Save")], pid=333)}
        presses = []
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HEYJEV_SCREEN_PID", None)
            fns = {"observe": lambda pid=None, ocr=True, deadline=None: snaps[pid or screen.frontmost()[0]],
                   "signature": lambda pid, d: {"n": len(presses)}, "element_state": lambda ref, d: {"v": len(presses)},
                   "press": lambda ref, d: presses.append(ref) or 0, "process_start": lambda pid, d: "s"}
            for name, fn in fns.items():
                p = patch.object(screen, name, fn)
                p.start()
                self.addCleanup(p.stop)

            def ask(pending):
                if not pending:
                    screen.end_handoff()
                    return
                screen.set_handoff(fg.front)  # what the pop-down does as it opens
                fg.front = fg.me
                if switch_to:
                    fg.front = switch_to
                eng.decide(pending["token"], True)
            with patch.object(diagnostics, "record", lambda *a, **k: None), \
                    patch.object(planner, "plan", lambda *a, **k: ("steps", [{"clause": "click Save",
                                                                              "action": "screen.press",
                                                                              "args": {"label": "Save"}}])):
                eng = Engine(lambda _: {}, policy=lambda: dict(actions.DEFAULT_POLICY), ask=ask)
                v = eng.wait(eng.submit("click Save", "cli")["id"], 10)
        return v, presses, save

    def test_ask_first_hands_off_to_the_same_target(self):
        v, presses, save = self.ask_first()
        self.assertEqual((v["state"], presses), ("completed", [save.ref]))

    def test_ask_first_then_the_user_switches_apps_presses_nothing(self):
        v, presses, _ = self.ask_first(switch_to=333)
        self.assertEqual((v["state"], v["steps"][0]["detail"], presses), ("failed", "target_changed", []))

class ScrollAndPointerTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(diagnostics, "record", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)

    def patch_all(self, fns):
        for name, fn in fns.items():
            p = patch.object(screen, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def run_action(self, action, args, policy=None):
        with patch.object(planner, "plan", lambda *a, **k: ("steps", [{"clause": "c", "action": action, "args": args}])):
            eng = Engine(lambda _: {}, policy=lambda: {**actions.DEFAULT_POLICY, "click": "auto", **(policy or {})})
            return eng.wait(eng.submit("c", "cli")["id"], 10)

    def test_direct_words_need_no_classification(self):
        called = []
        for said, want in [("scroll down", ("screen.scroll", {"direction": "down", "amount": "normal"})),
                           ("scroll up a little", ("screen.scroll", {"direction": "up", "amount": "little"})),
                           ("scroll to the top", ("screen.scroll", {"direction": "up", "amount": "end"})),
                           ("click", ("pointer.click", {"button": "left", "double": False})),
                           ("right click here", ("pointer.click", {"button": "right", "double": False}))]:
            kind, steps = planner.plan(said, lambda c: called.append(c) or {})
            self.assertEqual((kind, steps[0]["action"], steps[0]["args"]), ("steps", *want), said)
        self.assertEqual(called, [])
        for said in ["click Save", "click it", "click that", "click there"]:
            self.assertIsNone(planner.direct(said), said)  # "it/that/there" can name something discussed

    def test_every_setting_row_is_still_there(self):
        for e in ("scroll", "click", "type", "submit", "task", "in_task", "risky"):
            self.assertIn(e, actions.EFFECTS)
            self.assertIn(e, actions.EFFECT_LABELS)
        self.assertEqual(actions.DEFAULT_POLICY["in_task"], "auto")

    def scroll_setup(self, positions, err=0, app="Pad", front=None, window=None, member=True, switch_on_read=False):
        pos, sent, state = list(positions), [], {"front": front, "window": window}
        s = snap([item(1, "x")], app=app)

        def position(how, el, pick=None):
            if switch_on_read:
                state["front"] = 999  # the user switches apps while the position is being read
            return pos[0] if len(pos) == 1 else pos.pop(0)

        def scroll(how, el, d, a, dl, guard=None):
            why = guard() if guard else None
            if why:
                raise screen.Unavailable(why)
            sent.append((d, a))
            return err
        self.patch_all({"observe": lambda pid=None, ocr=True, deadline=None: s,
                        "scroll_target": lambda w: ("bar", "bar-el"), "scroll_position": position, "scroll": scroll,
                        "process_start": lambda pid, d: s.started, "in_window": lambda el, w: member,
                        "frontmost": lambda: (state["front"] or s.pid, "Pad", "com.pad"),
                        "current_window": lambda pid, d: state["window"] or s.window_token})
        return sent

    def test_scroll_completes_only_when_the_bar_moved(self):
        sent = self.scroll_setup([0.0, 0.25])
        v = self.run_action("screen.scroll", {"direction": "down", "amount": "normal"})
        self.assertEqual((v["state"], sent), ("completed", [("down", "normal")]))
        self.scroll_setup([0.5])
        self.assertEqual(self.run_action("screen.scroll", {"direction": "down"})["state"], "unverified")

    def test_invalid_readback_never_verifies(self):
        for bad in [("ok", float("nan")), ("ok", float("inf")), ("ok", 3.0), ("ok", True), ("unknown", None)]:
            with patch.object(screen, "_read", lambda el, n, bad=bad: bad):
                self.assertIsNone(screen.scroll_position("bar", "el"), bad)

    def test_scroll_at_the_edge_says_so(self):
        self.scroll_setup([1.0], err=screen.AX_NO_VALUE)
        v = self.run_action("screen.scroll", {"direction": "down"})
        self.assertEqual((v["state"], v["steps"][0]["detail"]), ("failed", "already at the bottom"))

    def test_scroll_rechecks_everything_right_before_writing(self):
        for kw in ({"front": 999}, {"window": "another"}, {"member": False}, {"switch_on_read": True}):
            sent = self.scroll_setup([0.0, 0.3], **kw)
            v = self.run_action("screen.scroll", {"direction": "down"})
            self.assertEqual((v["state"], sent), ("failed", []), kw)

    def test_web_scroll_is_sent_never_checked(self):
        s = snap([item(1, "x")])
        sent = []
        self.patch_all({"observe": lambda pid=None, ocr=True, deadline=None: s,
                        "scroll_target": lambda w: ("web", "area"), "in_window": lambda el, w: True,
                        "process_start": lambda pid, d: s.started, "frontmost": lambda: (s.pid, "Pad", "com.pad"),
                        "current_window": lambda pid, d: s.window_token,
                        "scroll": lambda how, el, d, a, dl, guard=None: (guard(), sent.append(d), 0)[2]})
        v = self.run_action("screen.scroll", {"direction": "down"})
        self.assertEqual((v["state"], sent, v["steps"][0]["facts"]["why"]),
                         ("unverified", ["down"], "this page doesn't report its scroll position"))

    def test_an_unreadable_scroll_bar_is_never_written(self):
        writes = []

        class AS:
            def AXUIElementSetAttributeValue(self, el, name, v):
                writes.append(v)
                return 0
        for value in [("unknown", None), ("ok", float("nan")), ("ok", 7.0), ("ok", True)]:
            with patch.object(screen, "_AS", lambda: AS()), patch.object(screen, "_read", lambda el, n: value), \
                    patch.object(screen, "_settable", lambda el, n: True), patch.object(screen, "_abandoned", []):
                with self.assertRaises(screen.Unavailable):
                    screen.scroll("bar", "el", "down", "normal", time.monotonic() + 1)
        with patch.object(screen, "_AS", lambda: AS()), patch.object(screen, "_read", lambda el, n: ("ok", 1.0)), \
                patch.object(screen, "_settable", lambda el, n: True), patch.object(screen, "_abandoned", []):
            self.assertEqual(screen.scroll("bar", "el", "down", "normal", time.monotonic() + 1), screen.AX_NO_VALUE)
        self.assertEqual(writes, [])

    def test_a_covered_spot_is_never_a_click_target(self):
        import Quartz
        wins = [{"kCGWindowLayer": 3, "kCGWindowOwnerPID": 50, "kCGWindowNumber": 1,
                 "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 500, "Height": 500}},
                {"kCGWindowLayer": 0, "kCGWindowOwnerPID": 7, "kCGWindowNumber": 2,
                 "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 800, "Height": 800}}]
        with patch.object(Quartz, "CGWindowListCopyWindowInfo", lambda *a: wins):
            self.assertEqual(screen.app_at((100, 100)), (None, None))
            self.assertEqual(screen.app_at((600, 600)), (7, 2))

    def pointer_setup(self, points, change=True):
        pts, clicks, sig = list(points), [], {"n": 0}
        self.patch_all({"pointer": lambda: pts[0] if len(pts) == 1 else pts.pop(0),
                        "app_at": lambda p: (7, 99), "_app_info": lambda pid: ("Pad", "com.pad"),
                        "process_start": lambda pid, d: "s", "signature": lambda pid, d: dict(sig),
                        "click_at": lambda p, b, dbl, d, expect=None: (clicks.append((p, b, dbl)),
                                                                       change and sig.update(n=1), 0)[2]})
        return clicks

    def test_click_at_the_pointer_is_delivered_never_done(self):
        clicks = self.pointer_setup([(100.4, 200.6)])
        v = self.run_action("pointer.click", {"button": "left", "double": False})
        self.assertEqual((v["state"], clicks), ("unverified", [((100, 201), "left", False)]))
        self.assertEqual(v["steps"][0]["facts"]["observed"], ["n"])  # recorded, but a timer could do the same

    def test_a_pointer_that_moved_after_the_ok_never_clicks(self):
        clicks = self.pointer_setup([(100, 200), (400, 300)])
        v = self.run_action("pointer.click", {"button": "left"})
        self.assertEqual((v["state"], v["steps"][0]["detail"], clicks), ("failed", "the pointer moved", []))

    def test_the_pointer_moving_between_events_stops_the_click(self):
        import Quartz
        posted, where = [], [(100, 100)]
        with patch.object(Quartz, "CGEventPost", lambda tap, e: (posted.append(e), where.__setitem__(0, (900, 900)))), \
                patch.object(screen, "pointer", lambda: where[0]), patch.object(screen, "app_at", lambda p: (7, 99)), \
                patch.object(screen, "_abandoned", []):
            with self.assertRaises(screen.Moved) as ctx:
                screen.click_at((100, 100), "left", True, time.monotonic() + 2, expect=(7, 99))
        self.assertEqual((len(posted), ctx.exception.args[0]), (2, 2))  # the first click went down and up, no second




class PickTests(unittest.TestCase):
    """"the third video": Jev classifies the controls, the code counts or finds the place, the press is exact."""

    def setUp(self):
        p = patch.object(diagnostics, "record", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)
        self.sent, self.presses = [], []

    def grid(self):
        # a 2x2 grid of videos with channel names and a menu, as a video site lays it out
        vids = [item(1, "Video A", role="AXLink", frame=(10, 10, 150, 20)), item(2, "Chan A", role="AXLink", frame=(10, 32, 80, 14)),
                item(3, "Video B", role="AXLink", frame=(200, 12, 150, 20)), item(4, "Chan B", role="AXLink", frame=(200, 34, 80, 14)),
                item(5, "Video C", role="AXLink", frame=(10, 210, 150, 20)), item(6, "Video D", role="AXLink", frame=(200, 208, 150, 20)),
                item(7, "Menu", frame=(380, 5, 20, 20))]
        return snap(vids)

    def run_pick(self, args, current, answer=None, policy=None):
        def classify(noun, labels):
            self.sent.append((noun, list(labels)))
            return answer(labels) if answer else [(l.startswith("Video"), 0.95) for l in labels]
        fns = {"observe": lambda pid=None, ocr=True, deadline=None: current,
               "signature": lambda pid, d: {"n": len(self.presses)},
               "element_state": lambda ref, d: {"v": str(len(self.presses))},
               "press": lambda ref, d: self.presses.append(ref) or 0}
        for name, fn in fns.items():
            p = patch.object(screen, name, fn)
            p.start()
            self.addCleanup(p.stop)
        with patch.object(actions, "CLASSIFY_ITEMS", classify), \
                patch.object(planner, "plan", lambda *a, **k: ("steps", [{"clause": "c", "action": "screen.pick",
                                                                          "args": args}])):
            eng = Engine(lambda _: {}, policy=lambda: {**actions.DEFAULT_POLICY, "click": "auto", **(policy or {})})
            return eng.wait(eng.submit("c", "cli")["id"], 10)

    def test_the_words(self):
        cases = {"click the third video": {"noun": "video", "ordinal": 3},
                 "click the third video in the chrome tab": {"noun": "video", "ordinal": 3, "app": "chrome"},
                 "click the video in the bottom-right": {"noun": "video", "where": "bottom-right"},
                 "play the last song": {"noun": "song", "ordinal": -1},
                 "open the 2nd result": {"noun": "result", "ordinal": 2}}
        for said, want in cases.items():
            self.assertEqual(planner.pick_args(said), want, said)
        for said in ["click the video", "click Save", "click the third"]:
            self.assertIsNone(planner.pick_args(said), said)

    def test_the_third_video_counts_rows_then_columns(self):
        g = self.grid()
        v = self.run_pick({"noun": "video", "ordinal": 3}, g)
        self.assertEqual((v["state"], self.presses), ("completed", [g.items[4].ref]))  # Video C starts row two
        self.assertEqual(self.sent[0][0], "video")

    def test_the_video_in_the_bottom_right(self):
        g = self.grid()
        v = self.run_pick({"noun": "video", "where": "bottom-right"}, g)
        self.assertEqual(self.presses, [g.items[5].ref])  # Video D

    def test_the_last_one_and_out_of_range(self):
        g = self.grid()
        self.run_pick({"noun": "video", "ordinal": -1}, g)
        self.assertEqual(self.presses, [g.items[5].ref])
        self.presses.clear()
        v = self.run_pick({"noun": "video", "ordinal": 9}, g)
        self.assertEqual((v["steps"][0]["detail"], self.presses), ("only 4 videos on screen", []))

    def test_unsure_or_malformed_classification_presses_nothing(self):
        g = self.grid()
        v = self.run_pick({"noun": "video", "ordinal": 1}, g, answer=lambda labels: [(True, 0.5)] * len(labels))
        self.assertEqual((v["state"], self.presses), ("failed", []))  # below the gate: no videos counted
        for bad in (lambda l: [(True, float("nan"))] * len(l), lambda l: [("yes", 0.9)] * len(l),
                    lambda l: [(True, 0.9)], lambda l: None):
            v = self.run_pick({"noun": "video", "ordinal": 1}, g, answer=bad)
            self.assertEqual((v["state"], self.presses), ("needs_clarification", []))

    def test_role_nouns_need_no_jev(self):
        g = self.grid()
        v = self.run_pick({"noun": "button", "ordinal": 1}, g)
        self.assertEqual((v["state"], self.presses, self.sent), ("completed", [g.items[6].ref], []))

    def test_only_control_names_go_to_jev(self):
        g = self.grid()
        g.items.append(item(8, "secret note", source="ocr", role="text", pressable=False))
        self.run_pick({"noun": "video", "ordinal": 1}, g)
        self.assertNotIn("secret note", self.sent[0][1])

    def test_a_named_app_must_be_in_front(self):
        v = self.run_pick({"noun": "video", "ordinal": 1, "app": "Chrome"}, self.grid())
        self.assertEqual((v["steps"][0]["detail"], self.presses), ("that app isn't in front", []))


class CardTests(unittest.TestCase):
    def test_a_card_carries_the_words_around_a_control_and_where_it_is(self):
        title = item(1, "Opus 5.5 is here", role="AXLink", frame=(600, 100, 250, 20))
        channel = item(2, "Nate Herk", role="AXLink", frame=(600, 124, 90, 14))
        age = item(3, "3 days ago", source="ocr", role="text", pressable=False, frame=(700, 124, 70, 14))
        far = item(4, "Unrelated sidebar", role="AXLink", frame=(10, 500, 120, 14))
        s = snap([title, channel, age, far])
        s.text_frames = [(0, 0, 1000, 1000)]
        s.window_frame = (0, 0, 900, 800)
        cards = actions.describe_cards([title], s, s.window_frame)
        self.assertEqual(len(cards), 1)
        self.assertTrue(cards[0].startswith("“Opus 5.5 is here”, near: "))
        self.assertEqual(set(cards[0].split("near: ")[1].rsplit(", ", 1)[0].split(" · ")), {"Nate Herk", "3 days ago"})
        self.assertTrue(cards[0].endswith(", top-right"))  # the sidebar link far below isn't part of this card

    def test_the_chooser_gets_cards_and_presses_that_exact_one(self):
        with patch.object(diagnostics, "record", lambda *a, **k: None):
            a = item(1, "Watch", role="AXLink", frame=(10, 100, 100, 20))
            b = item(2, "Watch", role="AXLink", frame=(600, 100, 100, 20))
            ch = item(3, "Nate Herk", role="AXLink", frame=(600, 124, 90, 14))
            s = snap([a, b, ch])
            s.window_frame = (0, 0, 800, 600)
            presses, seen = [], []
            for name, fn in {"observe": lambda pid=None, ocr=True, deadline=None: s,
                             "signature": lambda pid, d: {"n": len(presses)},
                             "element_state": lambda ref, d: {"v": str(len(presses))},
                             "press": lambda ref, d: presses.append(ref) or 0}.items():
                p = patch.object(screen, name, fn)
                p.start()
                self.addCleanup(p.stop)

            def choose(spoken, cards):
                seen.append(cards)
                return (next(k for k, c in enumerate(cards) if "Nate Herk" in c and "Watch" in c), 0.9)
            with patch.object(actions, "CHOOSE", choose), \
                    patch.object(planner, "plan", lambda *a, **k: ("steps", [{"clause": "c", "action": "screen.press",
                                                                              "args": {"label": "the one by Nate Herk"}}])):
                eng = Engine(lambda _: {}, policy=lambda: {**actions.DEFAULT_POLICY, "click": "auto"})
                v = eng.wait(eng.submit("c", "cli")["id"], 10)
        self.assertEqual((v["state"], presses), ("completed", [b.ref]))  # the right one of two identical labels


class DisplayTests(unittest.TestCase):
    def test_overlays_land_on_the_display_above_an_ultrawide_main(self):
        """Johnny's layout: a 3200x1350 main display and a 2560x1440 one above it (y from -1440 to 0)."""
        import AppKit
        import assistant_ui
        AppKit.NSApplication.sharedApplication()
        view = {"app": "Chrome", "at": time.time(), "ms": 1, "complete": True, "version": 1,
                "items": [{"n": 1, "source": "ax", "role": "AXLink", "label": "Video", "frame": [300, -1300, 200, 20],
                           "pressable": True, "shared": True, "field": False}]}
        main_h = AppKit.NSScreen.screens()[0].frame().size.height
        w = assistant_ui.inspect_window(view)
        f = w.frame()
        self.assertGreater(f.origin.y, main_h)  # Cocoa y above the main display: the upper screen
        self.assertLess(f.origin.x, 300)
        w2 = assistant_ui.numbers_window({"items": view["items"]})
        self.assertGreater(w2.frame().origin.y, main_h)

    def test_reading_order_and_places_on_a_very_wide_window(self):
        wide = (0, 0, 3200, 1300)
        items = [item(k, f"v{k}", frame=(x, y, 300, 20)) for k, (x, y) in
                 enumerate([(2800, 40), (100, 40), (1500, 42), (100, 700), (2900, 1200)], 1)]
        self.assertEqual([i.label for i in actions.reading_order(items)], ["v2", "v3", "v1", "v4", "v5"])
        self.assertEqual(actions.nearest_to(items, "bottom-right", wide).label, "v5")
        self.assertEqual(actions.nearest_to(items, "top-left", wide).label, "v2")


if __name__ == "__main__":
    unittest.main()
