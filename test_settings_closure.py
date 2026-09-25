"""Settings closure: Jev check, Voice pane sample, collapsible Advanced, wake reset, recognizer info.
The real window is built headless; every save, key, network and playback function is patched."""
import unittest
from unittest.mock import MagicMock, patch

import requests
from Foundation import NSDate, NSRunLoop

import assistant_ui
import permissions
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
                        patch.object(assistant_ui, "voice", lambda: {"id": "defaultvoice01", "title": "Hey Jev voice"}),
                        patch.object(assistant_ui, "jev_model", lambda p: assistant_ui.JEV_MODELS[p]),
                        patch.object(assistant_ui, "fish_model", lambda: "s2.1-pro-free"),
                        patch.object(assistant_ui, "whisper_model", lambda: "small.en"),
                        patch.object(assistant_ui, "ocr_level", lambda: "accurate"),
                        patch.object(assistant_ui, "app_folders", lambda: []),
                        patch.object(assistant_ui, "wake_settings", lambda: ("Hey Jev", [])),
                        patch.object(voice_output, "cues", lambda provider="fish": self.saved_cues),
                        patch.object(voice_output, "set_cues", MagicMock()),
                        patch.object(assistant_ui, "begin_sheet", lambda parent, sheet: None),  # no sheet on screen
                        patch.object(assistant_ui, "show_settings_window", lambda sheet: None),  # no window either
                        patch.object(permissions, "snapshot", lambda: {}),  # the Permissions pane reads no real state
                        patch.object(permissions, "running", lambda bundle: False)]
        self.saved_cues = {"mode": "all", "on": list(voice_output.CUES["fish"])}
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
        check.assert_called_once_with("typesafe", "ts-typed", "jev-1.13.0")
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

    def test_old_result_never_lands_in_a_reopened_window(self):
        with patch.object(assistant_ui.threading, "Thread"):  # hold every result back
            self.d.checkJev_(None)
            old = self.d.ops["check"]
            self.d.closeSettings_(None)
            self.d.jevChecked_({"generation": old, "ms": 999})  # closed: no crash
            self.d._show_settings()
            self.d.checkJev_(None)
            self.assertNotEqual(self.d.ops["check"], old)  # ids are never reused across sessions
            self.d.jevChecked_({"generation": old, "ms": 999})
        self.assertEqual(self.d.jev_check_result.stringValue(), "Checking…")

    def test_changing_source_or_key_clears_the_result(self):
        with patch.object(assistant_ui.threading, "Thread"):
            self.d.key_fields["JEV_OPENROUTER_API_KEY"].setStringValue_("key-a")
            self.d.checkJev_(None)
            op = self.d.ops["check"]
            self.d.key_fields["JEV_OPENROUTER_API_KEY"].setStringValue_("key-b")  # edited while checking
            self.d.jevChecked_({"generation": op, "ms": 10})
        self.assertEqual(self.d.jev_check_result.stringValue(), "Not checked")
        with patch.object(assistant_ui.threading, "Thread"):
            self.d.checkJev_(None)
            self.d.jevChecked_({"generation": self.d.ops["check"], "ms": 10})
        self.assertEqual(self.d.jev_check_result.stringValue(), "Connected · 10 ms")
        self.d.jev_provider.selectItemAtIndex_(1)
        self.d.jevSourceChanged_(self.d.jev_provider)
        self.assertEqual(self.d.jev_check_result.stringValue(), "Not checked")


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


class FakeRec:
    def __init__(self):
        self.on = self.paused = self.isolated = False


