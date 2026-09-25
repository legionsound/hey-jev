"""Duplicate-app tie-break: Jev may pick only at or above the threshold; below it, and for any error, choices stay explicit."""
import time
import unittest
from unittest.mock import MagicMock, patch

import engine
from test_engine import Calls, act, fake_plan

A = {"name": "Live", "bundle_id": "com.ableton.live", "path": "/Applications/Live.app"}
B = {"name": "Live", "bundle_id": "com.ableton.live", "path": "/Applications/Old/Live.app"}


@patch.object(engine.planner, "plan", fake_plan)
class TiebreakEngineTests(unittest.TestCase):
    def setUp(self):
        self.c, self.shown = Calls(), []

    def eng(self, pick, effect="open", policy=None, threshold=0.85, ask=True):
        a = act(self.c, "x", effect=effect, resolve=lambda args: ("choices", [dict(A), dict(B)]))
        return engine.Engine(classify=None, policy=lambda: policy or {"open": "auto", "quit": "auto"},
                             ask=self.shown.append if ask else None, actions={"x": a},
                             tiebreak=pick, threshold=lambda: threshold)

    def run_one(self, eng):
        return eng.wait(eng.submit("x", "cli")["id"], 5)

    def test_pick_at_or_above_threshold_runs_that_install(self):
        r = self.run_one(self.eng(lambda clause, ch: (1, 0.85)))
        self.assertEqual(r["state"], "completed")
        self.assertEqual(r["steps"][0]["target"]["path"], B["path"])
        self.assertEqual(r["steps"][0]["facts"]["seen"], "x")
        self.assertEqual(self.c.runs, ["x"])

    def test_below_threshold_keeps_choices(self):
        r = self.run_one(self.eng(lambda clause, ch: (1, 0.84)))
        self.assertEqual(r["state"], "needs_clarification")
        self.assertEqual(len(r["steps"][0]["facts"]["choices"]), 2)
        self.assertEqual(r["steps"][0]["facts"]["tiebreak"], {"score": 0.84, "threshold": 0.85})
        self.assertEqual(self.c.runs, [])

    def test_errors_and_bad_picks_keep_choices(self):
        for pick in (lambda c, ch: 1 / 0, lambda c, ch: None, lambda c, ch: (5, 0.99), lambda c, ch: (-1, 0.99)):
            self.assertEqual(self.run_one(self.eng(pick))["state"], "needs_clarification")
        self.assertEqual(self.run_one(self.eng(None))["state"], "needs_clarification")
        self.assertEqual(self.c.runs, [])

    def test_malformed_picks_keep_choices_and_dispatch_nothing(self):
        nan, inf = float("nan"), float("inf")
        bad = [(0, nan), (0, inf), (0, -inf), (0, 1.1), (0, -0.1), (True, 0.99), (False, 0.99), (0, True),
               (0, "0.99"), (0.0, 0.99), ("0", 0.99), (0,), (0, 0.99, 1), [0, 0.99], "app_0", 0.99, {"i": 0}]
        for got in bad:
            r = self.run_one(self.eng(lambda c, ch, got=got: got))
            self.assertEqual(r["state"], "needs_clarification", got)
            self.assertIn("error", r["steps"][0]["facts"]["tiebreak"], got)
            self.assertEqual(len(r["steps"][0]["facts"]["choices"]), 2)
        self.assertEqual(self.c.runs, [])

    def test_always_ask_never_calls_the_model(self):
        for need in (float("inf"), float("nan"), 1.5, -0.1):
            pick = MagicMock(return_value=(0, 1.0))
            r = self.run_one(self.eng(pick, threshold=need))
            self.assertEqual(r["state"], "needs_clarification", need)
            pick.assert_not_called()
        self.assertEqual(self.c.runs, [])

    def test_always_ask_setting_never_picks(self):
        r = self.run_one(self.eng(lambda c, ch: (0, 1.0), threshold=float("inf")))
        self.assertEqual((r["state"], self.c.runs), ("needs_clarification", []))

    def test_picked_quit_always_asks_even_when_automatic(self):
        eng = self.eng(lambda c, ch: (0, 0.99), effect="quit", policy={"quit": "auto"})
        rid = eng.submit("x", "cli")["id"]
        for _ in range(100):
            if self.shown and self.shown[-1]:
                break
            time.sleep(0.02)
        self.assertIsNotNone(self.shown[-1], "no pop-down for a picked quit")
        self.assertEqual(self.c.runs, [])
        eng.decide(self.shown[-1]["token"], True)
        r = eng.wait(rid, 5)
        self.assertEqual((r["state"], r["steps"][0]["target"]["path"]), ("completed", A["path"]))

    def test_picked_quit_without_ui_declines(self):
        r = self.run_one(self.eng(lambda c, ch: (0, 0.99), effect="quit", policy={"quit": "auto"}, ask=False))
        self.assertEqual((r["state"], r["steps"][0]["detail"]), ("declined", "no_confirmation_ui"))
        self.assertEqual(self.c.runs, [])

    def test_picked_target_gone_after_confirm_fails(self):
        seq = iter([("choices", [dict(A), dict(B)]), ("choices", [dict(B), dict(B, path="/x/Live.app")])])
        a = act(self.c, "x", effect="quit", resolve=lambda args: next(seq))
        shown = []
        eng = engine.Engine(classify=None, policy=lambda: {"quit": "ask"}, ask=shown.append, actions={"x": a},
                            tiebreak=lambda c, ch: (0, 0.99), threshold=lambda: 0.85)
        rid = eng.submit("x", "cli")["id"]
        for _ in range(100):
            if shown and shown[-1]:
                break
            time.sleep(0.02)
        eng.decide(shown[-1]["token"], True)
        r = eng.wait(rid, 5)
        self.assertEqual((r["state"], r["steps"][0]["detail"]), ("failed", "target_changed"))
        self.assertEqual(self.c.runs, [])


