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


if __name__ == "__main__":
    unittest.main()
