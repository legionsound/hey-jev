"""Settings closure: Jev check, Voice pane sample, collapsible Advanced, wake reset, recognizer info.
The real window is built headless; every save, key, network and playback function is patched."""
import unittest
from unittest.mock import MagicMock, patch

import requests
from Foundation import NSDate, NSRunLoop

import assistant_ui
import siri
import voice_output


def pump(until, seconds=2.0):
    """Run the main run loop until a background result has landed on the main thread."""
    end = NSDate.dateWithTimeIntervalSinceNow_(seconds)
    while not until() and NSDate.date().compare_(end) < 0:
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.02))
    return until()


class Base(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.object(assistant_ui.AppDelegate, "refreshModels_", lambda self, s: None),
                        patch.object(assistant_ui, "get_secret", lambda k: None),
                        patch.object(assistant_ui, "save_advanced_open", MagicMock()),
                        patch.object(assistant_ui, "advanced_open", lambda: False),
                        patch.object(assistant_ui, "whisper_cached", lambda size: True),
                        patch.object(assistant_ui, "voice", lambda: {"id": "defaultvoice01", "title": "Hey Jev voice"})]
        for p in self.patches:
            p.start()
        self.d = assistant_ui.AppDelegate.alloc().init()
        self.d.catalog, self.d.fetch_generation, self.d.worker_started, self.d.controls = [], 0, False, None
        self.d._show_settings()

    def tearDown(self):
        if getattr(self.d, "settings_sheet", None):
            self.d.closeSettings_(None)
        for p in self.patches:
            p.stop()


class JevCheckTests(Base):
    def test_never_automatic_and_uses_typed_key_and_source(self):
        with patch.object(siri, "check_jev", return_value=142) as check:
            pump(lambda: False, 0.1)
            check.assert_not_called()  # opening Settings never checks
            self.d.jev_provider.selectItemAtIndex_(1)  # TypeSafe
            self.d._sync_key_rows()
            self.d.key_fields["TYPESAFE_API_KEY"].setStringValue_("ts-typed")
            self.d.checkJev_(None)
            self.assertTrue(pump(lambda: self.d.jev_check_result.stringValue() != "Checking…"))
        check.assert_called_once_with("typesafe", "ts-typed")
        self.assertEqual(self.d.jev_check_result.stringValue(), "Connected · 142 ms")
        self.assertTrue(self.d.jev_check_button.isEnabled())

    def test_error_shown_in_plain_words(self):
        with patch.object(siri, "check_jev", side_effect=ValueError("The key was rejected.")):
            self.d.key_fields["JEV_OPENROUTER_API_KEY"].setStringValue_("or-typed")
            self.d.jev_provider.selectItemAtIndex_(0)
            self.d.checkJev_(None)
            self.assertTrue(pump(lambda: self.d.jev_check_result.stringValue() != "Checking…"))
        self.assertEqual(self.d.jev_check_result.stringValue(), "The key was rejected.")

    def test_unexpected_error_never_echoes_its_text(self):
        with patch.object(siri, "check_jev", side_effect=RuntimeError("secret-key-in-message")):
            self.d.checkJev_(None)
            self.assertTrue(pump(lambda: self.d.jev_check_result.stringValue() != "Checking…"))
        self.assertEqual(self.d.jev_check_result.stringValue(), "Check failed: RuntimeError")

    def test_result_after_close_is_ignored(self):
        with patch.object(siri, "check_jev", return_value=5):
            self.d.checkJev_(None)
            gen = self.d.jev_check_generation
            self.d.closeSettings_(None)
            self.d.jevChecked_({"generation": gen, "ms": 5})  # lands late: no crash, nothing shown


class CheckJevCallTests(unittest.TestCase):
    def response(self, status=200, body=None):
        r = MagicMock(status_code=status)
        r.json.return_value = body if body is not None else {"answers": {"greeting": {"type": "noul", "noul": 0.9}}}
        r.raise_for_status.side_effect = requests.HTTPError(response=r) if status >= 400 else None
        return r

    def test_one_small_request_with_the_given_key(self):
        with patch.object(siri.requests, "post", return_value=self.response()) as post:
            self.assertIsInstance(siri.check_jev("openrouter", "k-1"), int)
        post.assert_called_once()
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://openrouter.ai/api/alpha/decisions")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer k-1")
        self.assertEqual(list(kwargs["json"]["questions"]), ["greeting"])
        self.assertEqual((kwargs["timeout"], kwargs["allow_redirects"]), (10, False))

    def test_plain_errors(self):
        cases = [(self.response(401), "The key was rejected."),
                 (self.response(500), "api.typesafe.ai answered HTTP 500."),
                 (requests.Timeout(), "No answer from api.typesafe.ai within 10 seconds."),
                 (requests.ConnectionError(), "Can't reach api.typesafe.ai. Check the connection."),
                 (self.response(body={"nope": 1}), "api.typesafe.ai answered, but not in Jev's format.")]
        for outcome, message in cases:
            kw = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
            with patch.object(siri.requests, "post", **kw), self.assertRaises(ValueError) as caught:
                siri.check_jev("typesafe", "k-secret")
            self.assertEqual(str(caught.exception), message)
            self.assertNotIn("k-secret", str(caught.exception))

    def test_no_key_no_request(self):
        with patch.object(siri.requests, "post") as post, self.assertRaises(ValueError):
            siri.check_jev("typesafe", "")
        post.assert_not_called()


