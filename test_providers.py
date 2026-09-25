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
            "answers": {"intent": {"type": "choice", "choice": "hello", "confidence": 0.9,
                                  "probabilities": {"hello": 0.9, "x": 0.1}}},
            "usage": {"input_tokens": 100},
        }
        with patch.object(siri.requests, "post") as post:
            post.return_value.json.return_value = response
            for provider, url, model, key in (
                ("openrouter", "https://openrouter.ai/api/alpha/decisions", "typesafe/jev-1.13", "or-test"),
                ("typesafe", "https://api.typesafe.ai/v1/systemone", "jev-1.13.0", "ts-test"),
            ):
                with self.subTest(provider=provider), patch.object(siri, "JEV_PROVIDER", provider), \
                        patch.object(siri, "JEV_OR_KEY", "or-test"), patch.object(siri, "TS_KEY", "ts-test"):
                    answers, _, _ = siri.jev("hello", {"intent": {"type": "choice", "instructions": "which", "criteria": {"hello": "hi", "x": "other"}}})
                    self.assertEqual(answers["intent"], ("hello", 0.9))
                    args, kwargs = post.call_args
                    self.assertEqual(args[0], url)
                    self.assertEqual(kwargs["json"]["model"], model)
                    self.assertEqual(kwargs["headers"]["Authorization"], f"Bearer {key}")
                    self.assertIs(kwargs.get("allow_redirects"), False)

    def test_key_requests_disable_redirects(self):
        with patch.object(siri.requests, "post") as post:
            post.return_value.json.return_value = {
                "answers": {"intent": {"type": "choice", "choice": "x", "confidence": 0.9,
                                             "probabilities": {"hello": 0.1, "x": 0.9}}},
                "usage": {"input_tokens": 0}}
            with patch.object(siri, "JEV_PROVIDER", "typesafe"), \
                    patch.object(siri, "TS_KEY", "ts-test"):
                siri.jev("hello", {"intent": {"type": "choice", "instructions": "which", "criteria": {"hello": "hi", "x": "other"}}})
            _, kwargs = post.call_args
            self.assertIs(kwargs.get("allow_redirects"), False)
        with patch.object(siri.requests, "post") as post:
            post.return_value.content = b"RIFF"
            with patch.object(siri, "FISH_KEY", "fish-test"), \
                    patch("os.path.exists", return_value=False), \
                    patch("os.makedirs"), \
                    patch("builtins.open", create=True):
                siri.fetch_tts("hi")
            _, kwargs = post.call_args
            self.assertIs(kwargs.get("allow_redirects"), False)

    def test_disabled_answers_use_scripted_reply(self):
        ans = {"category": ("information_request", 0.9), "target": ("app", 0), "compound": (False, 0.9)}
        self.assertEqual(planner.plan("what is the capital of France", lambda _: ans, can_answer=False), ("reply", "info"))
        self.assertEqual(planner.plan("what is the capital of France", lambda _: ans, can_answer=True), ("answer", None))

class AnswerClockTests(unittest.TestCase):
    def test_answers_know_the_local_time(self):
        import datetime
        import siri
        sent = {}

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": "It's 7:35."}}], "usage": {}}

        def post(url, headers=None, json=None, timeout=None, allow_redirects=None):
            sent.update(json)
            return R()
        with patch.object(siri.requests, "post", post), patch.object(siri, "answer_settings", lambda: {"model": "m"}):
            siri.ask_llm("what time is it")
        system = sent["messages"][0]["content"]
        now = datetime.datetime.now().astimezone()
        self.assertIn(now.strftime("%A"), system)
        self.assertIn(now.strftime("%Y"), system)


if __name__ == "__main__":
    unittest.main()
