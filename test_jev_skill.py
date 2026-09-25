"""Hey Jev follows the jev skill (~/.agents/skills/jev): no decision on failure, validated answers, local limits,
no cross-question confidence comparison, no silent truncation, keys never in argv."""
import unittest
from unittest.mock import patch

import actions
import planner
import secrets_store
import siri

Q = {"pick": {"type": "choice", "instructions": "which", "criteria": {"a": "first", "b": "second"}}}
GOOD = {"answers": {"pick": {"type": "choice", "choice": "a", "confidence": 0.8,
                             "probabilities": {"a": 0.8, "b": 0.2}}}, "usage": {"input_tokens": 1}}


def call(response, questions=Q, key="k"):
    with patch.object(siri.requests, "post") as post, patch.object(siri, "JEV_PROVIDER", "openrouter"), \
            patch.object(siri, "JEV_OR_KEY", key):
        post.return_value.json.return_value = response
        return siri.jev("hi", questions), post


class ClientTests(unittest.TestCase):
    def test_valid_answer_passes(self):
        (ans, _, _), _ = call(GOOD)
        self.assertEqual(ans["pick"], ("a", 0.8))

    def test_malformed_answers_are_no_decision(self):
        bad = [{"answers": {}},
               {"answers": {"pick": {"type": "noul", "noul": 0.9}}},
               {"answers": {"pick": {"type": "choice", "choice": "zzz", "confidence": 0.8,
                                     "probabilities": {"a": 0.8, "b": 0.2}}}},
               {"answers": {"pick": {"type": "choice", "choice": "b", "confidence": 0.8,
                                     "probabilities": {"a": 0.8, "b": 0.2}}}},
               {"answers": {"pick": {"type": "choice", "choice": "a", "confidence": 0.8,
                                     "probabilities": {"a": 0.8, "b": 0.8}}}},
               {"answers": {"pick": {"type": "choice", "choice": "a", "probabilities": {"a": 0.8, "b": 0.2}}}}]
        for r in bad:
            with self.subTest(r=r), self.assertRaises(siri.JevError):
                call(r)

    def test_missing_key_never_reaches_network(self):
        with self.assertRaises(siri.JevError), patch.object(siri.requests, "post") as post, \
                patch.object(siri, "JEV_PROVIDER", "openrouter"), patch.object(siri, "JEV_OR_KEY", None):
            siri.jev("hi", Q)
        post.assert_not_called()

    def test_oversize_request_refused_not_truncated(self):
        with self.assertRaises(siri.JevError), patch.object(siri.requests, "post") as post, \
                patch.object(siri, "JEV_PROVIDER", "openrouter"), patch.object(siri, "JEV_OR_KEY", "k"):
            siri.jev("x" * 30000, Q)
        post.assert_not_called()
        many = {f"q{i}": {"type": "noul", "instructions": "yes?"} for i in range(129)}
        with self.assertRaises(siri.JevError):
            call(GOOD, many)

    def test_noul_carries_probability_of_its_value(self):
        q = {"y": {"type": "noul", "instructions": "yes?"}}
        (ans, _, _), _ = call({"answers": {"y": {"type": "noul", "noul": 0.2}}, "usage": {}}, q)
        self.assertEqual(ans["y"][0], False)
        self.assertAlmostEqual(ans["y"][1], 0.8)

    def test_native_model_is_pinned(self):
        import model_settings
        self.assertEqual(model_settings.JEV_MODELS["typesafe"], "jev-1.13.0")


class FailureIsNotNoTests(unittest.TestCase):
    def test_choose_control_failure_raises(self):
        with patch.object(siri, "jev", side_effect=siri.requests.ConnectionError("down")), \
                patch.object(siri.diagnostics, "record"):
            with self.assertRaises(siri.requests.ConnectionError):
                siri.choose_control("play", ["Play", "Pause"])

    def test_tiebreak_none_is_no_pick(self):
        with patch.object(siri, "jev", return_value=({"pick": ("none", 0.9)}, 1, 0)), \
                patch("actions.app_hints", return_value=[{"name": "A", "folder": "/x", "running": False,
                                                          "last_opened": None}] * 2):
            self.assertIsNone(siri.tiebreak("open a", ["/x/A.app", "/y/A.app"]))

    def test_tiebreak_offers_none(self):
        with patch.object(siri, "jev", return_value=({"pick": ("app_1", 0.9)}, 1, 0)) as j, \
                patch("actions.app_hints", return_value=[{"name": "A", "folder": "/x", "running": False,
                                                          "last_opened": None}] * 2):
            self.assertEqual(siri.tiebreak("open a", ["/x/A.app", "/y/A.app"]), (1, 0.9))
        self.assertIn("none", j.call_args[0][1]["pick"]["criteria"])


