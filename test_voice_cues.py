"""Voice cues: the user decides which performance tags the Fish voice gets: all, some, or none."""
import json
import unittest
from unittest.mock import patch

import voice_output


class FakePrefs:
    def __init__(self):
        self.d = {}

    def stringForKey_(self, k):
        return self.d.get(k)

    def setObject_forKey_(self, v, k):
        self.d[k] = v


class CueTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(voice_output, "PREFS", FakePrefs())
        self.prefs = p.start()
        self.addCleanup(p.stop)

    def test_default_is_the_lines_as_written(self):
        self.assertEqual(voice_output.cues("fish")["mode"], "all")
        self.assertEqual(voice_output.with_cues("[chuckling] There you go, Notes."), "[chuckling] There you go, Notes.")

    def test_none_removes_every_cue(self):
        voice_output.set_cues("fish", "none")
        self.assertEqual(voice_output.with_cues("[chuckling] There you go, Notes."), "There you go, Notes.")
        self.assertEqual(voice_output.with_cues("[clear throat] Sorry, say that again?"), "Sorry, say that again?")
        self.assertEqual(voice_output.with_cues("[whispering] psst"), "psst")  # a cue we don't list is still a cue

    def test_some_keeps_only_the_chosen_cues(self):
        voice_output.set_cues("fish", "some", ["cheerful", "not a cue"])
        self.assertEqual(voice_output.cues("fish"), {"mode": "some", "on": ["cheerful"]})
        self.assertEqual(voice_output.with_cues("[cheerful] Turning it up."), "[cheerful] Turning it up.")
        self.assertEqual(voice_output.with_cues("[sighing] A little quieter."), "A little quieter.")

    def test_each_provider_keeps_its_own_choice_and_bad_prefs_fall_back(self):
        self.prefs.d["voice_cues"] = json.dumps({"other": {"mode": "none"}})
        voice_output.set_cues("fish", "none")
        self.assertEqual(json.loads(self.prefs.d["voice_cues"])["other"], {"mode": "none"})
        self.prefs.d["voice_cues"] = "not json"
        self.assertEqual(voice_output.cues("fish")["mode"], "all")
        with self.assertRaises(ValueError):
            voice_output.set_cues("fish", "loud")

    def test_the_voice_and_the_answer_model_both_follow_the_setting(self):
        import siri
        voice_output.set_cues("fish", "none")
        self.assertIn("Never use bracketed tags", siri.cue_rule())
        sent = []
        with patch.object(siri, "fetch_tts", lambda t, *a, **k: sent.append(t) or ("/x.wav", 0, True)), \
                patch.object(voice_output, "play", lambda *a, **k: None), \
                patch.object(voice_output, "muted", lambda: False), patch.object(voice_output, "volume", lambda: 1):
            siri.speak("[chuckling] There you go, Notes.")
        self.assertEqual(sent, ["There you go, Notes."])
        voice_output.set_cues("fish", "some", ["laughing"])
        self.assertIn("[laughing]", siri.cue_rule())
        self.assertNotIn("[chuckling]", siri.cue_rule())


if __name__ == "__main__":
    unittest.main()