class TiebreakJevTests(unittest.TestCase):
    def setUp(self):
        import siri
        self.siri = siri

    def test_question_carries_hints_and_maps_back(self):
        hints = [{"name": "Live", "folder": "/Applications", "running": True, "last_opened": "2026-09-20"},
                 {"name": "Live", "folder": "/Applications/Old", "running": False, "last_opened": None}]
        with patch("actions.app_hints", return_value=hints), \
             patch.object(self.siri, "jev", return_value=({"pick": ("app_1", 0.9)}, 5, 0.0)) as jev:
            self.assertEqual(self.siri.tiebreak("open live", [A, B]), (1, 0.9))
        crit = jev.call_args[0][1]["pick"]["criteria"]
        self.assertEqual(crit["app_0"], "Live in /Applications, running right now, last opened 2026-09-20")
        self.assertEqual(crit["app_1"], "Live in /Applications/Old")

    def test_too_many_or_too_few_choices_skip_jev(self):
        with patch.object(self.siri, "jev") as jev:
            self.assertIsNone(self.siri.tiebreak("x", [A]))
            self.assertIsNone(self.siri.tiebreak("x", [A] * 7))
            jev.assert_not_called()

    def test_engine_threshold_follows_setting(self):
        with patch.object(self.siri, "tiebreak_threshold", return_value=85):
            import actions
            for name in ("CHOOSE", "CLASSIFY_ITEMS", "DESKTOP", "VISIBLE"):  # make_engine wires the app's globals: put them back
                self.addCleanup(setattr, actions, name, getattr(actions, name))
            eng = self.siri.make_engine()
            self.assertEqual(eng.threshold(), 0.85)
        with patch.object(self.siri, "tiebreak_threshold", return_value=100):
            self.assertEqual(eng.threshold(), float("inf"))
        eng.shutdown()


class ThresholdSettingTests(unittest.TestCase):
    def test_default_and_clamp(self):
        import model_settings as ms
        prefs = MagicMock()
        with patch.object(ms, "PREFS", prefs):
            prefs.objectForKey_.return_value = None
            self.assertEqual(ms.tiebreak_threshold(), 85)
            prefs.objectForKey_.return_value = 1
            for saved, want in ((20, 50), (140, 100), (70, 70)):
                prefs.integerForKey_.return_value = saved
                self.assertEqual(ms.tiebreak_threshold(), want)
            ms.save_tiebreak_threshold(84.6)
            prefs.setInteger_forKey_.assert_called_with(85, "tiebreak_threshold")


if __name__ == "__main__":
    unittest.main()