class PlannerTests(unittest.TestCase):
    def test_unsure_target_with_two_readings_does_not_guess(self):
        readings = {"app": (0.9, "app.open", {"app": "Loud"}), "volume": (0.7, "volume.up", {})}
        with patch.object(planner, "step_for", side_effect=lambda a, t, c, b=None: readings.get(t)):
            self.assertIsNone(planner.pick({"target": ("app", 0.3)}, "make it loud"))

    def test_unsure_target_with_one_reading_acts(self):
        readings = {"volume": (0.7, "volume.up", {})}
        with patch.object(planner, "step_for", side_effect=lambda a, t, c, b=None: readings.get(t)):
            self.assertEqual(planner.pick({"target": ("app", 0.3)}, "louder")[1], "volume.up")


class QualifiedPickTests(unittest.TestCase):
    def test_words(self):
        cases = {"Click on the full tilt video": {"noun": "video", "kind": "full tilt", "ordinal": 0},
                 "play the Daft Punk song": {"noun": "song", "kind": "Daft Punk", "ordinal": 0},
                 "open the top result": {"noun": "result", "ordinal": 1}}
        for said, want in cases.items():
            self.assertEqual(planner.pick_args(said), want, said)
        for said in ["click the save button", "click the Settings tab", "click the video"]:
            self.assertIsNone(planner.pick_args(said), said)


class TaskListTests(unittest.TestCase):
    def test_a_cut_list_says_so(self):
        import json, task
        from types import SimpleNamespace as NS
        snap = NS(items=[NS(source="ax", label=f"b{k}", role="AXButton", frame=(0, k, 10, 10), secure=False)
                         for k in range(70)], walk_complete=True, text_frames=[], field_frames=[], app="X",
                  window_frame=(0, 0, 100, 100))
        with patch.object(task, "_pressable", lambda i: True), patch.object(task, "_typeable", lambda i: False):
            items, _ = task.shareable(snap)
            state = json.loads(task.state_text("g", snap, items, [], []))
        self.assertEqual(len(state["screen_items_in_reading_order"]), task.MAX_ITEMS)
        self.assertEqual(state["items_not_shown"], 70 - task.MAX_ITEMS)


class SecretTests(unittest.TestCase):
    def test_key_value_never_in_argv(self):
        with patch.object(secrets_store.subprocess, "run") as run:
            run.return_value.returncode = 0
            secrets_store.save_secret("OPENROUTER_API_KEY", "sk-secret-value")
        argv = run.call_args[0][0]
        self.assertNotIn("sk-secret-value", " ".join(argv))
        self.assertIn("sk-secret-value", run.call_args[1]["input"])

    def test_keychain_beats_environment(self):
        with patch.object(secrets_store, "keychain_value", return_value="from-keychain"), \
                patch.dict("os.environ", {"OPENROUTER_API_KEY": "from-env"}):
            self.assertEqual(secrets_store.get_secret("OPENROUTER_API_KEY"), "from-keychain")

    def test_quote_in_key_refused(self):
        with self.assertRaises(ValueError):
            secrets_store.save_secret("OPENROUTER_API_KEY", 'a"b')