class VoiceSampleTests(Base):
    def setUp(self):
        super().setUp()
        self.played = []
        for p in (patch.object(voice_output, "muted", lambda: False), patch.object(voice_output, "volume", lambda: 0.8),
                  patch.object(voice_output, "play", self.played.append), patch.object(voice_output, "stop", MagicMock())):
            p.start()
            self.addCleanup(p.stop)

    def test_play_uses_typed_key_then_returns_to_play(self):
        self.d.key_fields["FISH_AUDIO_API_KEY"].setStringValue_("fish-typed")
        with patch.object(siri, "fetch_tts", return_value=("/tmp/s.wav", 10, False)) as fetch:
            self.d.playSample_(None)
            self.assertEqual(self.d.sample_button.title(), "Stop")
            self.assertTrue(pump(lambda: self.d.sample_button.title() == "Play Sample"))
        fetch.assert_called_once_with(assistant_ui.SAMPLE_LINE, key="fish-typed", voice_id="defaultvoice01")
        self.assertEqual(self.played, ["/tmp/s.wav"])

    def test_stop_while_fetching_never_starts_playback(self):
        self.d.key_fields["FISH_AUDIO_API_KEY"].setStringValue_("fish-typed")
        with patch.object(siri, "fetch_tts", return_value=("/tmp/s.wav", 10, False)):
            with patch.object(assistant_ui.threading, "Thread") as thread:
                self.d.playSample_(None)
                run = thread.call_args.kwargs["target"]
            self.d.playSample_(None)  # Stop
            voice_output.stop.assert_called_once()
            run()  # the fetch finishes after Stop
        self.assertEqual(self.played, [])
        self.assertEqual(self.d.sample_button.title(), "Play Sample")

    def test_muted_or_no_key_explains_and_fetches_nothing(self):
        with patch.object(siri, "fetch_tts") as fetch:
            self.d.playSample_(None)
            self.assertEqual(self.d.sample_result.stringValue(), "Enter a Fish Audio key first.")
            with patch.object(voice_output, "muted", lambda: True):
                self.d.playSample_(None)
            self.assertIn("muted", self.d.sample_result.stringValue())
        fetch.assert_not_called()

    def test_close_stops_a_playing_sample(self):
        self.d.sample_playing = True
        self.d.closeSettings_(None)
        voice_output.stop.assert_called_once()

    def test_pane_mirrors_live_gain_and_mute(self):
        with patch.object(voice_output, "volume", lambda: 0.25), patch.object(voice_output, "muted", lambda: True):
            self.d._sync_voice_pane()
        self.assertAlmostEqual(self.d.settings_voice_slider.doubleValue(), 0.25)
        self.assertFalse(self.d.settings_voice_slider.isEnabled())
        self.assertEqual(self.d.settings_mute.state(), 1)


