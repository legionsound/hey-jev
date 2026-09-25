"""Live inspection: stable numbers, stale refreshes dropped, spoken numbers bound to what was shown."""
import threading
import time
import unittest
from unittest.mock import patch

import actions
import diagnostics
import inspector
import planner
import screen
from engine import Engine

WIN = object()


def item(label, ref=None, source="ax", role="AXButton", frame=(10, 10, 80, 30), pressable=True):
    return screen.Item(0, source, role, label, frame, pressable, ref=ref if ref is not None else object())


def snap(items, window=WIN, pid=7):
    s = screen.Snapshot(pid, "Pad", "com.pad", "Pad", (0, 0, 400, 300), items, window_ref=window, started="s",
                        text_frames=[(0, 0, 400, 300)])
    for n, i in enumerate(items, 1):
        i.n = n  # reading order, as observe() numbers them
    return s


class NumberingTests(unittest.TestCase):
    def test_an_item_keeps_its_number_while_it_stays_in_view(self):
        a, b, c = object(), object(), object()
        num = inspector.Numbering()
        first = num.apply(snap([item("Save", a), item("Open", b)]))
        self.assertEqual([i.n for i in first.items], [1, 2])
        after_scroll = num.apply(snap([item("New", c), item("Open", b)]))  # Save scrolled away, New scrolled in
        self.assertEqual({i.label: i.n for i in after_scroll.items}, {"New": 3, "Open": 2})

    def test_a_replacement_at_the_same_spot_gets_a_new_number(self):
        num = inspector.Numbering()
        num.apply(snap([item("Save", object())]))
        again = num.apply(snap([item("Save", object())]))  # same label and place, a different element
        self.assertEqual(again.items[0].n, 2)

    def test_numbering_restarts_for_another_window(self):
        num = inspector.Numbering()
        num.apply(snap([item("Save", object()), item("Open", object())]))
        other = num.apply(snap([item("Close", object())], window=object()))
        self.assertEqual(other.items[0].n, 1)


class InspectorTests(unittest.TestCase):
    def test_a_refresh_that_lands_after_stop_is_dropped(self):
        shown, release = [], threading.Event()

        def slow_observe(deadline):
            release.wait(2)
            return snap([item("Save")])
        ins = inspector.Inspector(shown.append, observe=slow_observe)
        with patch.object(inspector, "PERIOD", 0.05), patch.object(screen, "remember", lambda s: None):
            ins.start()
            time.sleep(0.1)
            ins.stop()  # toggled off while a read is out
            release.set()
            time.sleep(0.3)
        self.assertEqual(shown, [None])  # only the stop's clear, never the stale view

    def test_no_reads_while_a_command_runs(self):
        reads = []
        ins = inspector.Inspector(lambda v: None, busy=lambda: True, observe=lambda d: reads.append(1))
        with patch.object(inspector, "PERIOD", 0.05):
            ins.start()
            time.sleep(0.3)
            ins.stop()
        self.assertEqual(reads, [])

    def test_the_view_says_what_would_be_shared(self):
        field = item("Message", role="AXTextField", pressable=False, frame=(10, 100, 300, 30))
        leak = item("draft text", source="ocr", role="text", pressable=False, frame=(20, 105, 100, 20))
        ok = item("Hello", source="ocr", role="text", pressable=False, frame=(20, 200, 60, 20))
        s = snap([field, leak, ok])
        s.field_frames = [(10, 100, 300, 30)]
        ins = inspector.Inspector(lambda v: None, observe=lambda d: s)
        ins.gen = 5
        with patch.object(screen, "remember", lambda sn: None):
            view = ins.refresh(5)
        shared = {i["label"]: i["shared"] for i in view["items"]}
        self.assertEqual(shared, {"Message": True, "draft text": False, "Hello": True})
        self.assertTrue([i for i in view["items"] if i["label"] == "Message"][0]["field"])


