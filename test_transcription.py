"""Backend choice: Apple is used only when ready; otherwise listening is blocked with a reason, never a fallback."""
import sys
import types
import unittest
from unittest.mock import patch

import model_settings
import siri


def fake_speech(state, reason=""):
    mod = types.ModuleType("speech_apple")
    mod.status = lambda locale="en-US": (state, reason)
    mod.AppleTranscriber = lambda locale: types.SimpleNamespace(transcribe=lambda audio, prompt=None: ("hi", 5))
    return mod


class BackendTests(unittest.TestCase):
    def test_apple_ready_is_used(self):
        with patch.dict(sys.modules, {"speech_apple": fake_speech("ready")}):
            fn, blocked = siri.load_transcriber("apple", None)
        self.assertIsNone(blocked)
        self.assertEqual(fn(None, None), ("hi", 5))

    def test_apple_not_ready_blocks_without_loading_whisper(self):
        with patch.dict(sys.modules, {"speech_apple": fake_speech("denied", "Permission denied."),
                                      "faster_whisper": None}):
            fn, blocked = siri.load_transcriber("apple", None)
        self.assertIsNone(fn)
        self.assertIn("Permission denied.", blocked)

    def test_apple_missing_bindings_blocks(self):
        with patch.dict(sys.modules, {"speech_apple": None}):
            fn, blocked = siri.load_transcriber("apple", None)
        self.assertIsNone(fn)
        self.assertIn("isn't installed", blocked)

    def test_backend_pref_validates(self):
        saved = model_settings.PREFS.stringForKey_("transcription_backend")
        try:
            with self.assertRaises(ValueError):
                model_settings.save_transcription_backend("cloud")
            model_settings.save_transcription_backend("apple")
            self.assertEqual(model_settings.transcription_backend(), "apple")
            model_settings.PREFS.setObject_forKey_("bogus", "transcription_backend")
            self.assertEqual(model_settings.transcription_backend(), "whisper")
        finally:
            if saved:
                model_settings.PREFS.setObject_forKey_(saved, "transcription_backend")
            else:
                model_settings.PREFS.removeObjectForKey_("transcription_backend")


if __name__ == "__main__":
    unittest.main()