class VoicePickerTests(Base):
    VOICES = [{"id": "voiceaaaa01", "title": "Aria", "author": "fish"},
              {"id": "defaultvoice01", "title": "Hey Jev voice", "author": ""}]

    def find(self, query="", **kw):
        self.d.voice_search.setStringValue_(query)
        with patch.object(siri, "fish_voices", **kw) as fv:
            self.d.findVoices_(None)
            self.assertTrue(pump(lambda: self.d.voice_find.isEnabled()))
        return fv

    def test_saved_voice_shown_first_and_find_is_deliberate(self):
        self.assertEqual(self.d.voice_popup.titleOfSelectedItem(), "Hey Jev voice")
        self.d.key_fields["FISH_AUDIO_API_KEY"].setStringValue_("fish-typed")
        fv = self.find("aria", return_value=self.VOICES)
        fv.assert_called_once_with("fish-typed", "aria")
        self.assertEqual([v["id"] for v in self.d.voice_choices], ["defaultvoice01", "voiceaaaa01"])  # no duplicate
        self.assertEqual(self.d.voice_popup.titleOfSelectedItem(), "Hey Jev voice")  # finding never switches
        self.assertEqual(self.d.voice_message.stringValue(), "1 voices matching \u201caria\u201d.")

    def test_sample_uses_the_chosen_unsaved_voice(self):
        self.find(return_value=self.VOICES)
        self.d.voice_popup.selectItemAtIndex_(1)
        self.d.key_fields["FISH_AUDIO_API_KEY"].setStringValue_("fish-typed")
        with patch.object(voice_output, "muted", lambda: False), patch.object(voice_output, "volume", lambda: 1.0), \
                patch.object(voice_output, "play"), patch.object(siri, "fetch_tts", return_value=("/tmp/a.wav", 1, False)) as f:
            self.d.playSample_(None)
            self.assertTrue(pump(lambda: self.d.sample_button.title() == "Play Sample"))
        self.assertEqual(f.call_args.kwargs["voice_id"], "voiceaaaa01")

    def test_error_keeps_the_current_choice(self):
        self.find(side_effect=ValueError("Fish Audio rejected the key."))
        self.assertEqual(self.d.voice_message.stringValue(), "Fish Audio rejected the key.")
        self.assertEqual(len(self.d.voice_choices), 1)

    def test_save_stores_a_changed_voice_only(self):
        self.find(return_value=self.VOICES)
        saves = {name: patch.object(assistant_ui, name) for name in (
            "save_secret", "save_wake_settings", "save_answer_settings", "save_confirm_policy",
            "save_transcription_backend", "save_tiebreak_threshold", "save_voice")}
        mocks = {n: p.start() for n, p in saves.items()}
        self.addCleanup(lambda: [p.stop() for p in saves.values()])
        with patch.object(assistant_ui, "get_secret", return_value="stored"), patch.object(siri, "reload_keys"), \
                patch.object(assistant_ui, "wake_settings", return_value=("Hey Jev", [])), \
                patch.object(assistant_ui.AppDelegate, "_start_worker"):
            self.d.saveSettings_(None)
            mocks["save_voice"].assert_not_called()  # unchanged
            self.d._show_settings()
            self.find(return_value=self.VOICES)
            self.d.voice_popup.selectItemAtIndex_(1)
            self.d.saveSettings_(None)
        mocks["save_voice"].assert_called_once_with("voiceaaaa01", "Aria")


class FishVoicesCallTests(unittest.TestCase):
    def response(self, status=200, items=None):
        r = MagicMock(status_code=status)
        r.json.return_value = {"items": items if items is not None else [
            {"_id": "voiceaaaa01", "type": "tts", "state": "trained", "title": "Aria", "author": {"nickname": "fish"}},
            {"_id": "voicebbbb02", "type": "tts", "state": "training", "title": "Half done"},
            {"_id": "voicecccc03", "type": "svc", "state": "trained", "title": "Not TTS"},
            {"_id": "bad id!", "type": "tts", "state": "trained", "title": "Bad"}]}
        return r

    def test_own_voices_when_empty_public_search_otherwise(self):
        with patch.object(siri.requests, "get", return_value=self.response()) as get:
            self.assertEqual(siri.fish_voices("k-1", ""), [{"id": "voiceaaaa01", "title": "Aria", "author": "fish"}])
            args, kw = get.call_args
            self.assertEqual(args[0], "https://api.fish.audio/model")
            self.assertEqual(kw["params"]["self"], "true")
            self.assertNotIn("title", kw["params"])
            self.assertEqual((kw["headers"]["Authorization"], kw["allow_redirects"], kw["timeout"]), ("Bearer k-1", False, 10))
            siri.fish_voices("k-1", " aria ")
            self.assertEqual(get.call_args.kwargs["params"]["title"], "aria")
            self.assertNotIn("self", get.call_args.kwargs["params"])

    def test_plain_errors_without_the_key(self):
        for outcome, message in [(self.response(401), "Fish Audio rejected the key."),
                                 (self.response(502), "Fish Audio answered HTTP 502."),
                                 (requests.Timeout(), "Fish Audio didn't answer within 10 seconds."),
                                 (requests.ConnectionError(), "Can't reach Fish Audio. Check the connection.")]:
            kw = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
            with patch.object(siri.requests, "get", **kw), self.assertRaises(ValueError) as caught:
                siri.fish_voices("k-secret", "x")
            self.assertEqual(str(caught.exception), message)
        with patch.object(siri.requests, "get") as get, self.assertRaises(ValueError):
            siri.fish_voices("", "x")
        get.assert_not_called()