class VoiceSampleTests(Base):
    def setUp(self):
        super().setUp()
        self.played = []
        fake_play = lambda path, owner=None, cancelled=None: (
            False if cancelled is not None and cancelled.is_set() else self.played.append((path, owner)) or True)
        for p in (patch.object(voice_output, "muted", lambda: False), patch.object(voice_output, "volume", lambda: 0.8),
                  patch.object(voice_output, "play", side_effect=fake_play), patch.object(voice_output, "stop", MagicMock()),
                  patch.object(siri, "fetch_tts", return_value=("/tmp/s.wav", 10, False))):
            p.start()
            self.addCleanup(p.stop)
        self.d.key_fields["FISH_AUDIO_API_KEY"].setStringValue_("fish-typed")

    def test_without_the_worker_plays_directly_and_returns_to_play(self):
        self.d.playSample_(None)
        self.assertEqual(self.d.sample_button.title(), "Stop")
        self.assertTrue(pump(lambda: self.d.sample_button.title() == "Play Sample"))
        siri.fetch_tts.assert_called_once_with(siri.SAMPLE_LINE, key="fish-typed", voice_id="defaultvoice01",
                                               model="s2.1-pro-free")
        self.assertEqual([p for p, _ in self.played], ["/tmp/s.wav"])

    def test_with_the_worker_it_goes_through_the_speech_owner(self):
        import queue
        self.d.worker_started, self.d.controls = True, queue.Queue()
        self.d.playSample_(None)
        kind, op = self.d.controls.get_nowait()
        self.assertEqual(kind, "voice_sample")
        self.assertIs(op, self.d.sample_op)
        self.assertEqual(self.played, [])  # nothing plays outside the owner

    def test_stop_cancels_only_this_sample(self):
        with patch.object(assistant_ui.threading, "Thread"):
            self.d.playSample_(None)
        op = self.d.sample_op
        self.d.playSample_(None)  # Stop
        self.assertTrue(op.cancelled.is_set())
        voice_output.stop.assert_called_once_with(owner=op)  # never a bare stop of Jev's own speech
        siri.play_sample(op, siri.contextlib.nullcontext)  # the fetch finishes afterwards
        self.assertEqual(self.played, [])

    def test_close_and_reopen_during_fetch(self):
        with patch.object(assistant_ui.threading, "Thread"):
            self.d.playSample_(None)
        old = self.d.sample_op
        self.d.closeSettings_(None)
        self.assertTrue(old.cancelled.is_set())
        self.d._show_settings()
        self.d.key_fields["FISH_AUDIO_API_KEY"].setStringValue_("fish-typed")
        with patch.object(assistant_ui.threading, "Thread"):
            self.d.playSample_(None)
        new = self.d.sample_op
        self.assertNotEqual(new.id, old.id)
        self.d.sampleState_({"op": old.id, "state": "done", "text": "old"})  # the old one lands late
        self.assertIs(self.d.sample_op, new)
        self.assertEqual(self.d.sample_button.title(), "Stop")
        siri.play_sample(old, siri.contextlib.nullcontext)
        self.assertEqual(self.played, [])

    def test_muted_or_no_key_explains_and_fetches_nothing(self):
        self.d.key_fields["FISH_AUDIO_API_KEY"].setStringValue_("")
        self.d.playSample_(None)
        self.assertEqual(self.d.sample_result.stringValue(), "Enter a Fish Audio key first.")
        with patch.object(voice_output, "muted", lambda: True):
            self.d.playSample_(None)
        self.assertIn("muted", self.d.sample_result.stringValue())
        siri.fetch_tts.assert_not_called()

    def test_pane_mirrors_live_gain_and_mute(self):
        with patch.object(voice_output, "volume", lambda: 0.25), patch.object(voice_output, "muted", lambda: True):
            self.d._sync_voice_pane()
        self.assertAlmostEqual(self.d.settings_voice_slider.doubleValue(), 0.25)
        self.assertFalse(self.d.settings_voice_slider.isEnabled())
        self.assertEqual(self.d.settings_mute.state(), 1)


class FirstSetupSampleTests(Base):
    """A sample started before setup plays directly; the first Save must silence it before the mic opens."""

    def setUp(self):
        super().setUp()
        for p in (patch.object(voice_output, "muted", lambda: False), patch.object(voice_output, "volume", lambda: 1.0),
                  patch.object(voice_output, "stop", MagicMock())):
            p.start()
            self.addCleanup(p.stop)
        self.d.key_fields["FISH_AUDIO_API_KEY"].setStringValue_("fish-typed")

    def first_save_with_short_phrase(self):
        self.d.wake_field.setStringValue_("Hi")  # the warning keeps Settings open, so close can't be relied on
        started = []
        saves = [patch.object(assistant_ui, n) for n in SAVES]
        for p in saves:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in saves])
        with patch.object(assistant_ui, "get_secret", return_value="stored"), patch.object(siri, "reload_keys"), \
                patch.object(assistant_ui.threading, "Thread") as thread:
            thread.return_value.start.side_effect = lambda: started.append(True)
            self.d.saveSettings_(None)
        return started

    def test_delayed_fetch_never_plays_after_listening_starts(self):
        played = []
        with patch.object(assistant_ui.threading, "Thread") as thread:
            self.d.playSample_(None)
            run_args = thread.call_args.kwargs["args"]  # (op, nullcontext): the fetch hasn't finished
        op = self.d.sample_op
        started = self.first_save_with_short_phrase()
        self.assertTrue(self.d.worker_started and started)
        self.assertTrue(op.cancelled.is_set())
        self.assertIsNone(self.d.sample_op)
        self.assertIsNotNone(self.d.settings_sheet)  # the warning window stayed open (closing clears it)
        with patch.object(siri, "fetch_tts", return_value=("/tmp/s.wav", 1, False)), \
                patch.object(voice_output, "play", side_effect=lambda *a, **k: played.append(a) or True):
            siri.play_sample(*run_args)  # the fetch lands after the mic opened
        self.assertEqual(played, [])

    def test_active_playback_is_stopped_before_listening(self):
        with patch.object(assistant_ui.threading, "Thread"):
            self.d.playSample_(None)
        op = self.d.sample_op
        self.first_save_with_short_phrase()
        voice_output.stop.assert_called_once_with(owner=op)


