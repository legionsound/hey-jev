"""Johnny's trial of 5882a59 (2026-09-25): Chrome ignored every click, "the first video" refused on a cold page,
"the NetworkChuck video" offered four unrelated videos, "pause the video" paused Spotify, "click Johnny" gave no way
to answer. Each test fails on 5882a59."""
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

import actions
import diagnostics
import planner
import screen
from engine import Engine
from test_screen import FakeScreen, PlannerTests, item, snap


class ChromiumPressTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(diagnostics, "record", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)

    def run_press(self, fake):
        with patch.object(planner, "plan", lambda *a, **k: ("steps", [{"clause": "click Watch", "action": "screen.press",
                                                                       "args": {"label": "Watch"}}])):
            eng = Engine(lambda _: {}, policy=lambda: {**actions.DEFAULT_POLICY, "click": "auto"})
            return eng.wait(eng.submit("click Watch", "cli")["id"], 10)

    def test_a_page_control_in_chromium_gets_focus_and_a_key_not_axpress(self):
        fake = FakeScreen(self, [item(1, "Watch", role="AXLink")])
        keys = []

        def page_key(pid, ref, role, deadline):
            keys.append((pid, role))
            fake.state = {**fake.state, "exists": False}  # the link navigated away
            return 0
        with patch.object(screen, "chromium_page", lambda pid, ref, d: True), patch.object(screen, "page_key", page_key):
            v = self.run_press(fake)
        self.assertEqual((v["state"], keys, fake.presses), ("completed", [(7, "AXLink")], []))

    def test_a_page_that_wont_focus_the_control_presses_nothing(self):
        fake = FakeScreen(self, [item(1, "Watch", role="AXLink")])
        with patch.object(screen, "chromium_page", lambda pid, ref, d: True), \
                patch.object(screen, "page_key", lambda *a: screen.NOT_FOCUSED):
            v = self.run_press(fake)
        self.assertEqual((v["state"], fake.presses), ("failed", []))

    def test_other_apps_keep_axpress(self):
        fake = FakeScreen(self, [item(1, "Watch")])
        with patch.object(screen, "chromium_page", lambda pid, ref, d: False):
            v = self.run_press(fake)
        self.assertEqual((v["state"], len(fake.presses)), ("completed", 1))

    def test_page_content_is_what_sits_under_a_web_area(self):
        web, win = object(), object()
        link, tool = object(), object()
        tree = {link: web, web: win, tool: win}
        roles = {web: "AXWebArea", win: "AXWindow"}

        def attr(el, name):
            return tree.get(el) if name == "AXParent" else roles.get(el) if name == "AXRole" else None
        with patch.object(screen, "_is_chromium", lambda pid: True), patch.object(screen, "_attr", attr):
            self.assertTrue(screen.chromium_page(1, link, time.monotonic() + 1))
            self.assertFalse(screen.chromium_page(1, tool, time.monotonic() + 1))  # Chrome's own toolbar
        with patch.object(screen, "_is_chromium", lambda pid: False), patch.object(screen, "_attr", attr):
            self.assertFalse(screen.chromium_page(1, link, time.monotonic() + 1))


def videos(n, labels=None):
    return [NS(source="ax", pressable=True, enabled=True, from_value=False, role="AXLink",
               label=(labels or [f"Video {i}" for i in range(n)])[i], frame=(0, i * 40, 100, 15), token=f"e{i}",
               secure=False) for i in range(n)]


def page(items, truncated=False):
    return NS(items=items, app="Chrome", window_frame=(0, 0, 500, 500), pid=1, started=1, window_token="w",
              truncated=truncated, field_frames=[], text_frames=[], walk_complete=True)


