"""Offline checks for provider routing and role-specific credentials."""
import unittest
from unittest.mock import patch

import planner
import secrets_store
import siri


class ProviderTests(unittest.TestCase):
    def test_required_keys_follow_selected_roles(self):
        with patch.object(secrets_store, "get_secret", return_value=None):
            self.assertEqual(
                secrets_store.missing_secrets("openrouter", "disabled"),
                ["FISH_AUDIO_API_KEY", "JEV_OPENROUTER_API_KEY"],
            )
            self.assertEqual(
                secrets_store.missing_secrets("typesafe", "openrouter"),
                ["FISH_AUDIO_API_KEY", "TYPESAFE_API_KEY", "OPENROUTER_API_KEY"],
            )

    def test_jev_uses_selected_endpoint_and_key(self):
        response = {
            "answers": {"intent": {"type": "choice", "choice": "hello", "confidence": 0.9}},
            "usage": {"input_tokens": 100},
        }
        with patch.object(siri.requests, "post") as post:
            post.return_value.json.return_value = response
            for provider, url, model, key in (
                ("openrouter", "https://openrouter.ai/api/alpha/decisions", "typesafe/jev-1.13", "or-test"),
                ("typesafe", "https://api.typesafe.ai/v1/systemone", "jev-latest", "ts-test"),
            ):
                with self.subTest(provider=provider), patch.object(siri, "JEV_PROVIDER", provider), \
                        patch.object(siri, "JEV_OR_KEY", "or-test"), patch.object(siri, "TS_KEY", "ts-test"):
                    answers, _, _ = siri.jev("hello", {"intent": {"type": "choice"}})
                    self.assertEqual(answers["intent"], ("hello", 0.9))
                    args, kwargs = post.call_args
                    self.assertEqual(args[0], url)
                    self.assertEqual(kwargs["json"]["model"], model)
                    self.assertEqual(kwargs["headers"]["Authorization"], f"Bearer {key}")

    def test_disabled_answers_use_scripted_reply(self):
        ans = {"category": ("information_request", 0.9), "target": ("app", 0), "compound": (False, 0.9)}
        self.assertEqual(planner.plan("what is the capital of France", lambda _: ans, can_answer=False), ("reply", "info"))
        self.assertEqual(planner.plan("what is the capital of France", lambda _: ans, can_answer=True), ("answer", None))

if __name__ == "__main__":
    unittest.main()