class OwnershipTests(unittest.TestCase):
    def test_stop_between_read_and_publish_installs_nothing(self):
        installed = []
        ins = inspector.Inspector(lambda v: None, observe=lambda d: snap([item("Save")]))
        ins.gen = 9
        real = inspector.task.shareable

        def stop_midway(sn):
            ins.stop()  # the toggle goes off while this refresh is being prepared
            return real(sn)
        with patch.object(inspector.task, "shareable", stop_midway), patch.object(screen, "remember",
                                                                                  lambda sn: installed.append(sn)):
            self.assertIsNone(ins.refresh(9))
        self.assertEqual(installed, [])

    def test_a_command_starting_during_the_read_discards_it(self):
        busy = [False]

        def read(deadline):
            busy[0] = True  # a command began while the window was being read
            return snap([item("Save")])
        installed = []
        ins = inspector.Inspector(lambda v: None, busy=lambda: busy[0], observe=read)
        ins.gen = 4
        with patch.object(screen, "remember", lambda sn: installed.append(sn)):
            self.assertIsNone(ins.refresh(4))
        self.assertEqual(installed, [])

    def test_a_queued_paint_from_before_the_toggle_went_off_never_paints(self):
        import AppKit
        import assistant_ui
        AppKit.NSApplication.sharedApplication()
        d = assistant_ui.AppDelegate.alloc().init()
        d.inspector = inspector.Inspector(lambda v: None, observe=lambda dl: snap([]))
        d.inspector.gen = 0  # already off
        view = {"gen": 3, "app": "Pad", "at": time.time(), "ms": 1, "complete": True,
                "items": [{"n": 1, "source": "ax", "role": "AXButton", "label": "Save", "frame": [10, 10, 80, 30],
                           "pressable": True, "shared": True, "field": False}]}
        d.showInspect_(view)
        self.assertIsNone(getattr(d, "inspect_window", None))

    def test_the_readout_ages_from_the_read_and_says_stale(self):
        import assistant_ui
        view = {"app": "Pad", "at": time.time() - 10, "ms": 200, "complete": False, "items": [{"n": 1}]}
        text = assistant_ui.hud_text(view)
        self.assertIn("STALE", text)
        self.assertIn("OCR withheld", text)
        self.assertNotIn("no screen text shared", text)