class MicTestOwnershipTests(Base):
    def test_old_mic_result_never_touches_a_reopened_window(self):
        import queue
        self.d.worker_started, self.d.controls = True, queue.Queue()
        self.d.micTest_(None)
        _kind, old_reply = self.d.controls.get_nowait()
        self.d.closeSettings_(None)
        self.d._show_settings()
        self.d.micTest_(None)
        self.assertFalse(self.d.test_button.isEnabled())
        old_reply({"text": "OLD SESSION", "ms": 1, "wake": "Hey Jev", "wake_matched": False})
        pump(lambda: False, 0.2)
        self.assertNotIn("OLD SESSION", self.d.test_result.stringValue())
        self.assertFalse(self.d.test_button.isEnabled())  # still waiting for its own test


class TeachJevTests(Base):
    def setUp(self):
        super().setUp()
        import queue
        self.d.worker_started, self.d.controls = True, queue.Queue()

    def takes(self):
        out = []
        while not self.d.controls.empty():
            kind, reply = self.d.controls.get_nowait()
            self.assertEqual(kind, "mic_test")  # transcript-only capture; nothing is dispatched
            out.append(reply)
        return out

    def run_takes(self, results):
        self.d.teachWake_(None)
        for r in results:
            (reply,) = self.takes()  # one take at a time, the next only after this one's result
            reply(r)
            self.assertTrue(pump(lambda: not self.d.controls.empty() or self.d.teach.get("done")))

    def test_needs_the_worker_and_a_saved_phrase(self):
        self.d.worker_started = False
        self.d.teachWake_(None)
        self.assertEqual(self.d.settings_message.stringValue(), "Hey Jev isn't running yet.")
        self.d.worker_started = True
        self.d.wake_field.setStringValue_("Okay Zorblat")  # typed, not saved
        self.d.teachWake_(None)
        self.assertIn("Save first", self.d.settings_message.stringValue())
        self.assertEqual(self.takes(), [])

    def test_five_takes_suggest_unchecked_spellings_and_count_skips(self):
        heard = [{"text": "Hey Jev"}, {"text": "Hey Jazz"}, {"error": "Busy right now."}, {"text": "Hey Jazz"},
                 {"text": "Hey Jev"}]
        self.run_takes(heard)
        self.assertEqual(self.d.teach_status.stringValue(), "Heard it 2 of 4 takes. 1 take skipped (nothing heard or "
                         "the mic was busy). Tick any spelling that was really you, then Add Selected.")
        (box, alias), = self.d.teach_boxes
        self.assertEqual((box.title(), box.state(), alias), ("\u201cHey Jazz\u201d \u00d72", 0, "Hey Jazz"))
        self.assertFalse(self.d.teach_add.isEnabled())  # nothing is added without a tick
        box.setState_(1)
        self.d.teachTicked_(box)
        with patch.object(assistant_ui, "save_wake_settings") as save:
            self.d.teachAdd_(None)
        save.assert_not_called()  # the user saves as usual
        self.assertEqual(self.d.alias_field.stringValue(), "Hey Jazz")
        self.assertEqual(self.d.settings_message.stringValue(), "Added 1 spelling. Save to apply.")
        self.assertIsNone(self.d.teach)

    def test_close_after_results_keeps_nothing_heard(self):
        self.run_takes([{"text": "Hey Jazz"}, {"text": "Hey Jazz"}, {"text": "Hey Jev"}, {"text": "Hey Jev"},
                        {"text": "Hey Jev"}])
        self.assertEqual([a for _, a in self.d.teach_boxes], ["Hey Jazz"])
        self.d.teachClose_(None)
        self.assertEqual((self.d.teach, self.d.teach_sheet, self.d.teach_boxes), (None, None, []))
        for name in ("teach_status", "teach_list", "teach_add", "teach_close"):
            self.assertIsNone(getattr(self.d, name))
        self.assertEqual(self.d.alias_field.stringValue(), "")  # nothing was added without a tick

    def test_all_matched(self):
        self.run_takes([{"text": "Hey Jev"}] * 5)
        self.assertEqual(self.d.teach_status.stringValue(), "Hey Jev already hears you: 5 of 5 takes matched.")
        self.assertEqual(self.d.teach_boxes, [])

    def test_stop_mid_sequence_ignores_late_takes(self):
        self.d.teachWake_(None)
        (reply,) = self.takes()
        self.d.teachClose_(None)
        reply({"text": "Hey Jazz"})
        pump(lambda: False, 0.2)
        self.assertEqual(self.takes(), [])  # no further take was started
        self.assertIsNone(self.d.teach)

    def test_close_and_reopen_ignores_the_old_session(self):
        self.d.teachWake_(None)
        (old,) = self.takes()
        self.d.closeSettings_(None)
        self.d._show_settings()
        self.d.teachWake_(None)
        (new,) = self.takes()
        old({"text": "Hey Jazz"})
        pump(lambda: False, 0.2)
        self.assertEqual(self.d.teach["take"], 1)
        self.assertEqual(self.d.teach["texts"], [])
        self.assertEqual(self.takes(), [])

    def test_respects_the_six_spelling_limit(self):
        self.d.alias_field.setStringValue_("")
        self.run_takes([{"text": "Hey Jazz"}, {"text": "Hey Jams"}, {"text": "Hey Jazz"}, {"text": "Hey Jams"},
                        {"text": "Hey Jev"}])
        full = "A One, B Two, C Three, D Four, E Five"
        self.d.alias_field.setStringValue_(full)
        for box, _ in self.d.teach_boxes:
            box.setState_(1)
        self.d.teachAdd_(None)
        self.assertEqual(self.d.alias_field.stringValue(), full + ", Hey Jazz")
        self.assertIn("1 didn't fit: up to 6 extra spellings", self.d.settings_message.stringValue())


