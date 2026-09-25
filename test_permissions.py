"""Permissions: status mapping for every API value, and the Settings pane's buttons wired to stubbed APIs.
No real prompt, no System Settings, no window on screen."""
import unittest
from unittest.mock import MagicMock, patch

import assistant_ui
import permissions
from test_settings_closure import Base, pump


class MappingTests(unittest.TestCase):
    def test_av_every_value(self):
        self.assertEqual([permissions.av_status(v) for v in (0, 1, 2, 3, 9, None)],
                         ["not_asked", "restricted", "denied", "granted", "unknown", "unknown"])

    def test_speech_every_value(self):
        self.assertEqual([permissions.speech_status(v) for v in (0, 1, 2, 3, 9, None)],
                         ["not_asked", "denied", "restricted", "granted", "unknown", "unknown"])

    def test_trusted_yes_no_only(self):
        self.assertEqual([permissions.trusted_status(v) for v in (True, False, None, 1)],
                         ["granted", "denied", "unknown", "unknown"])

    def test_ae_every_value(self):
        self.assertEqual([permissions.ae_status(v) for v in (0, -1743, -1744, -600, -50, None)],
                         ["granted", "denied", "not_asked", "not_running", "unknown", "unknown"])

    def test_every_status_has_a_label(self):
        for status in ("granted", "denied", "not_asked", "restricted", "not_running", "unknown"):
            self.assertIn(status, permissions.LABELS)

    def test_rows_in_order_with_automation_per_target(self):
        keys = [r[0] for r in permissions.rows()]
        self.assertEqual(keys, ["microphone", "dictation", "accessibility", "screen", "automation:com.apple.systemevents",
                                "automation:com.spotify.client", "automation:com.apple.Safari",
                                "automation:com.google.Chrome"])

    def test_automation_targets_match_usage_description(self):
        with open("setup.py") as f:
            usage = next(line for line in f if "NSAppleEventsUsageDescription" in line)
        for _bundle, name, _why in permissions.AUTOMATION:
            self.assertIn(name.replace("Google ", ""), usage)

    def test_deep_links(self):
        base = "x-apple.systempreferences:com.apple.preference.security?Privacy_"
        self.assertEqual({k: permissions.settings_url(k) for k in ("microphone", "dictation", "accessibility", "screen",
                                                                    "automation:com.spotify.client")},
                         {"microphone": base + "Microphone", "dictation": base + "SpeechRecognition",
                          "accessibility": base + "Accessibility", "screen": base + "ScreenCapture",
                          "automation:com.spotify.client": base + "Automation"})

    def test_status_reads_each_api_without_asking(self):
        with patch.object(permissions, "raw_microphone", return_value=3), \
                patch.object(permissions, "raw_dictation", return_value=0), \
                patch.object(permissions, "raw_accessibility", return_value=False), \
                patch.object(permissions, "raw_screen", return_value=True), \
                patch.object(permissions, "raw_automation", side_effect=lambda b, ask=False: {
                    "com.apple.systemevents": -600, "com.spotify.client": 0, "com.apple.Safari": -1744,
                    "com.google.Chrome": -1743}[b]) as ae:
            snap = permissions.snapshot()
        self.assertEqual(snap, {"microphone": "granted", "dictation": "not_asked", "accessibility": "denied",
                                "screen": "granted", "automation:com.apple.systemevents": "not_running",
                                "automation:com.spotify.client": "granted", "automation:com.apple.Safari": "not_asked",
                                "automation:com.google.Chrome": "denied"})
        self.assertTrue(all(c.kwargs.get("ask", False) is False and len(c.args) == 1 for c in ae.call_args_list))

    def test_error_reads_unknown_never_granted(self):
        with patch.object(permissions, "raw_microphone", side_effect=ImportError("AVFoundation")):
            self.assertEqual(permissions.status("microphone"), "unknown")

    def test_request_routes_each_key(self):
        done = MagicMock()
        stubs = {n: MagicMock(side_effect=lambda d: d()) for n in
                 ("ask_microphone", "ask_dictation", "ask_accessibility", "ask_screen")}
        with patch.multiple(permissions, **stubs):
            for key in ("microphone", "dictation", "accessibility", "screen"):
                self.assertTrue(permissions.request(key, done))
        for stub in stubs.values():
            stub.assert_called_once()
        self.assertEqual(done.call_count, 4)

    def test_automation_request_never_launches(self):
        with patch.object(permissions, "running", return_value=False), \
                patch.object(permissions, "ask_automation") as ask:
            self.assertFalse(permissions.request("automation:com.spotify.client", MagicMock()))
        ask.assert_not_called()
        with patch.object(permissions, "running", return_value=True), \
                patch.object(permissions, "ask_automation") as ask:
            self.assertTrue(permissions.request("automation:com.spotify.client", MagicMock()))
        self.assertEqual(ask.call_args.args[0], "com.spotify.client")

    def test_ask_automation_asks(self):
        with patch.object(permissions, "raw_automation", return_value=0) as raw:
            done = MagicMock()
            permissions.ask_automation("com.apple.Safari", done)
        raw.assert_called_once_with("com.apple.Safari", ask=True)
        done.assert_called_once()