class SpokenNumberTests(unittest.TestCase):
    """"click N" means the number the overlay showed, re-checked against the live element."""

    def setUp(self):
        p = patch.object(diagnostics, "record", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)

    def press(self, number, current):
        presses = []
        fns = {"observe": lambda pid=None, ocr=True, deadline=None: current,
               "signature": lambda pid, d: {"n": len(presses)}, "element_state": lambda ref, d: {"v": str(len(presses))},
               "press": lambda ref, d: presses.append(ref) or 0}
        for name, fn in fns.items():
            p = patch.object(screen, name, fn)
            p.start()
            self.addCleanup(p.stop)
        with patch.object(planner, "plan", lambda *a, **k: ("steps", [{"clause": "c", "action": "screen.press",
                                                                        "args": {"number": number}}])):
            eng = Engine(lambda _: {}, policy=lambda: {**actions.DEFAULT_POLICY, "click": "auto"})
            return eng.wait(eng.submit("c", "cli")["id"], 10), presses

    def test_a_number_from_before_a_scroll_never_retargets(self):
        save, new = object(), object()
        num = inspector.Numbering()
        shown = num.apply(snap([item("Save", save), item("Open", object())]))
        screen.remember(shown)
        now = snap([item("New", new), item("Open", shown.items[1].ref)])  # Save scrolled away; the list moved
        v, presses = self.press(1, now)  # 1 was Save on screen; in the fresh read position 1 is New
        self.assertEqual((v["state"], presses), ("failed", []))

    def press_as_heard(self, number, version, current):
        presses = []
        fns = {"observe": lambda pid=None, ocr=True, deadline=None: current,
               "signature": lambda pid, d: {"n": len(presses)}, "element_state": lambda ref, d: {"v": str(len(presses))},
               "press": lambda ref, d: presses.append(ref) or 0}
        for name, fn in fns.items():
            p = patch.object(screen, name, fn)
            p.start()
            self.addCleanup(p.stop)
        with patch.object(planner, "plan", lambda *a, **k: ("steps", [{"clause": "c", "action": "screen.press",
                                                                        "args": {"number": number}}])):
            eng = Engine(lambda _: {}, policy=lambda: {**actions.DEFAULT_POLICY, "click": "auto"})
            return eng.wait(eng.submit("click 1", "voice", shown=version)["id"], 10), presses

    def test_a_number_means_what_was_on_screen_when_it_was_said(self):
        old_save = object()
        heard = screen.remember(inspector.Numbering().apply(snap([item("Save", old_save)])))  # user sees 1 = Save
        other = inspector.Numbering().apply(snap([item("Delete all", object())], window=object()))
        screen.remember(other)  # a refresh of another window installs its own 1 before the turn submits
        v, presses = self.press_as_heard(1, heard, other)
        self.assertEqual((v["state"], presses), ("failed", []))  # never the new window's 1

    def test_an_observed_but_unpainted_list_never_becomes_the_numbers(self):
        old = inspector.Numbering().apply(snap([item("Save", object())]))
        v1 = screen.remember(old)
        screen.set_displayed(v1)  # painted
        t0 = time.monotonic()
        fresh = inspector.Numbering().apply(snap([item("Delete all", object())], window=object()))
        screen.remember(fresh)  # observed by a refresh whose paint hasn't happened yet
        heard = screen.bind_spoken(t0, time.monotonic())
        self.assertEqual(heard, v1)
        v, presses = self.press_as_heard(1, heard, fresh)
        self.assertEqual((v["state"], presses), ("failed", []))

    def test_the_display_changing_mid_sentence_refuses_the_number(self):
        screen.set_displayed(111, at=time.monotonic() - 5)
        t0 = time.monotonic()
        screen.set_displayed(222, at=t0 + 0.01)  # a refresh painted while the user was talking
        self.assertEqual(screen.bind_spoken(t0, t0 + 0.5), "unstable")
        v, presses = self.press_as_heard(1, "unstable", snap([item("Save")]))
        self.assertEqual((v["state"], presses), ("failed", []))

    def test_a_spoken_number_with_no_known_list_is_refused(self):
        v, presses = self.press_as_heard(1, None, snap([item("Save")]))  # voice, provenance unknown
        self.assertEqual((v["steps"][0]["detail"], presses),
                         ("the numbers changed since you spoke; ask what you can click again", []))

    def test_a_number_too_old_to_know_is_refused(self):
        with patch.object(screen, "_shown", {}):
            v, presses = self.press_as_heard(1, 12345, snap([item("Save")]))
        self.assertEqual((v["state"], v["steps"][0]["detail"], presses),
                         ("failed", "the numbers changed since you spoke; ask what you can click again", []))

    def test_the_shown_number_presses_that_exact_element(self):
        open_ref = object()
        num = inspector.Numbering()
        num.apply(snap([item("Save", object()), item("Open", open_ref)]))
        shown = num.apply(snap([item("New", object()), item("Open", open_ref)]))  # Open keeps 2, New is 3
        screen.remember(shown)
        v, presses = self.press(2, shown)
        self.assertEqual((v["state"], presses), ("completed", [open_ref]))


class WindowTests(unittest.TestCase):
    def test_the_overlay_is_never_captured_and_works_on_any_display(self):
        import AppKit
        import assistant_ui
        AppKit.NSApplication.sharedApplication()
        view = {"app": "Pad", "at": time.time(), "ms": 120, "complete": True,
                "items": [{"n": 1, "source": "ax", "role": "AXButton", "label": "Save", "frame": [-1500, 200, 80, 30],
                           "pressable": True, "shared": True, "field": False},
                          {"n": 2, "source": "ocr", "role": "text", "label": "words", "frame": [-1400, 300, 60, 20],
                           "pressable": False, "shared": False, "field": False}]}
        w = assistant_ui.inspect_window(view)
        self.assertEqual(w.sharingType(), 0)  # NSWindowSharingNone: OCR can never read the overlay back
        self.assertTrue(w.ignoresMouseEvents())
        self.assertLess(w.frame().origin.x, -1500)  # a display to the left of the main one


if __name__ == "__main__":
    unittest.main()
