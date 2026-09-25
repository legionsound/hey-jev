"""Johnny, 2026-09-25: "I want Jev to see my entire screen at once and pick from buttons inside multiple apps without
me selecting that app." One numbered list across every visible window; a number presses exactly that control in its
own window; a web page behind is brought forward only for a keyboard press."""
import time
import unittest
from unittest.mock import patch

import actions
import diagnostics
import inspector
import planner
import screen
from engine import Engine
from test_screen import FakeScreen, item, snap


def desktop(wins, reads, front_pid, ocr_seen=None):
    """screen.desktop_view() over fake windows: wins [(pid, frame)] front to back, reads {pid: Snapshot}."""
    def observe(pid=None, ocr=True, deadline=None, window=None, walk_cap=None):
        if ocr_seen is not None:
            ocr_seen.append((pid, ocr, walk_cap))
        return reads[pid]
    def front(pid, deadline):
        if ocr_seen is not None:
            ocr_seen.append((pid, "with text", None))
        return reads[pid], lambda: reads[pid]
    with patch.object(screen, "trusted", lambda: True), patch.object(screen, "frontmost", lambda: (front_pid, "x", "x")), \
            patch.object(screen, "visible_windows", lambda: wins), patch.object(screen, "front_with_text", front), \
            patch.object(screen, "_ax_window", lambda pid, frame: object()), patch.object(screen, "observe", observe):
        return screen.desktop_view()


class DesktopViewTests(unittest.TestCase):
    def two_apps(self):
        pages = snap([item(1, "Save", frame=(100, 100, 60, 20))], pid=1, window="Doc", app="Pages")
        mail = snap([item(1, "Send", frame=(900, 500, 60, 20)), item(2, "Hidden", frame=(120, 105, 40, 20))],
                    pid=2, window="Draft", app="Mail")
        return pages, mail

    def test_one_list_numbers_run_on_across_windows_each_item_keeps_its_window(self):
        pages, mail = self.two_apps()
        seen = []
        view = desktop([(1, (0, 0, 800, 600)), (2, (50, 50, 1200, 900))], {1: pages, 2: mail}, 1, seen)
        self.assertTrue(view.desktop)
        self.assertEqual([(i.n, i.label, i.home.app) for i in view.items], [(1, "Save", "Pages"), (2, "Send", "Mail")])
        self.assertEqual((view.pid, view.app), (1, "Pages"))  # identity is the front window's
        self.assertEqual([(ocr, cap) for _, ocr, cap in seen], [("with text", None), (False, screen.BACK_WALK)])
        self.assertEqual([screen.window_index(i, view) for i in view.items], [0, 1])

    def test_numbers_hold_when_focus_moves_to_the_other_app(self):
        pages, mail = self.two_apps()
        num = inspector.Numbering()
        side = [(1, (0, 0, 400, 300)), (2, (850, 350, 600, 400))]  # side by side: nothing covers anything
        first = num.apply(desktop(side, {1: pages, 2: mail}, 1))
        before = {i.label: i.n for i in first.items}
        pages2, mail2 = snap(list(pages.items), pid=1, window="Doc", app="Pages"), \
            snap(list(mail.items), pid=2, window="Draft", app="Mail")
        again = num.apply(desktop(side[::-1], {1: pages2, 2: mail2}, 2))
        after = {i.label: i.n for i in again.items}
        self.assertEqual(before["Save"], after["Save"])
        self.assertEqual(before["Send"], after["Send"])

    def test_nothing_readable_is_unavailable_not_an_empty_list(self):
        with self.assertRaises(screen.Unavailable):
            desktop([], {}, 1)


class FrontTextTests(unittest.TestCase):
    def test_the_front_text_is_read_while_the_windows_behind_are(self):
        order = []
        part = {"pid": 1}

        def ocr(p, deadline):
            order.append("ocr start")
            time.sleep(0.2)
            order.append("ocr end")
            return ["t"], "ok"
        with patch.object(screen, "_observe_ax", lambda pid, d: part), patch.object(screen, "_ocr", ocr), \
                patch.object(screen, "_finish", lambda p, texts, st: (texts, st)):
            first, finish = screen.front_with_text(1, time.monotonic() + 2)
            order.append("back windows")
            t0 = time.monotonic()
            self.assertEqual((first, finish()), (([], "off"), (["t"], "ok")))
        self.assertLess(order.index("back windows"), order.index("ocr end"))

    def test_text_that_misses_the_deadline_leaves_the_controls(self):
        with patch.object(screen, "_observe_ax", lambda pid, d: {}), \
                patch.object(screen, "_ocr", lambda p, d: (time.sleep(1), (["late"], "ok"))[1]), \
                patch.object(screen, "_finish", lambda p, texts, st: (texts, st)):
            _, finish = screen.front_with_text(1, time.monotonic() + 0.1)
            self.assertEqual(finish(), ([], "timed_out"))


class DesktopNumberTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(diagnostics, "record", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)

    def listed(self):
        pages = snap([item(1, "Save")], pid=1, window="Doc", app="Pages")
        mail = snap([item(1, "Send", frame=(900, 500, 60, 20)),
                     item(2, "To", role="AXTextField", frame=(900, 400, 200, 20))], pid=2, window="Draft", app="Mail")
        view = desktop([(1, (0, 0, 800, 600)), (2, (850, 350, 600, 400))], {1: pages, 2: mail}, 1)
        screen.remember(view)
        return pages, mail, view

    def test_a_number_in_another_window_presses_that_control_there(self):
        pages, mail, view = self.listed()

        def observe(pid=None, ocr=True, deadline=None, window=None, walk_cap=None):
            return pages if pid in (None, 1) else mail
        with patch.object(screen, "observe", observe):
            got = actions.resolve_screen_press({"number": 2})
        self.assertEqual((got[0], got[1]["app"], got[1]["label"]), ("target", "Mail", "Send"))

    def test_a_number_whose_control_went_away_is_refused(self):
        pages, mail, view = self.listed()
        gone = snap([], pid=2, window="Draft", app="Mail")

        def observe(pid=None, ocr=True, deadline=None, window=None, walk_cap=None):
            return pages if pid in (None, 1) else gone
        with patch.object(screen, "observe", observe):
            self.assertEqual(actions.resolve_screen_press({"number": 2}), ("none", "that one isn't on screen anymore"))

    def test_typing_into_a_field_in_another_window_asks_to_bring_it_forward(self):
        pages, mail, view = self.listed()
        with patch.object(screen, "observe", lambda pid=None, ocr=True, deadline=None, window=None, walk_cap=None: pages):
            got = actions.resolve_screen_type({"number": 3, "text": "hi"})
        self.assertEqual(got, ("none", "that field is in another window; bring it to the front first"))

    def test_show_what_i_can_click_lists_every_window(self):
        pages, mail, view = self.listed()
        t = {}
        with patch.object(actions, "DESKTOP", lambda d: None), \
                patch.object(screen, "desktop_view", lambda deadline=None, ocr=True: view):
            actions.run_screen_list(t, time.monotonic() + 1)
            state, facts = actions.verify_screen_list(t, time.monotonic() + 1)
        self.assertEqual([(i["label"], i["win"]) for i in facts["items"]], [("Save", 0), ("Send", 1), ("To", 1)])


class BackWindowWebPressTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(diagnostics, "record", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)

    def run_press(self):
        with patch.object(planner, "plan", lambda *a, **k: ("steps", [{"clause": "click Watch", "action": "screen.press",
                                                                       "args": {"label": "Watch"}}])):
            eng = Engine(lambda _: {}, policy=lambda: {**actions.DEFAULT_POLICY, "click": "auto"})
            return eng.wait(eng.submit("click Watch", "cli")["id"], 10)

    def press_behind(self, raised):
        fake = FakeScreen(self, [item(1, "Watch", role="AXLink")])
        calls = []

        def page_key(pid, ref, role, deadline):
            calls.append("key")
            fake.state = {**fake.state, "exists": False}
            return 0

        def bring(pid, win, deadline):
            calls.append("raise")
            return raised
        with patch.object(screen, "chromium_page", lambda pid, ref, d: True), \
                patch.object(screen, "in_front", lambda pid, win, d: False), \
                patch.object(screen, "bring_forward", bring), patch.object(screen, "page_key", page_key):
            return self.run_press(), calls

    def test_a_page_behind_is_brought_forward_then_keyed(self):
        v, calls = self.press_behind(True)
        self.assertEqual((v["state"], calls), ("completed", ["raise", "key"]))
        self.assertTrue(v["steps"][0]["facts"]["brought_forward"])

    def test_a_window_that_wont_come_forward_gets_no_key(self):
        v, calls = self.press_behind(False)
        self.assertEqual((v["state"], calls), ("failed", ["raise"]))

    def test_native_controls_behind_are_pressed_where_they_are(self):
        fake = FakeScreen(self, [item(1, "Watch")])
        with patch.object(screen, "chromium_page", lambda pid, ref, d: False), \
                patch.object(screen, "in_front", lambda pid, win, d: False), \
                patch.object(screen, "bring_forward", lambda *a: self.fail("raised a native press")):
            v = self.run_press()
        self.assertEqual((v["state"], len(fake.presses)), ("completed", 1))


class OverlayTests(unittest.TestCase):
    def test_one_overlay_per_window_on_each_display_and_the_readout_names_every_app(self):
        import AppKit
        import assistant_ui
        AppKit.NSApplication.sharedApplication()
        main_h = AppKit.NSScreen.screens()[0].frame().size.height
        view = {"app": "Chrome", "apps": ["Chrome", "Finder"], "skipped": 1, "at": time.time(), "ms": 5,
                "complete": True, "version": 1,
                "items": [{"n": 1, "source": "ax", "role": "AXLink", "label": "Video", "frame": [300, 200, 200, 20],
                           "pressable": True, "shared": True, "field": False, "win": 0},
                          {"n": 2, "source": "ax", "role": "AXButton", "label": "Back", "frame": [300, -1300, 60, 20],
                           "pressable": True, "shared": True, "field": False, "win": 1}]}
        o = assistant_ui.inspect_window(view)
        self.assertEqual(len(o.windows), 2)
        self.assertLess(o.windows[0].frame().origin.y, main_h)
        self.assertGreater(o.windows[1].frame().origin.y, main_h)  # the upper display
        self.assertTrue(all(w.sharingType() == 0 and w.ignoresMouseEvents() for w in o.windows))
        hud = [s for s in o.windows[0].contentView().subviews() if s.identifier() == "hud"]
        self.assertIn("Chrome, Finder", hud[0].stringValue())
        self.assertIn("1 window not read", hud[0].stringValue())
        self.assertFalse(any(s.identifier() == "hud" for s in o.windows[1].contentView().subviews()))
        self.assertEqual(len(assistant_ui.numbers_window(view).windows), 2)


if __name__ == "__main__":
    unittest.main()
