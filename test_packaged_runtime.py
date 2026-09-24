"""The py2app bundle runs helpers through Contents/MacOS/python, a symlink that starts the base interpreter without the
app's venv. These tests run the real helpers through such a symlink, the way the live trial app does."""
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import actions
import engine
import url_adapter
from actions import Failed
from test_engine import fake_plan


def bundle_python():
    """A symlink to this venv's python in a folder with no pyvenv.cfg, exactly like the bundle's MacOS/python."""
    d = tempfile.mkdtemp()
    link = os.path.join(d, "python")
    os.symlink(sys.executable, link)
    return link


class PackagedHelperTests(unittest.TestCase):
    def setUp(self):
        self.py = bundle_python()

    def test_bare_bundle_python_really_lacks_appkit(self):
        """Guards the premise: without the fix's environment, the bundle interpreter cannot import AppKit."""
        import subprocess
        r = subprocess.run([self.py, "-c", "import AppKit"], capture_output=True, text=True,
                           env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"})
        self.assertNotEqual(r.returncode, 0)

    def test_quit_helper_runs_under_bundle_python(self):
        """Real helper, real Finder pid, deliberately wrong identity: it must start and answer 'mismatch'."""
        [(path, pid)] = actions.running("com.apple.finder", time.monotonic() + 5)
        with patch.object(actions.sys, "executable", self.py), \
             patch.object(actions, "running", return_value=[("/Applications/NotFinder.app", pid)]):
            with self.assertRaises(Failed) as cm:
                actions.run_app_quit({"bundle_id": "com.example.not-finder", "path": "/Applications/NotFinder.app"},
                                     time.monotonic() + 20)
        self.assertIn("mismatch", str(cm.exception))
        self.assertNotIn("could not start", str(cm.exception))

    def test_helper_that_cannot_start_is_failed_not_unknown(self):
        with patch.object(actions.sys, "executable", self.py), \
             patch.object(actions, "helper_env", return_value={k: v for k, v in os.environ.items() if k != "PYTHONPATH"}), \
             patch.object(actions, "running", return_value=[("/A.app", 1)]):
            with self.assertRaises(Failed) as cm:
                actions.run_app_quit({"bundle_id": "b", "path": "/A.app"}, time.monotonic() + 20)
        self.assertIn("could not start", str(cm.exception))

    def test_browser_lookup_runs_under_bundle_python(self):
        """url.open's pre-dispatch lookup must work in the bundle too; stop right after it, before any tab opens."""
        with patch.object(url_adapter.sys, "executable", self.py), \
             patch.object(actions.url_adapter, "run_url_open", side_effect=RuntimeError("stop")) as opened:
            with self.assertRaises(actions.Uncertain):
                actions.run_url({"url": "https://example.com/"}, time.monotonic() + 20)
        self.assertEqual(opened.call_count, 1)
        browser = opened.call_args.kwargs["_default_browser_fn"](1)
        self.assertRegex(browser, r"^[\w.-]+\.[\w.-]+$")


@patch.object(engine.planner, "plan", lambda text, classify, can_answer=False: ("answer", None))
class AnswerFailureTests(unittest.TestCase):
    def test_answer_error_is_its_own_failure(self):
        def boom(text):
            raise ValueError("Model returned no spoken answer.")
        eng = engine.Engine(classify=None, answer=boom)
        r = eng.wait(eng.submit("capital?", "voice")["id"], 5)
        self.assertEqual((r["state"], r["error"]), ("failed", "answer_failed"))
        self.assertIn("no spoken answer", r["detail"])
        import siri
        self.assertIn(siri.line_for(r), siri.REPLIES["answer_failed"])


class ReasoningBudgetTests(unittest.TestCase):
    def payload(self, params, supported):
        import model_settings as ms
        s = {"model": "m", "parameters": params, "metadata": {"supported_parameters": supported}}
        with patch.object(ms, "answer_settings", return_value=s):
            return ms.answer_payload([])

    def test_reasoning_model_gets_minimal_effort(self):
        self.assertEqual(self.payload({}, ["max_tokens", "reasoning"])["reasoning"], {"effort": "minimal"})

    def test_user_reasoning_setting_is_kept(self):
        p = self.payload({"reasoning_effort": "high"}, ["max_tokens", "reasoning", "reasoning_effort"])
        self.assertNotIn("reasoning", p)
        self.assertEqual(p["reasoning_effort"], "high")
        p = self.payload({"reasoning": {"effort": "low"}}, ["reasoning"])
        self.assertEqual(p["reasoning"], {"effort": "low"})

    def test_non_reasoning_model_untouched(self):
        self.assertNotIn("reasoning", self.payload({}, ["max_tokens"]))


if __name__ == "__main__":
    unittest.main()
