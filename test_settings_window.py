"""The real Settings window, built headless: the close button must discard exactly like Cancel."""
import unittest
from unittest.mock import patch

import assistant_ui


class SettingsCloseTests(unittest.TestCase):
    def make(self):
        d = assistant_ui.AppDelegate.alloc().init()
        d.catalog, d.fetch_generation, d.worker_started, d.controls = [], 0, False, None
        return d

    def test_close_button_discards_like_cancel(self):
        with patch.object(assistant_ui.AppDelegate, "refreshModels_", lambda self, s: None):
            d = self.make()
            d._show_settings()
            first, gen = d.settings_sheet, d.fetch_generation
            d.key_fields["FISH_AUDIO_API_KEY"].setStringValue_("draft-not-saved")
            first.performClose_(None)  # the red close button
            self.assertIsNone(d.settings_sheet)
            self.assertEqual(d.fetch_generation, gen + 1)  # a fetch in flight is now stale
            d.modelsLoaded_({"generation": gen, "models": []})  # lands late: ignored, no crash
            d._show_settings()
            self.assertIsNot(d.settings_sheet, first)
            self.assertEqual(d.key_fields["FISH_AUDIO_API_KEY"].stringValue(), "")  # the draft is gone
            d.closeSettings_(None)
            self.assertIsNone(d.settings_sheet)
            self.assertEqual(d.fetch_generation, gen + 2)  # Cancel discards once, not twice

    def test_mic_test_names_the_phrase_it_tested(self):
        with patch.object(assistant_ui.AppDelegate, "refreshModels_", lambda self, s: None):
            d = self.make()
            d._show_settings()
            d.wake_field.setStringValue_("Okay Zorblat")
            d.micTestDone_({"text": "Okay Zorblat open Notes", "ms": 90, "wake": "Okay Zorblat", "wake_matched": True})
            self.assertEqual(d.test_result.stringValue(), "“Okay Zorblat open Notes” · heard “Okay Zorblat” · 90 ms")
            d.wake_field.setStringValue_("Hey José")  # typed, not saved: the test still used the running phrase
            d.micTestDone_({"text": "Hey José open Notes", "ms": 90, "wake": "Okay Zorblat", "wake_matched": False})
            self.assertEqual(d.test_result.stringValue(),
                             "“Hey José open Notes” · didn't hear “Okay Zorblat” · 90 ms · Save to test the new phrase")
            d.closeSettings_(None)


if __name__ == "__main__":
    unittest.main()
