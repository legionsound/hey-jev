"""Settings for Claude Code / Codex answer sessions: working folder, adapter status, Save rules.
Built headless: no Settings window, folder picker or sheet appears on screen."""
import os
import tempfile
import unittest
from unittest.mock import patch

import assistant_ui
import model_settings
import secrets_store
import siri
from test_settings_closure import SAVES, Base


class FakePrefs:
    def __init__(self):
        self.d = {}

    def stringForKey_(self, k):
        return self.d.get(k)

    def setObject_forKey_(self, v, k):
        self.d[k] = v


class AgentPrefsTests(unittest.TestCase):
    def test_folder_defaults_to_home_and_saves_only_real_folders(self):
        with patch.object(model_settings, "PREFS", FakePrefs()), tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(model_settings.agent_settings("claude"), {"cwd": os.path.expanduser("~")})
            real = os.path.realpath(tmp)
            self.assertEqual(model_settings.save_agent_settings("codex", tmp), real)
            self.assertEqual(model_settings.agent_settings("codex"), {"cwd": real})
            self.assertEqual(model_settings.agent_settings("claude")["cwd"], os.path.expanduser("~"))  # per agent
            for bad in ("", os.path.join(tmp, "missing")):
                with self.assertRaises(ValueError):
                    model_settings.save_agent_settings("claude", bad)
            with self.assertRaises(ValueError):
                model_settings.save_agent_settings("gemini", tmp)
        with patch.object(model_settings, "PREFS", FakePrefs()) as prefs:
            prefs.d["agent_cwd_claude"] = "/no/such/folder"
            self.assertEqual(model_settings.agent_settings("claude")["cwd"], os.path.expanduser("~"))  # gone: home

    def test_agents_are_valid_answer_providers(self):
        for name in ("claude", "codex"):
            self.assertIn(name, secrets_store.SETTINGS["ANSWER_PROVIDER"])
        self.assertEqual(assistant_ui.ANSWER_PROVIDERS[-2:], ("claude", "codex"))


class AgentSettingsTests(Base):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.realpath(self.tmp.name)
        self.adapters = {"claude": "/opt/bin/claude-agent-acp", "codex": None}
        for p in (patch.object(assistant_ui, "show_settings_window", lambda sheet: None),
                  patch.object(assistant_ui, "agent_settings", lambda name: {"cwd": self.home}),
                  patch.object(assistant_ui, "find_adapter", lambda name: self.adapters[name])):
            p.start()
            self.addCleanup(p.stop)
        super().setUp()

    def tearDown(self):
        super().tearDown()
        self.tmp.cleanup()

    def pick(self, name):
        self.d.answer_provider.selectItemAtIndex_(assistant_ui.ANSWER_PROVIDERS.index(name))
        self.d.answerSourceChanged_(None)

    def save(self):
        patches = {n: patch.object(assistant_ui, n) for n in SAVES + ("save_agent_settings",)}
        mocks = {n: p.start() for n, p in patches.items()}
        for p in patches.values():
            self.addCleanup(p.stop)
        with patch.object(assistant_ui, "get_secret", return_value="stored"), \
                patch.object(assistant_ui, "wake_settings", return_value=("Hey Jev", [])), \
                patch.object(siri, "reload_keys"), \
                patch.dict(siri.STT, {"backend": "whisper", "blocked": None, "switching": False}), \
                patch.object(assistant_ui.AppDelegate, "_start_worker"):
            self.d.saveSettings_(None)
        return mocks

    def test_popup_titles_and_rows_follow_the_selected_agent(self):
        titles = [self.d.answer_provider.itemTitleAtIndex_(i) for i in range(self.d.answer_provider.numberOfItems())]
        self.assertEqual(titles[-2:], ["Claude Code", "Codex"])
        self.pick("openrouter")
        self.assertEqual((self.d.agent_folder.stringValue(), self.d.agent_note.stringValue()), ("", ""))
        self.assertFalse(self.d.agent_choose.isEnabled())
        self.pick("claude")
        self.assertEqual(self.d.agent_folder.stringValue(), self.home)
        self.assertIn("Full Claude Code session", self.d.agent_note.stringValue())
        self.assertTrue(self.d.agent_choose.isEnabled())
        self.pick("codex")
        self.assertIn("not found", self.d.agent_note.stringValue())

    def test_missing_adapter_blocks_save_for_that_provider(self):
        self.pick("codex")
        mocks = self.save()
        self.assertIn("Codex can't answer yet", self.d.settings_message.stringValue())
        mocks["save_secret"].assert_not_called()
        mocks["save_agent_settings"].assert_not_called()

    def test_chosen_folder_is_saved_for_that_agent_only(self):
        sub = os.path.join(self.home, "work")
        os.mkdir(sub)
        self.pick("claude")
        with patch.object(assistant_ui, "choose_folder", lambda prompt: sub):
            self.d.chooseAgentFolder_(None)
        self.assertEqual(self.d.agent_folder.stringValue(), sub)
        mocks = self.save()
        mocks["save_agent_settings"].assert_called_once_with("claude", sub)
        mocks["save_secret"].assert_any_call("ANSWER_PROVIDER", "claude")

    def test_unchanged_folders_are_not_rewritten_and_cancel_keeps_the_draft(self):
        self.pick("claude")
        with patch.object(assistant_ui, "choose_folder", lambda prompt: None):  # picker cancelled
            self.d.chooseAgentFolder_(None)
        self.assertEqual(self.d.agent_folder.stringValue(), self.home)
        self.save()["save_agent_settings"].assert_not_called()

    def test_a_folder_that_vanished_before_save_stops_the_save(self):
        sub = os.path.join(self.home, "gone")
        os.mkdir(sub)
        self.pick("claude")
        with patch.object(assistant_ui, "choose_folder", lambda prompt: sub):
            self.d.chooseAgentFolder_(None)
        os.rmdir(sub)
        mocks = self.save()
        self.assertIn("isn't a folder", self.d.settings_message.stringValue())
        mocks["save_secret"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