class AuditRound2Tests(unittest.TestCase):
    """Astra's 2026-09-25 audit: singleton selectors and scores that disagree with their distribution."""
    S = {"s": {"type": "score", "instructions": "how", "criteria": ["low", "mid", "high", "higher", "top"]}}

    def score(self, value, probs):
        return {"answers": {"s": {"type": "score", "score": value, "confidence": 0.9, "probabilities": probs,
                                  "legend": {str(i): "x" for i in range(5)}}}}

    def test_a_score_must_be_its_distributions_expectation(self):
        with self.assertRaises(siri.JevError):
            call(self.score(4, {"0": 1, "1": 0, "2": 0, "3": 0, "4": 0}), self.S)
        (ans, _, _), _ = call(self.score(1.5, {"0": 0, "1": .5, "2": .5, "3": 0, "4": 0}), self.S)
        self.assertIn("s", ans)  # accepted

    def test_a_single_candidate_is_never_asked_as_a_choice(self):
        import task
        one = {"i0": object()}
        q = task.questions(["press_item", "type_text", "open_app"], one, fields=one, openable={"a0": {"name": "Mail"}})
        self.assertEqual(set(q), {"kind"})
        siri.validate_jev_request({"state": "x", "questions": q})  # the batch is sendable

    def test_decide_takes_the_only_candidate_at_the_kinds_confidence(self):
        import task
        only = object()
        with patch.object(task, "offer", lambda *a: (["press_item", "done"], {"i0": only}, {}, {})), \
                patch.object(task, "state_text", lambda *a: "s"):
            got = task.decide(lambda s, q: {"kind": ("press_item", 0.8)}, "goal", None, [], [], [], None)
        self.assertEqual(got, ("press_item", 0.8, only))


    def test_an_unsure_volume_level_asks_instead_of_setting(self):
        ans = {"target": ("volume", .99), "volume_action": ("set", .99), "volume_scope": ("system", .99),
               "volume_level": ("loud", .10)}
        self.assertEqual(planner.step_for(ans, "volume", "set volume loud")[1:], ("clarify", "unsure_level"))
        ans["volume_level"] = ("loud", .9)
        self.assertEqual(planner.step_for(ans, "volume", "set volume loud")[1:], ("volume.set", {"level": "loud"}))

    def test_score_labels_come_from_our_rubric_not_the_returned_legend(self):
        r = {"answers": {"s": {"type": "score", "score": 1, "confidence": 0.9,
                               "probabilities": {"0": 0, "1": 1, "2": 0, "3": 0, "4": 0},
                               "legend": {str(i): "EVIL" for i in range(5)}}}}
        (ans, _, _), _ = call(r, self.S)
        self.assertEqual(ans["s"][0], "mid")


class PickAuditTests(unittest.TestCase):
    def items(self, n):
        from types import SimpleNamespace as NS
        return [NS(source="ax", pressable=True, enabled=True, from_value=False, role="AXLink", label=f"Video {i}",
                   frame=(0, i * 40, 100, 15), token=f"e{i}", secure=False) for i in range(n)]

    def pick(self, args, flags, truncated=False, n=3):
        from types import SimpleNamespace as NS
        snap = NS(items=self.items(n), app="Browser", window_frame=(0, 0, 500, 500), pid=1, started=1,
                  window_token="w", truncated=truncated, field_frames=[], text_frames=[],
                  walk_complete=True)
        with patch.object(actions.screen, "observe", return_value=snap), \
                patch.object(actions, "CLASSIFY_ITEMS", return_value=flags), \
                patch.object(actions, "_live", lambda i: True):
            return actions.resolve_screen_pick(args)

    def test_a_cut_screen_never_picks(self):
        self.assertEqual(self.pick({"noun": "video", "ordinal": -1}, [(True, .99)] * 3, truncated=True)[0], "none")

    def test_an_unsure_item_before_the_ordinal_asks(self):
        got = self.pick({"noun": "video", "ordinal": 1}, [(True, .51), (True, .99), (True, .99)])
        self.assertEqual(got[0], "choices")

    def test_an_unsure_item_after_the_last_asks(self):
        got = self.pick({"noun": "video", "ordinal": -1}, [(True, .99), (True, .99), (False, .55)])
        self.assertEqual(got[0], "choices")

    def test_an_unsure_item_makes_only_one_ask(self):
        got = self.pick({"noun": "video", "ordinal": 0}, [(True, .99), (False, .55), (False, .99)])
        self.assertEqual(got[0], "choices")

    def test_ordering_is_one_full_pool_order_never_a_regrouped_subset(self):
        from types import SimpleNamespace as NS
        mk = lambda n, f: NS(source="ax", pressable=True, enabled=True, from_value=False, role="AXLink", label=n,
                             frame=f, token=n, secure=False)
        items = [mk("A", (0, 0, 50, 100)), mk("B", (300, 40, 50, 10)), mk("C", (400, 20, 50, 10))]
        snap = NS(items=items, app="Browser", window_frame=(0, 0, 500, 500), pid=1, started=1, window_token="w",
                  truncated=False, field_frames=[], text_frames=[], walk_complete=True)
        with patch.object(actions.screen, "observe", return_value=snap), patch.object(actions, "_live", lambda i: True), \
                patch.object(actions, "CLASSIFY_ITEMS", return_value=[(True, .99), (True, .99), (False, .55)]):
            self.assertEqual(actions.resolve_screen_pick({"noun": "video", "ordinal": -1})[0], "choices")

    def test_sure_answers_still_pick(self):
        got = self.pick({"noun": "video", "ordinal": 2}, [(True, .99), (False, .99), (True, .9)])
        self.assertEqual((got[0], got[1]["label"]), ("target", "Video 2"))


if __name__ == "__main__":
    unittest.main()