class PlaySampleOwnerTests(unittest.TestCase):
    """play_sample against the real Floor with a fake recorder: the wake listener must not hear the sample."""

    def setUp(self):
        self.rec = FakeRec()
        self.floor = siri.Floor(self.rec)
        self.reports = []
        self.op = siri.SampleOp("k", "voiceaaaa01", lambda i, st, t: self.reports.append((st, t)))
        self.seen = []
        fake_play = lambda path, owner=None, cancelled=None: (
            self.seen.append((self.floor.locked(), self.rec.paused, owner)) or True)
        for p in (patch.object(siri, "fetch_tts", return_value=("/tmp/s.wav", 1, False)),
                  patch.object(voice_output, "play", side_effect=fake_play), patch.object(siri.time, "sleep")):
            p.start()
            self.addCleanup(p.stop)

    def test_plays_holding_the_floor_with_the_mic_paused(self):
        siri.play_sample(self.op, self.floor.hold)
        self.assertEqual(self.seen, [(True, True, self.op)])
        self.assertFalse(self.floor.locked())
        self.assertFalse(self.rec.paused)
        self.assertEqual(self.reports[-1], ("done", ""))

    def test_stop_while_queued_for_the_floor_never_plays(self):
        import contextlib

        @contextlib.contextmanager
        def busy_floor():  # Stop is clicked while the sample waits for Jev to finish speaking
            self.op.cancel()
            with self.floor.hold():
                yield
        siri.play_sample(self.op, busy_floor)
        self.assertEqual(self.seen, [])
        self.assertEqual(self.reports[-1], ("done", "Stopped."))


class VoiceOutputOwnerTests(unittest.TestCase):
    def test_stop_with_an_owner_leaves_other_speech_alone(self):
        sound, mine = MagicMock(), object()
        with patch.object(voice_output, "_sound", sound), patch.object(voice_output, "_owner", None):
            voice_output.stop(owner=mine)  # Jev's own reply is playing
            sound.stop.assert_not_called()
        with patch.object(voice_output, "_sound", sound), patch.object(voice_output, "_owner", mine):
            voice_output.stop(owner=mine)
            sound.stop.assert_called_once()

    def test_cancelled_before_start_never_opens_the_sound(self):
        import threading
        cancelled = threading.Event()
        cancelled.set()
        with patch.object(voice_output, "muted", lambda: False), patch.object(voice_output, "volume", lambda: 1.0), \
                patch.object(voice_output, "NSSound") as nssound:
            self.assertFalse(voice_output.play("/tmp/x.wav", cancelled=cancelled))
        nssound.alloc.assert_not_called()


