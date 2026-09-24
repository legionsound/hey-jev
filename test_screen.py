"""Screen control: the walk, the merge, the planner's words, and press/list through the real engine with a fake screen."""
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


def item(n, label, source="ax", role="AXButton", frame=(10, 10, 80, 30), pressable=True):
    return screen.Item(n, source, role, label, frame, pressable, ref=object())


def snap(items, pid=7, window="Pad", app="Pad"):
    return screen.Snapshot(pid, app, "com.pad", window, (0, 0, 400, 300), items)


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
    """Patches screen's observe/last/signature/press so the real actions and engine run without touching the Mac."""

    def __init__(self, test, items, shown=None):
        self.current, self.shown, self.presses, self.changes = snap(items), shown, [], True
        self.sig = {"text": "a"}
        for name, fn in {"observe": self.observe, "last": lambda: self.shown, "signature": lambda pid: dict(self.sig),
                         "element_state": lambda ref: {}, "press": self.press}.items():
            p = patch.object(screen, name, fn)
            p.start()
            test.addCleanup(p.stop)
        test.addCleanup(patch.stopall)

    def observe(self, pid=None, ocr=True, deadline=None):
        return self.current

    def press(self, ref):
        self.presses.append(ref)
        if self.changes:
            self.sig = {"text": "b"}
        return 0


class ScreenActionTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(diagnostics, "record", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)
        self.chosen = None
        p = patch.object(actions, "CHOOSE", lambda spoken, labels: self.chosen or (None, 0.0))
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
        self.assertEqual((v["state"], v["steps"][0]["facts"]), ("completed", {"changed": ["text"]}))
        self.assertEqual(len(fake.presses), 1)

    def test_press_with_no_change_is_unverified_not_done(self):
        fake = FakeScreen(self, [item(1, "Add one")])
        fake.changes = False
        v = self.run_text("click Add one", "screen.press", {"label": "Add one"})
        self.assertEqual(v["state"], "unverified")

    def test_number_uses_the_list_the_user_saw_and_checks_it_is_still_there(self):
        shown = snap([item(1, "Title", "ocr", "text", pressable=False), item(2, "Save")])
        fake = FakeScreen(self, [item(1, "Title", "ocr", "text", pressable=False), item(2, "Save")], shown)
        self.assertEqual(self.run_text("click 2", "screen.press", {"number": 2})["state"], "completed")
        v = self.run_text("click 1", "screen.press", {"number": 1})  # OCR text is not a control
        self.assertEqual((v["state"], v["steps"][0]["detail"]), ("failed", "not_a_control"))
        fake.current = snap([item(1, "Title", "ocr", "text", pressable=False), item(2, "Save", frame=(50, 50, 80, 30))])
        v = self.run_text("click 2", "screen.press", {"number": 2})  # it moved: never press a guess
        self.assertEqual((v["state"], v["steps"][0]["detail"]), ("failed", "screen_changed"))
        fake.current = snap([item(2, "Save")], window="Other")
        self.assertEqual(self.run_text("click 2", "screen.press", {"number": 2})["steps"][0]["detail"], "screen_changed")
        self.assertEqual(len(fake.presses), 1)

    def test_identical_labels_ask_which(self):
        fake = FakeScreen(self, [item(1, "Buy", frame=(10, 10, 60, 30)), item(2, "Buy", frame=(300, 250, 60, 30))])
        v = self.run_text("click Buy", "screen.press", {"label": "Buy"})
        self.assertEqual(v["state"], "needs_clarification")
        self.assertEqual([c["name"] for c in v["steps"][0]["facts"]["choices"]], ["Buy (top-left)", "Buy (bottom-right)"])
        self.assertEqual(fake.presses, [])

    def test_jev_picks_only_above_the_gate(self):
        fake = FakeScreen(self, [item(1, "New Note"), item(2, "Delete Note")])
        self.chosen = (0, 0.5)
        self.assertEqual(self.run_text("click compose", "screen.press", {"label": "compose"})["state"], "failed")
        self.chosen = (0, 0.9)
        v = self.run_text("click compose", "screen.press", {"label": "compose"})
        self.assertEqual((v["state"], v["steps"][0]["target"]["label"]), ("completed", "New Note"))
        self.assertEqual(len(fake.presses), 1)

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

    def test_screen_text_stays_out_of_the_log(self):
        FakeScreen(self, [item(1, "Secret plan")])
        logged = []
        with patch.object(diagnostics, "record", lambda *a, **k: logged.append(k)):
            self.run_text("what can I click", "screen.list", {})
        verify = [k for k in logged if "facts" in k and k["facts"]]
        self.assertEqual(verify[-1]["facts"]["items"], 1)
        self.assertNotIn("Secret plan", repr(logged))


if __name__ == "__main__":
    unittest.main()