class PickTests(unittest.TestCase):
    def pick(self, args, flags, observe, cards=None, kind=None):
        # flags answer the qualified question; kind (per label) answers "is it a <noun> at all" for word matches
        classify = lambda noun, labels: [kind.get(l, (False, .99)) for l in labels] if kind and noun == args["noun"] else flags
        with patch.object(actions.screen, "observe", observe), \
                patch.object(actions, "CLASSIFY_ITEMS", classify), \
                patch.object(actions, "_live", lambda i: True), \
                patch.object(actions, "describe_cards", lambda pool, s, f: cards or [f"“{i.label}”, top-left" for i in pool]):
            return actions.resolve_screen_pick(args)

    def test_a_cut_first_read_is_read_again_longer(self):
        full = page(videos(3))
        calls = []

        def observe(pid=None, ocr=True, deadline=None, walk_cap=None):
            calls.append(walk_cap)
            return page(videos(3)[:1], truncated=True) if len(calls) == 1 else full
        got = self.pick({"noun": "video", "ordinal": 1}, [(True, .99)] * 3, observe)
        self.assertEqual((got[0], got[1]["label"]), ("target", "Video 0"))
        self.assertEqual(calls[0], None)
        self.assertGreater(calls[1], 0.6)

    def test_still_cut_after_the_longer_read_never_picks(self):
        got = self.pick({"noun": "video", "ordinal": 1}, [(True, .99)] * 3,
                        lambda **k: page(videos(3), truncated=True))
        self.assertEqual(got[0], "none")

    def test_heard_words_match_a_card_whatever_the_spacing(self):
        items = videos(3, ["Paperclip", "TV of Babel", "Doom"])
        cards = ["“Paperclip”, near: NetworkChuck · 2 days ago, top-left", "“TV of Babel”, top-centre",
                 "“Doom”, near: Counterpoint, top-right"]
        got = self.pick({"noun": "video", "kind": "network Chuck", "ordinal": 0}, [(True, .5), (True, .5), (True, .5)],
                        lambda **k: page(items), cards, kind={cards[0]: (True, .95)})
        self.assertEqual((got[0], got[1]["label"]), ("target", "Paperclip"))

    def test_the_words_prove_the_name_not_the_kind(self):
        # Astra: an unsure channel link named NetworkChuck is not a video just because the name matches
        items = videos(2, ["NetworkChuck", "Paperclip"])
        cards = ["“NetworkChuck”, top-left", "“Paperclip”, near: NetworkChuck, top-centre"]
        got = self.pick({"noun": "video", "kind": "networkchuck", "ordinal": 0}, [(True, .5), (True, .5)],
                        lambda **k: page(items), cards, kind={cards[0]: (False, .55), cards[1]: (True, .95)})
        self.assertEqual(got[0], "choices")  # the link stays unsure, so it asks
        got = self.pick({"noun": "video", "kind": "networkchuck", "ordinal": 0}, [(True, .5), (True, .5)],
                        lambda **k: page(items), cards, kind={cards[0]: (False, .95), cards[1]: (True, .95)})
        self.assertEqual((got[0], got[1]["label"]), ("target", "Paperclip"))

    def test_the_words_match_whole_words_only(self):
        # Astra: "the Apple video" must not settle on "Pineapple farming"
        items = videos(1, ["Pineapple farming"])
        got = self.pick({"noun": "video", "kind": "Apple", "ordinal": 0}, [(False, .6)], lambda **k: page(items))
        self.assertEqual(got[0], "choices")
        self.assertTrue(actions._says("Paperclip, near: Network Chuck", "networkchuck"))
        self.assertFalse(actions._says("Pineapple farming", "apple"))
        self.assertFalse(actions._says("Applesauce", "apple"))

    def test_a_sure_no_is_not_overruled_by_the_words(self):
        items = videos(2, ["NetworkChuck", "Paperclip"])
        cards = ["“NetworkChuck”, top-left", "“Paperclip”, near: NetworkChuck, top-centre"]
        got = self.pick({"noun": "video", "kind": "networkchuck", "ordinal": 0}, [(False, .95), (True, .4)],
                        lambda **k: page(items), cards, kind={cards[1]: (True, .95)})
        self.assertEqual((got[0], got[1]["label"]), ("target", "Paperclip"))  # the channel link is not the video

    def test_which_one_lists_the_likeliest_first(self):
        items = videos(5)
        flags = [(False, .6), (True, .3), (False, .2), (True, .6), (False, .5)]
        got = self.pick({"noun": "video", "kind": "blender", "ordinal": 0}, flags, lambda **k: page(items))
        self.assertEqual(got[0], "choices")
        self.assertEqual([c["target"]["label"] for c in got[1]], ["Video 3", "Video 1", "Video 2", "Video 4"])


class PageOnlyTests(unittest.TestCase):
    def test_the_first_video_counts_the_page_not_the_browser_tabs(self):
        tab = NS(source="ax", pressable=True, enabled=True, from_value=False, role="AXRadioButton", label="watch ?v=2",
                 frame=(200, 10, 150, 30), token="t", secure=False)
        vids = videos(2)
        for v in vids:
            v.frame = (v.frame[0], v.frame[1] + 100, v.frame[2], v.frame[3])
        snap_ = page([tab] + vids)
        snap_.window_ref = object()
        with patch.object(actions.screen, "observe", lambda **k: snap_), \
                patch.object(actions.screen, "page_frame", lambda win, d: (0, 80, 500, 420)), \
                patch.object(actions, "CLASSIFY_ITEMS", lambda noun, labels: [(True, .99)] * len(labels)), \
                patch.object(actions, "_live", lambda i: True):
            got = actions.resolve_screen_pick({"noun": "video", "ordinal": 1})
        self.assertEqual((got[0], got[1]["label"]), ("target", "Video 0"))

    def test_the_first_tab_is_the_browser_tab_strip(self):
        # Astra: the page filter is for content; "the first tab" still counts the window's tabs
        tab = NS(source="ax", pressable=True, enabled=True, from_value=False, role="AXRadioButton", label="Inbox",
                 frame=(10, 10, 150, 30), token="t", secure=False)
        inpage = NS(source="ax", pressable=True, enabled=True, from_value=False, role="AXTab", label="Overview",
                    frame=(10, 200, 100, 30), token="p", secure=False)
        snap_ = page([tab, inpage])
        snap_.window_ref = object()
        with patch.object(actions.screen, "observe", lambda **k: snap_), \
                patch.object(actions.screen, "page_frame", lambda win, d: (0, 80, 500, 420)), \
                patch.object(actions, "_live", lambda i: True):
            got = actions.resolve_screen_pick({"noun": "tab", "ordinal": 1})
        self.assertEqual((got[0], got[1]["label"]), ("target", "Inbox"))