class AppsPaneTests(Base):
    def setUp(self):
        super().setUp()
        import tempfile, os
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp)
        self.real = os.path.join(self.tmp, "Apps")
        os.mkdir(self.real)
        self.link = os.path.join(self.tmp, "link")
        os.symlink(self.real, self.link)

    def test_add_resolves_symlinks_dedupes_and_rejects_non_folders(self):
        import os
        self.d.folder_drafts = []
        self.d._add_folder(self.link)
        self.d._add_folder(self.real)  # the same folder through its real path
        self.assertEqual(self.d.folder_drafts, [os.path.realpath(self.real)])
        self.d._add_folder(os.path.join(self.tmp, "missing"))
        self.assertIn("Not a folder", self.d.settings_message.stringValue())
        self.assertEqual(len(self.d.folder_drafts), 1)

    def test_remove_and_save_validates_before_writing(self):
        import os, shutil
        extra = os.path.join(self.tmp, "Extra")
        os.mkdir(extra)
        self.d.folder_drafts = []
        self.d._add_folder(self.real)
        self.d._add_folder(extra)
        remove = MagicMock()
        remove.tag.return_value = 0
        self.d.removeAppFolder_(remove)
        self.assertEqual(self.d.folder_drafts, [os.path.realpath(extra)])
        shutil.rmtree(extra)  # vanished before Save
        with patch.object(assistant_ui, "save_app_folders") as save_folders, \
                patch.object(assistant_ui, "save_secret") as save_secret, \
                patch.object(assistant_ui, "save_answer_settings") as save_answers:
            self.d.saveSettings_(None)
        self.assertIn("Not a folder", self.d.settings_message.stringValue())
        for m in (save_folders, save_secret, save_answers):
            m.assert_not_called()  # nothing half-saved

    def test_refresh_runs_off_the_main_thread_and_shows_misses(self):
        import app_catalog, threading
        where = []
        with patch.object(app_catalog, "refresh", side_effect=lambda: where.append(threading.current_thread()) or 42), \
                patch.object(app_catalog, "last_source_misses", return_value={"mdfind": "unavailable"}):
            self.d.refreshApps_(None)
            self.assertFalse(self.d.apps_refresh.isEnabled())
            self.assertTrue(pump(lambda: self.d.apps_refresh.isEnabled()))
        self.assertIsNot(where[0], threading.main_thread())
        self.assertEqual(self.d.apps_found.stringValue(), "42 apps · Spotlight unavailable")


class AppFolderPrefsTests(unittest.TestCase):
    def test_saved_to_the_key_app_catalog_reads(self):
        import model_settings, tempfile, os
        store = MagicMock()
        d = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, d)
        with patch.object(model_settings, "PREFS", store):
            model_settings.save_app_folders([d, d])
        store.setObject_forKey_.assert_called_once_with([os.path.realpath(d)], "app_folders")
        with self.assertRaises(ValueError):
            model_settings.clean_app_folders([d] * 1 + [f"/nope/{i}" for i in range(2)])


class AsyncReopenTests(Base):
    def test_voice_lookup_and_app_scan_results_from_an_old_window_are_dropped(self):
        with patch.object(assistant_ui.threading, "Thread"):
            self.d.findVoices_(None)
            self.d.refreshApps_(None)
            old_voices, old_apps = self.d.ops["voices"], self.d.ops["apps"]
            self.d.closeSettings_(None)
            self.d._show_settings()
            self.d.findVoices_(None)
            self.d.refreshApps_(None)
        self.assertNotIn(self.d.ops["voices"], (old_voices, old_apps))
        self.d.voicesLoaded_({"generation": old_voices, "voices": [{"id": "voiceaaaa01", "title": "Old", "author": ""}],
                              "query": ""})
        self.d.appsRefreshed_({"generation": old_apps, "count": 999, "misses": []})
        self.assertEqual(len(self.d.voice_choices), 1)
        self.assertEqual(self.d.apps_found.stringValue(), "Scanning…")


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
    def test_whisper_size_picker_download_note_and_loading(self):
        self.d.backend_popup.selectItemAtIndex_(0)
        self.d._show_backend()
        self.assertFalse(self.d.whisper_popup.isHidden())
        self.assertTrue(self.d.backend_model.isHidden())
        self.assertEqual(self.d.whisper_popup.titleOfSelectedItem(), "small.en · 480 MB")
        with patch.object(assistant_ui, "whisper_cached", lambda size: False), \
                patch.dict(siri.STT, {"backend": None, "blocked": None, "switching": True}):
            self.d.whisper_popup.selectItemAtIndex_(1)  # base.en, not saved yet
            self.d.whisperChanged_(None)
            self.assertEqual(self.d.backend_next.stringValue(), "Whisper base.en downloads about 145 MB the next "
                             "time it loads. Save to switch; it reloads without a restart.")
            self.assertEqual(self.d.backend_status.stringValue(), "Loading…")
        with patch.dict(siri.STT, {"backend": "whisper", "blocked": None, "switching": False}):
            self.d.whisper_popup.selectItemAtIndex_(2)
            self.d._show_backend()
            self.assertEqual(self.d.backend_next.stringValue(), "Whisper small.en is downloaded.")
            self.assertEqual(self.d.backend_status.stringValue(), "Loaded, runs on this Mac")
        self.assertEqual(self.d.backend_locale.stringValue(), "English (US)")

    def test_apple_has_no_fictitious_model_picker(self):
        self.d.backend_popup.selectItemAtIndex_(1)
        with patch.dict(siri.STT, {"backend": None, "blocked": None, "switching": False}):
            self.d._show_backend()
        self.assertTrue(self.d.whisper_popup.isHidden())
        self.assertEqual(self.d.backend_model.stringValue(), "Managed by macOS, on-device only")


