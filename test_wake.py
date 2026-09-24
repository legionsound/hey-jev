import unittest

import wake


class WakeTests(unittest.TestCase):
    def test_default_keeps_known_mishearings(self):
        w = wake.Wake()
        for heard in ["Hey Jev, open Notes", "hey jeff open notes", "Okay Jev. Open Notes", "Hey Jeb open Notes"]:
            self.assertEqual(w.match(heard).lower(), "open notes", heard)
        self.assertEqual(w.match("Hey Jev"), "")
        self.assertIsNone(w.match("Hey Jeffrey open Notes"))
        self.assertIsNone(w.match("they said hey jev"))  # only at the start

    def test_custom_phrase_is_exact(self):
        w = wake.Wake("Okay Zorblat")
        self.assertEqual(w.match("Okay Zorblat, open Notes."), "open Notes")
        self.assertEqual(w.match("okay   ZORBLAT open notes"), "open notes")
        for near in ["OK Zorblat open Notes", "Okay Zorblot open Notes", "Okay Zorblats open Notes",
                     "Hey Jev open Notes", "so okay zorblat open notes", "Case or blood open Notes"]:
            self.assertIsNone(w.match(near), near)

    def test_aliases_are_explicit(self):
        w = wake.Wake("Okay Zorblat", wake.parse_aliases("OK Zorblat, Okay Zorblot"))
        self.assertEqual(w.match("OK Zorblat open Notes"), "open Notes")
        self.assertEqual(w.match("Okay Zorblot, open Notes"), "open Notes")
        self.assertIsNone(w.match("Okay Zorblit open Notes"))
        self.assertEqual(w.hints, ["Okay Zorblat", "OK Zorblat", "Okay Zorblot"])

    def test_prompt_and_hint_follow_the_phrase(self):
        w = wake.Wake("Computer please")
        self.assertIn("Computer please, open Spotify.", w.prompt)
        self.assertIn("Computer please", w.hint_text())
        self.assertIn("Hey Jev, open Spotify.", wake.Wake().prompt)

    def test_validation(self):
        for bad in ["", "   ", "a b c d e", "Hi", "hey <jev>", "x" * 41]:
            with self.assertRaises(ValueError, msg=bad):
                wake.validate(bad)
        self.assertEqual(wake.validate("  Hey   Jarvis "), "Hey Jarvis")
        self.assertEqual(wake.validate("Computer"), "Computer")
        with self.assertRaises(ValueError):
            wake.parse_aliases(",".join(f"Hey Bot{i}" for i in range(7)))

    def test_regex_special_characters_are_literal(self):
        w = wake.Wake("Hey J.D.")
        self.assertIsNone(w.match("Hey JxDx open Notes"))


if __name__ == "__main__":
    unittest.main()