class PageKeyDeadlineTests(unittest.TestCase):
    def run_key(self, focus_delay, budget):
        import sys
        posted = []
        quartz = NS(CGEventCreateKeyboardEvent=lambda src, key, down: (key, down),
                    CGEventPostToPid=lambda pid, ev: posted.append(ev))
        ref = object()

        def attr(el, name):
            time.sleep(focus_delay)
            return ref
        AS = NS(AXUIElementSetAttributeValue=lambda *a: 0, AXUIElementCreateApplication=lambda pid: object(),
                AXUIElementSetMessagingTimeout=lambda *a: 0)
        with patch.dict(sys.modules, {"Quartz": quartz}), patch.object(screen, "_AS", lambda: AS), \
                patch.object(screen, "_attr", attr), patch.object(screen, "EFFECT_SETTLE", 0.5):
            got = screen.page_key(1, ref, "AXLink", time.monotonic() + budget)
            time.sleep(focus_delay + 0.05)
        return got, posted

    def test_a_slow_focus_read_past_the_deadline_sends_nothing(self):
        # Astra repro: both key events went out ~65 ms after a 10 ms deadline
        got, posted = self.run_key(0.075, 0.01)
        self.assertEqual((got, posted), (screen.TOO_LATE, []))

    def test_in_time_sends_the_pair(self):
        got, posted = self.run_key(0, 1)
        self.assertEqual((got, posted), (0, [(screen.KEY_RETURN, True), (screen.KEY_RETURN, False)]))


class VideoMediaTests(unittest.TestCase):
    def answers(self, **kw):
        return PlannerTests.answers(None, "media", **kw)

    def test_pause_the_video_is_the_player_on_screen(self):
        for said in ("Pause the video", "resume the video", "pause this clip"):
            kind, steps = planner.plan(said, lambda _: self.answers(media_action=("pause", 0.99)))
            self.assertEqual((kind, steps[0]["action"], steps[0]["args"]), ("steps", "screen.press", {"intent": said}),
                             said)

    def test_music_stays_with_spotify(self):
        for said in ("pause the music", "pause", "pause the music video on Spotify"):
            kind, steps = planner.plan(said, lambda _: self.answers(media_action=("pause", 0.99)))
            self.assertEqual(steps[0]["action"], "media.pause", said)

    def test_never_inside_a_multi_step_request(self):
        kind, got = planner.plan("turn it up, then pause the video", lambda _: self.answers(media_action=("pause", 0.99)))
        self.assertEqual(kind, "clarify")


class ClickWordsTests(unittest.TestCase):
    def test_click_with_an_odd_name_is_still_a_click(self):
        # Johnny's trial: Jev heard "click TV of Babel" as unclear (0.9) and nothing was clicked
        for cat in ("unclear", "chit_chat", "information_request"):
            ans = lambda _: {**PlannerTests.answers(None, "screen"), "category": (cat, 0.9)}
            kind, steps = planner.plan("click TV of Babel", ans)
            self.assertEqual((kind, steps[0]["action"]), ("steps", "screen.press"), cat)
        ans = lambda _: {**PlannerTests.answers(None, "screen"), "category": ("unclear", 0.9)}
        self.assertEqual(planner.plan("blorp the thing", ans), ("clarify", "unclear"))


class WhichOneLineTests(unittest.TestCase):
    def line(self, wheres):
        import siri
        choices = [{"name": f"Johnny ({w})", "where": w} for w in wheres]
        return siri.line_for({"state": "needs_clarification", "steps": [
            {"action": "screen.press", "state": "needs_clarification", "facts": {"choices": choices}}]})

    def test_places_when_each_is_somewhere_else(self):
        line = self.line(["top-left", "middle-centre", "bottom-right"])
        self.assertIn("I see 3: top left, middle centre or bottom right. Which one?", line)
        self.assertNotIn("Johnny", line)  # control text never goes to the voice service

    def test_ordinals_when_places_repeat(self):
        self.assertIn("I see 2. Say the first or the second.", self.line(["top-left", "top-left"]))


if __name__ == "__main__":
    unittest.main()