SAVES = ("save_secret", "save_wake_settings", "save_answer_settings", "save_confirm_policy", "save_transcription_backend",
         "save_tiebreak_threshold", "save_voice", "save_app_folders", "save_jev_model", "save_fish_model",
         "save_whisper_model", "save_ocr_level")


class ModelSettingsSaveTests(Base):
    def save(self, worker=False):
        import queue
        mocks = {}
        patches = [patch.object(assistant_ui, n) for n in SAVES]
        for n, p in zip(SAVES, patches):
            mocks[n] = p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        self.d.worker_started, self.d.controls = worker, queue.Queue()
        with patch.object(assistant_ui, "get_secret", return_value="stored"), patch.object(siri, "reload_keys"), \
                patch.object(assistant_ui, "wake_settings", return_value=("Hey Jev", [])), \
                patch.dict(siri.STT, {"backend": "whisper", "blocked": None, "switching": False}), \
                patch.object(assistant_ui.AppDelegate, "_start_worker"):
            self.d.saveSettings_(None)
        return mocks

    def test_defaults_shown_and_nothing_saved_until_changed(self):
        self.assertEqual(self.d.jev_model_fields["openrouter"].stringValue(), "typesafe/jev-1.13")
        self.assertEqual(self.d.jev_model_fields["typesafe"].stringValue(), "jev-1.13.0")
        self.assertEqual(self.d.fish_model_popup.titleOfSelectedItem(), "s2.1-pro-free")
        self.assertEqual(self.d.ocr_popup.titleOfSelectedItem(), "Accurate")
        mocks = self.save()
        for n in ("save_jev_model", "save_fish_model", "save_whisper_model", "save_ocr_level"):
            mocks[n].assert_not_called()

    def test_changed_models_are_saved_and_whisper_reloads_live(self):
        self.d.jev_model_fields["typesafe"].setStringValue_("jev-2")
        self.d.fish_model_popup.selectItemWithTitle_("s1")
        self.d.ocr_popup.selectItemAtIndex_(1)
        self.d.backend_popup.selectItemAtIndex_(0)
        self.d.whisper_popup.selectItemAtIndex_(0)  # tiny.en
        mocks = self.save(worker=True)
        mocks["save_jev_model"].assert_called_once_with("typesafe", "jev-2")
        mocks["save_fish_model"].assert_called_once_with("s1")
        mocks["save_ocr_level"].assert_called_once_with("fast")
        mocks["save_whisper_model"].assert_called_once_with("tiny.en")
        self.assertEqual(self.d.controls.get_nowait(), ("transcription", "whisper"))  # reload, no restart

    def test_bad_jev_model_saves_nothing(self):
        self.d.jev_model_fields["openrouter"].setStringValue_("bad model!")
        mocks = self.save()
        self.assertIn("Jev model", self.d.settings_message.stringValue())
        for m in mocks.values():
            m.assert_not_called()

    def test_check_uses_the_typed_model_and_model_edit_clears_result(self):
        self.d.jev_provider.selectItemAtIndex_(0)
        self.d._sync_key_rows()
        self.d.jev_model_fields["openrouter"].setStringValue_("typesafe/jev-2")
        with patch.object(siri, "check_jev", return_value=7) as check:
            self.d.checkJev_(None)
            self.assertTrue(pump(lambda: self.d.jev_check_result.stringValue() != "Checking…"))
        self.assertEqual(check.call_args.args[2], "typesafe/jev-2")
        with patch.object(assistant_ui.threading, "Thread"):
            self.d.checkJev_(None)
            op = self.d.ops["check"]
            self.d.jev_model_fields["openrouter"].setStringValue_("typesafe/jev-3")  # edited while checking
            self.d.jevChecked_({"generation": op, "ms": 1})
        self.assertEqual(self.d.jev_check_result.stringValue(), "Not checked")