class RowTextTests(unittest.TestCase):
    def row(self, key, status, running=True, acted=False):
        name = next(r[1] for r in permissions.rows() if r[0] == key)
        return assistant_ui.permission_row_text(key, name, "why", status, running, acted)

    def test_granted_disables_request(self):
        self.assertEqual(self.row("microphone", "granted"), ("Granted", "why", False))

    def test_not_asked_enables_request(self):
        self.assertEqual(self.row("dictation", "not_asked"), ("Not asked yet", "why", True))

    def test_refused_prompt_points_at_settings(self):
        self.assertEqual(self.row("microphone", "denied"), ("Not granted", "Turn it on in System Settings.", False))
        self.assertEqual(self.row("automation:com.apple.Safari", "denied")[2], False)

    def test_ax_and_screen_can_always_be_requested_when_off(self):
        self.assertEqual(self.row("accessibility", "denied"), ("Not granted", "why", True))
        self.assertEqual(self.row("screen", "denied"), ("Not granted", "why", True))

    def test_relaunch_only_after_the_user_acted(self):
        self.assertNotIn("reopen", self.row("screen", "denied", acted=False)[1])
        self.assertIn("reopen", self.row("screen", "denied", acted=True)[1])
        self.assertEqual(self.row("screen", "granted", acted=True)[1], "why")
        self.assertNotIn("reopen", self.row("accessibility", "denied", acted=True)[1])

    def test_not_running_target_says_open_it_first(self):
        self.assertEqual(self.row("automation:com.spotify.client", "not_running", running=False),
                         ("Not running", "Open Spotify, then Request.", False))
        self.assertEqual(self.row("automation:com.google.Chrome", "not_asked", running=False)[1:],
                         ("Open Google Chrome, then Request.", False))
        self.assertIn("first time", self.row("automation:com.apple.systemevents", "not_running", running=False)[1])

    def test_every_line_fits_its_column(self):
        from AppKit import NSFont, NSFontAttributeName, NSString
        font = {NSFontAttributeName: NSFont.systemFontOfSize_(11)}
        width = (assistant_ui.PANE_W - 2 * assistant_ui.GROUP_X - 28 - assistant_ui.PERM_STATUS_W
                 - assistant_ui.PERM_REQUEST_W - assistant_ui.PERM_OPEN_W - 16)
        for key, _name, why, _a in permissions.rows():
            for status in permissions.LABELS:
                for running in (True, False):
                    for acted in (True, False):
                        line = self.row(key, status, running, acted)[1].replace("why", why)
                        self.assertLessEqual(NSString.stringWithString_(line).sizeWithAttributes_(font).width, width,
                                             line)

    def test_restricted(self):
        self.assertEqual(self.row("microphone", "restricted")[0::2], ("Restricted", False))


SNAP = {"microphone": "granted", "dictation": "not_asked", "accessibility": "denied", "screen": "denied",
        "automation:com.apple.systemevents": "not_running", "automation:com.spotify.client": "not_asked",
        "automation:com.apple.Safari": "granted", "automation:com.google.Chrome": "denied"}
UP = {"com.apple.systemevents": False, "com.spotify.client": True, "com.apple.Safari": True,
      "com.google.Chrome": True}