class VoiceSettingTests(unittest.TestCase):
    def test_saved_voice_is_used_and_bad_storage_falls_back(self):
        import model_settings
        store = MagicMock()
        with patch.object(model_settings, "PREFS", store):
            store.stringForKey_.return_value = '{"id": "voiceaaaa01", "title": "Aria"}'
            self.assertEqual(model_settings.voice(), {"id": "voiceaaaa01", "title": "Aria"})
            for bad in ('{"id": "x"}', '{"id": "bad id!", "title": "t"}', "not json", None):
                store.stringForKey_.return_value = bad
                self.assertEqual(model_settings.voice(), model_settings.DEFAULT_VOICE)
            with self.assertRaises(ValueError):
                model_settings.save_voice("bad id!", "t")
            model_settings.save_voice("voiceaaaa01", "Aria")
            store.setObject_forKey_.assert_called_once()

    def test_tts_speaks_with_the_saved_voice(self):
        with patch.object(siri.model_settings, "voice", return_value={"id": "voiceaaaa01", "title": "Aria"}), \
                patch.object(siri.requests, "post") as post, patch("os.path.exists", return_value=False), \
                patch("os.makedirs"), patch("builtins.open", create=True):
            post.return_value.content = b"RIFF"
            siri.fetch_tts("hi", key="k")
            self.assertEqual(post.call_args.kwargs["json"]["reference_id"], "voiceaaaa01")
            siri.fetch_tts("hi", key="k", voice_id="voicebbbb02")
            self.assertEqual(post.call_args.kwargs["json"]["reference_id"], "voicebbbb02")


class VoiceStopTests(unittest.TestCase):
    def test_stop_stops_the_current_sound(self):
        sound = MagicMock()
        with patch.object(voice_output, "_sound", sound):
            voice_output.stop()
        sound.stop.assert_called_once()
        voice_output.stop()  # nothing playing: no error


class AdvancedTests(Base):
    def test_collapsed_by_default_and_toggle_persists(self):
        self.assertTrue(self.d.advanced_view.isHidden())
        self.d.advanced_toggle.setState_(1)
        self.d.toggleAdvanced_(self.d.advanced_toggle)
        self.assertFalse(self.d.advanced_view.isHidden())
        assistant_ui.save_advanced_open.assert_called_with(True)

    def test_summary_counts_overrides_while_collapsed(self):
        self.assertEqual(self.d.advanced_summary.stringValue(), "Defaults")
        field = self.d.parameter_fields["temperature"]
        self.assertTrue(field.isEnabled())  # the default model supports it
        field.setStringValue_("0.4")
        self.d._sync_advanced_summary()
        self.assertEqual(self.d.advanced_summary.stringValue(), "1 override")


class WakeResetTests(Base):
    def test_reset_fills_default_and_saves_nothing(self):
        self.d.wake_field.setStringValue_("Okay Zorblat")
        self.d.alias_field.setStringValue_("OK Zorblat")
        with patch.object(assistant_ui, "save_wake_settings") as save:
            self.d.resetWake_(None)
        save.assert_not_called()
        self.assertEqual((self.d.wake_field.stringValue(), self.d.alias_field.stringValue()), ("Hey Jev", ""))
        self.assertIn("Save to apply", self.d.settings_message.stringValue())


class RecognizerInfoTests(Base):
    def test_whisper_model_download_and_loading(self):
        self.d.backend_popup.selectItemAtIndex_(0)
        with patch.object(assistant_ui, "whisper_cached", lambda size: False), \
                patch.dict(siri.STT, {"backend": None, "blocked": None, "switching": True}):
            self.d._show_backend()
            self.assertEqual(self.d.backend_model.stringValue(),
                             f"Whisper {siri.WHISPER_MODEL} · downloads about 480 MB at first start")
            self.assertEqual(self.d.backend_status.stringValue(), "Loading…")
        with patch.dict(siri.STT, {"backend": "whisper", "blocked": None, "switching": False}):
            self.d._show_backend()
            self.assertEqual(self.d.backend_model.stringValue(), f"Whisper {siri.WHISPER_MODEL} · downloaded")
            self.assertEqual(self.d.backend_status.stringValue(), "Loaded, runs on this Mac")
        self.assertEqual(self.d.backend_locale.stringValue(), "English (US)")

    def test_apple_has_no_fictitious_model_picker(self):
        self.d.backend_popup.selectItemAtIndex_(1)
        with patch.dict(siri.STT, {"backend": None, "blocked": None, "switching": False}):
            self.d._show_backend()
        self.assertEqual(self.d.backend_model.stringValue(), "Managed by macOS, on-device only")


if __name__ == "__main__":
    unittest.main()