class LayoutFitTests(Base):
    """Panes scroll when needed. The common panes fit without scrolling at their default rows; every pane's content
    stays inside its scrolling area, including Confirmations once the task and scroll rows arrive."""

    def check_contained(self):
        for item in self.d.settings_tabs.tabViewItems():
            doc = item.view().documentView()
            for sub in doc.subviews():
                if not sub.isHidden():
                    f = sub.frame()
                    self.assertLessEqual(f.origin.y + f.size.height, doc.frame().size.height,
                                         f"{item.identifier()}: {type(sub).__name__} outside its pane")

    def heights(self):
        return {i.identifier(): i.view().documentView().frame().size.height for i in self.d.settings_tabs.tabViewItems()}

    def test_common_panes_fit_without_scrolling(self):
        import tempfile, os
        folders = [tempfile.mkdtemp() for _ in range(assistant_ui.MAX_APP_FOLDERS)]
        self.addCleanup(lambda: [os.rmdir(f) for f in folders])
        self.d.folder_drafts = list(folders)
        self.d._show_folders()
        self.d.advanced_toggle.setState_(1)
        self.d.toggleAdvanced_(self.d.advanced_toggle)
        for ident, h in self.heights().items():
            if ident != "confirm":  # Confirmations may scroll; see the next test
                self.assertEqual(h, assistant_ui.PANE_H, f"{ident} needs scrolling at its default rows")
        self.check_contained()

    def test_confirmations_scrolls_with_the_task_and_scroll_rows(self):
        extra = {"task": "Start multi-step tasks", "in_task": "Each step inside a task",
                 "risky": "Risky buttons", "scroll": "Scroll pages"}
        self.d.closeSettings_(None)
        with patch.object(assistant_ui, "EFFECTS", assistant_ui.EFFECTS + tuple(extra)), \
                patch.dict(assistant_ui.EFFECT_LABELS, extra), \
                patch.object(assistant_ui, "confirm_policy", lambda: {}):
            self.d._show_settings()
        self.assertEqual(len(self.d.policy_popups), 16)
        confirm = self.d.settings_tabs.tabViewItemAtIndex_(
            self.d.settings_tabs.indexOfTabViewItemWithIdentifier_("confirm")).view()
        self.assertGreater(confirm.documentView().frame().size.height, assistant_ui.PANE_H)  # it scrolls
        self.assertTrue(confirm.hasVerticalScroller())
        headers = [v.stringValue() for v in confirm.documentView().subviews() if hasattr(v, "stringValue")]
        self.assertIn("Ask before: everyday", headers)
        self.assertIn("Ask before: screen and tasks", headers)
        self.check_contained()


class OcrLevelTests(unittest.TestCase):
    def test_vision_request_follows_the_setting(self):
        import model_settings, screen
        for level, expected in (("accurate", (0, True)), ("fast", (1, False))):
            req = MagicMock()
            with patch.object(model_settings, "ocr_level", return_value=level):
                screen.configure_ocr(req)
            req.setRecognitionLevel_.assert_called_once_with(expected[0])
            req.setUsesLanguageCorrection_.assert_called_once_with(expected[1])
        req = MagicMock()
        with patch.object(model_settings, "ocr_level", side_effect=RuntimeError("unreadable")):
            self.assertFalse(screen.configure_ocr(req))
        req.setRecognitionLevel_.assert_called_once_with(0)

    def test_read_text_uses_it(self):
        import inspect, screen
        self.assertIn("configure_ocr(req)", inspect.getsource(screen.read_text))