class PaneTests(Base):
    def setUp(self):
        self.snap = dict(SNAP)
        self.extra = [patch.object(permissions, "snapshot", side_effect=lambda: dict(self.snap)),
                      patch.object(permissions, "running", side_effect=lambda b: UP[b]),
                      patch.object(assistant_ui, "run_async", lambda work: work()),
                      patch.object(assistant_ui, "open_url", MagicMock())]
        super().setUp()
        for p in self.extra:  # on top of Base's stubs, removed before them
            p.start()
        self.d._refresh_permissions()
        self.painted()

    def tearDown(self):
        for p in reversed(self.extra):
            p.stop()
        super().tearDown()

    def painted(self):
        return pump(lambda: self.d.perm_status == self.snap)

    def rows(self):
        return self.d.perm_rows

    def test_every_row_has_status_and_both_buttons(self):
        self.assertEqual(list(self.rows()), [r[0] for r in permissions.rows()])
        for key, row in self.rows().items():
            self.assertEqual(row["request"].action(), "requestPermission:")
            self.assertEqual(row["open"].action(), "openPermissionPane:")
            self.assertIs(row["request"].target(), self.d)
        self.assertEqual(self.rows()["microphone"]["status"].stringValue(), "Granted")
        self.assertFalse(self.rows()["microphone"]["request"].isEnabled())
        self.assertEqual(self.rows()["dictation"]["status"].stringValue(), "Not asked yet")
        self.assertTrue(self.rows()["dictation"]["request"].isEnabled())
        self.assertEqual(self.rows()["automation:com.google.Chrome"]["status"].stringValue(), "Not granted")

    def test_pane_is_listed_and_fits(self):
        self.assertIn("permissions", [p[0] for p in assistant_ui.SETTINGS_PANES])
        pane = self.d.pane_views["permissions"]
        self.assertLessEqual(assistant_ui.content_bottom(pane), assistant_ui.PANE_H)

    def test_request_asks_that_permission_then_rereads(self):
        self.snap["dictation"] = "granted"
        with patch.object(permissions, "request", side_effect=lambda k, done: done() or True) as req:
            self.d.requestPermission_(self.rows()["dictation"]["request"])
            self.assertTrue(self.painted())
        self.assertEqual(req.call_args.args[0], "dictation")
        self.assertEqual(self.rows()["dictation"]["status"].stringValue(), "Granted")

    def test_each_button_requests_its_own_key(self):
        with patch.object(permissions, "request", return_value=True) as req:
            for key, row in self.rows().items():
                row["request"].setEnabled_(True)
                self.d.requestPermission_(row["request"])
        self.assertEqual([c.args[0] for c in req.call_args_list], list(self.rows()))

    def test_open_settings_opens_its_pane_and_counts_as_acted(self):
        self.d.openPermissionPane_(self.rows()["screen"]["open"])
        assistant_ui.open_url.assert_called_once_with(permissions.settings_url("screen"))
        self.assertNotIn("reopen", self.rows()["screen"]["why"].stringValue())
        self.d._refresh_permissions()  # coming back: still off, and the user acted
        self.assertTrue(pump(lambda: "reopen" in self.rows()["screen"]["why"].stringValue()))

    def test_focus_rereads_without_timers(self):
        self.snap["accessibility"] = "granted"
        note = MagicMock()
        note.object.return_value = self.d.settings_sheet
        self.d.windowDidBecomeKey_(note)
        self.assertTrue(self.painted())
        self.assertEqual(self.rows()["accessibility"]["status"].stringValue(), "Granted")

    def test_other_windows_focus_does_not_read(self):
        note = MagicMock()
        note.object.return_value = object()
        before = permissions.snapshot.call_count
        self.d.windowDidBecomeKey_(note)
        self.assertEqual(permissions.snapshot.call_count, before)

    def test_late_read_never_paints_over_newer(self):
        self.d.ops["perm"] = 999999999
        self.d.permissionsRead_({"op": 1, "status": {k: "granted" for k in SNAP}, "running": UP})
        self.assertEqual(self.rows()["dictation"]["status"].stringValue(), "Not asked yet")

    def test_request_error_is_shown_not_raised(self):
        with patch.object(permissions, "request", side_effect=RuntimeError("boom")):
            self.d.requestPermission_(self.rows()["microphone"]["request"])
        self.assertIn("RuntimeError", self.rows()["microphone"]["why"].stringValue())

    def test_status_panel_button_opens_this_pane(self):
        self.d.openPermissionSettings_(None)
        self.assertEqual(self.d.settings_tabs.selectedTabViewItem().identifier(), "permissions")
        self.assertEqual(self.d.settings_sheet.title(), "Permissions")
        assistant_ui.open_url.assert_not_called()


if __name__ == "__main__":
    unittest.main()