class VoiceCueTests(Base):
    def save(self):
        patches = [patch.object(assistant_ui, n) for n in SAVES]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        with patch.object(assistant_ui, "get_secret", return_value="stored"), patch.object(siri, "reload_keys"), \
                patch.object(assistant_ui.AppDelegate, "_start_worker"):
            self.d.saveSettings_(None)

    def voice_height(self):
        return self.d.pane_views["voice"].frame().size.height

    def test_default_all_hides_the_checkboxes_and_saves_nothing(self):
        self.assertEqual(self.d.cue_mode.titleOfSelectedItem(), "All")
        self.assertTrue(self.d.cue_view.isHidden())
        self.assertEqual(self.voice_height(), assistant_ui.PANE_H)  # fits without scrolling
        self.save()
        voice_output.set_cues.assert_not_called()

    def test_some_shows_one_checkbox_per_cue_and_saves_the_ticked_ones(self):
        self.d.cue_mode.selectItemAtIndex_(1)
        self.d.cueModeChanged_(self.d.cue_mode)
        self.assertFalse(self.d.cue_view.isHidden())
        self.assertEqual(list(self.d.cue_boxes), voice_output.CUES["fish"])
        self.assertTrue(all(b.state() for b in self.d.cue_boxes.values()))  # starts from what All performed
        self.d.cue_boxes["chuckling"].setState_(0)
        self.d.cue_boxes["sighing"].setState_(0)
        doc = self.d.pane_views["voice"]
        self.assertLessEqual(self.d.cue_view.frame().origin.y + self.d.cue_view.frame().size.height, doc.frame().size.height)
        self.save()
        voice_output.set_cues.assert_called_once_with("fish", "some", ["laughing", "cheerful", "clear throat"])

    def test_cue_list_sits_under_the_fish_group_and_playback_moves_down(self):
        pb = lambda: self.d.playback_view.frame().origin.y
        self.assertEqual(pb(), self.d.cue_top)  # All: Playback takes the cue list's place
        self.d.cue_mode.selectItemAtIndex_(1)
        self.d.cueModeChanged_(self.d.cue_mode)
        cue = self.d.cue_view.frame()
        self.assertEqual(cue.origin.y, self.d.cue_top)
        self.assertGreaterEqual(pb(), cue.origin.y + cue.size.height)  # below the cue list, no overlap
        self.d.cue_mode.selectItemAtIndex_(0)
        self.d.cueModeChanged_(self.d.cue_mode)
        self.assertEqual(pb(), self.d.cue_top)

    def test_none_saves_none_and_back_to_all(self):
        self.d.cue_mode.selectItemAtIndex_(2)
        self.d.cueModeChanged_(self.d.cue_mode)
        self.assertTrue(self.d.cue_view.isHidden())
        self.save()
        voice_output.set_cues.assert_called_once_with("fish", "none", [])

    def test_second_save_in_an_open_window_compares_with_what_was_saved(self):
        def saved(provider, mode, on=()):
            self.saved_cues = {"mode": mode, "on": list(on) if mode == "some" else
                               list(voice_output.CUES["fish"]) if mode == "all" else []}
        voice_output.set_cues.side_effect = saved
        self.d.wake_field.setStringValue_("Jo")  # the short-phrase warning keeps Settings open after Save
        self.d.cue_mode.selectItemAtIndex_(2)
        self.save()
        self.assertIsNotNone(self.d.settings_sheet)
        self.assertEqual(self.saved_cues["mode"], "none")
        self.d.cue_mode.selectItemAtIndex_(0)  # back to All in the same window
        self.save()
        self.assertEqual(voice_output.set_cues.call_args_list[-1].args, ("fish", "all", []))
        self.assertEqual(self.saved_cues["mode"], "all")

    def test_saved_some_is_shown_as_saved(self):
        self.d.closeSettings_(None)
        self.saved_cues = {"mode": "some", "on": ["cheerful"]}
        self.d._show_settings()
        self.assertEqual(self.d.cue_mode.titleOfSelectedItem(), "Some")
        self.assertFalse(self.d.cue_view.isHidden())
        self.assertEqual([c for c, b in self.d.cue_boxes.items() if b.state()], ["cheerful"])
        self.save()
        voice_output.set_cues.assert_not_called()  # unchanged


class ModelPrefsTests(unittest.TestCase):
    def test_defaults_are_todays_values_and_bad_storage_falls_back(self):
        import model_settings as ms
        store = MagicMock()
        store.stringForKey_.return_value = None
        with patch.object(ms, "PREFS", store):
            self.assertEqual((ms.jev_model("openrouter"), ms.jev_model("typesafe"), ms.fish_model(), ms.whisper_model(),
                              ms.ocr_level()), ("typesafe/jev-1.13", "jev-1.13.0", "s2.1-pro-free", "small.en", "accurate"))
            store.stringForKey_.return_value = "not a real choice!"
            self.assertEqual((ms.jev_model("typesafe"), ms.fish_model(), ms.whisper_model(), ms.ocr_level()),
                             ("jev-1.13.0", "s2.1-pro-free", "small.en", "accurate"))
            for bad in (lambda: ms.save_fish_model("x"), lambda: ms.save_whisper_model("large"),
                        lambda: ms.save_ocr_level("slow"), lambda: ms.save_jev_model("typesafe", "a b")):
                with self.assertRaises(ValueError):
                    bad()
            store.setObject_forKey_.assert_not_called()

    def test_siri_reads_the_chosen_models(self):
        with patch.object(siri.model_settings, "jev_model", lambda p: f"custom-{p}"):
            self.assertEqual(siri.jev_route("openrouter")[1], "custom-openrouter")
            self.assertEqual(siri.jev_route("typesafe")[1], "custom-typesafe")
        with patch.object(siri.model_settings, "fish_model", return_value="s1"), \
                patch.object(siri.model_settings, "voice", return_value={"id": "voiceaaaa01", "title": "A"}), \
                patch.object(siri.requests, "post") as post, patch("os.path.exists", return_value=False), \
                patch("os.makedirs"), patch("builtins.open", create=True):
            post.return_value.content = b"RIFF"
            siri.fetch_tts("hi", key="k")
        self.assertEqual(post.call_args.kwargs["headers"]["model"], "s1")


if __name__ == "__main__":
    unittest.main()
