"""Native status, menu bar controls and separate provider/model settings."""
import os
import queue
import sys
import threading

import AppKit
import objc
from AppKit import (
    NSApp, NSApplicationActivationPolicyAccessory, NSStatusBar, NSVariableStatusItemLength,
    NSView, NSSlider, NSSearchField, NSTabView, NSTabViewItem, NSImage,
    NSApplication,
    NSApplicationActivationPolicyRegular,
    NSBackingStoreBuffered,
    NSMenu,
    NSMenuItem,
    NSButton,
    NSPopUpButton,
    NSColor,
    NSEvent,
    NSEventMaskFlagsChanged,
    NSEventModifierFlagOption,
    NSFont,
    NSMakePoint,
    NSMakeRect,
    NSScreen,
    NSSegmentedControl,
    NSWindow,
    NSSecureTextField,
    NSTextField,
    NSVisualEffectBlendingModeBehindWindow,
    NSVisualEffectMaterialHUDWindow,
    NSVisualEffectStateActive,
    NSVisualEffectView,
    NSImageView,
    NSBox,
    NSBezierPath,
    NSInsetRect,
    NSToolbar,
    NSToolbarItem,
    NSImageSymbolConfiguration,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskFullSizeContentView,
    NSWindowStyleMaskTitled,
)
from AppKit import NSPopover, NSViewController
from Foundation import NSObject, NSTimer, NSUserDefaults
from actions import EFFECT_LABELS, EFFECTS
from model_settings import (PREFS, PARAMETERS, TIEBREAK_MAX, TIEBREAK_MIN, answer_settings, cached_models, confirm_policy,
                            fetch_models, save_answer_settings, save_confirm_policy, save_tiebreak_threshold,
                            save_transcription_backend, tiebreak_threshold, transcription_backend, validate_parameters,
                            BACKENDS, save_wake_settings, wake_settings)
import voice_output
from secrets_store import KEY_NAMES, get_secret, get_setting, missing_secrets, save_secret


WIDTH, BASE_HEIGHT, ROW = 400, 170, 26
STICK_TOP, STICK_BOTTOM = 8, 32  # NSViewMinYMargin, NSViewMaxYMargin
NORMAL, FLOATING = 0, 3  # NSNormalWindowLevel, NSFloatingWindowLevel
def hint(mode):
    """What to do next in this listening mode, with the wake phrase the user chose."""
    if mode == "ptt":
        return "Hold right Option to talk"
    import wake
    return wake.Wake(*wake_settings()).hint_text()
MODES = ("ptt", "wake")

STATUS_COLORS = {
    "Starting": NSColor.systemOrangeColor(),
    "Ready": NSColor.systemGreenColor(),
    "Listening": NSColor.systemRedColor(),
    "Transcribing": NSColor.systemBlueColor(),
    "Thinking": NSColor.systemPurpleColor(),
    "Doing it": NSColor.systemOrangeColor(),
    "Speaking": NSColor.systemTealColor(),
    "Something went wrong": NSColor.systemRedColor(),
    "Time's up": NSColor.systemYellowColor(),
}


STATE_SYMBOLS = {
    "Starting": "hourglass", "Ready": "checkmark", "Listening": "waveform", "Transcribing": "text.bubble",
    "Thinking": "sparkles", "Doing it": "bolt.fill", "Speaking": "speaker.wave.2.fill",
    "Something went wrong": "exclamationmark.triangle.fill", "Time's up": "bell.fill", "Paused": "pause.fill",
    "Dictation unavailable": "mic.slash.fill", "Couldn't hear that": "ear", "Mic test": "mic.fill",
}


def symbol(name, size, weight=0.23):
    image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    config = NSImageSymbolConfiguration.configurationWithPointSize_weight_(size, weight)
    return image.imageWithSymbolConfiguration_(config) if image else None


def symbol_button(name, target, action, tip, size=15):
    button = NSButton.buttonWithImage_target_action_(symbol(name, size), target, action)
    button.setBordered_(False)
    button.setToolTip_(tip)
    button.setAccessibilityLabel_(tip)
    button.setContentTintColor_(NSColor.secondaryLabelColor())
    return button


def text(value, frame, size, color=None, weight=0.0):
    """Plain system text: regular weight unless asked, one line, tail truncation."""
    view = label(value, frame, size, color)
    view.setFont_(NSFont.systemFontOfSize_weight_(size, weight))
    return view


def glass_backdrop(window, content):
    """Liquid Glass (NSGlassEffectView, macOS 26+) behind the content; the older HUD material elsewhere."""
    glass_class = objc.lookUpClass("NSGlassEffectView") if hasattr(AppKit, "NSGlassEffectView") else None
    if glass_class is not None:
        window.setOpaque_(False)
        window.setBackgroundColor_(NSColor.clearColor())
        glass = glass_class.alloc().initWithFrame_(content.frame())
        glass.setCornerRadius_(26)
        glass.setContentView_(content)
        glass.setAutoresizingMask_(18)
        return glass
    backdrop = NSVisualEffectView.alloc().initWithFrame_(content.frame())
    backdrop.setMaterial_(NSVisualEffectMaterialHUDWindow)
    backdrop.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
    backdrop.setState_(NSVisualEffectStateActive)
    backdrop.addSubview_(content)
    return backdrop


PANE_W, PANE_H, FOOTER_H = 580, 560, 56
GROUP_X, CONTROL_W = 20, 250
SETTINGS_PANES = (("providers", "Providers", "key.fill"), ("answers", "Answers", "sparkles"),
                  ("confirm", "Confirmations", "checkmark.shield"), ("transcription", "Transcription", "waveform"))


class FlippedView(NSView):
    """Top-down layout for forms."""
    def isFlipped(self):
        return True


class GroupView(FlippedView):
    """Rounded inset panel; label-colour tints keep it right in light and dark."""
    def drawRect_(self, _rect):
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(NSInsetRect(self.bounds(), 0.5, 0.5), 10, 10)
        NSColor.labelColor().colorWithAlphaComponent_(0.045).setFill()
        path.fill()
        NSColor.labelColor().colorWithAlphaComponent_(0.08).setStroke()
        path.setLineWidth_(1)
        path.stroke()


def form_group(parent, top, header, rows, row_h=40):
    """A System Settings style inset group: small header, rounded background, one row per (title, control) with
    the title left and the control right-aligned, hairline separators. Returns the y below it."""
    width = PANE_W - 2 * GROUP_X
    if header:
        parent.addSubview_(text(header, NSMakeRect(GROUP_X + 6, top, width, 18), 12,
                                NSColor.secondaryLabelColor(), weight=0.3))
        top += 22
    box = GroupView.alloc().initWithFrame_(NSMakeRect(GROUP_X, top, width, row_h * len(rows)))
    parent.addSubview_(box)
    for i, (title, control) in enumerate(rows):
        y = i * row_h
        if title:
            box.addSubview_(text(title, NSMakeRect(14, y + (row_h - 17) / 2, width - CONTROL_W - 40, 17), 13))
        f = control.frame()
        control.setFrame_(NSMakeRect(width - 14 - f.size.width, y + (row_h - f.size.height) / 2, f.size.width,
                                     f.size.height))
        box.addSubview_(control)
        if i:
            line = NSBox.alloc().initWithFrame_(NSMakeRect(14, y, width - 28, 1))
            line.setBoxType_(2)  # separator
            box.addSubview_(line)
    return top + row_h * len(rows) + 22


def footnote(parent, top, value):
    view = text(value, NSMakeRect(GROUP_X + 6, top - 12, PANE_W - 2 * GROUP_X - 12, 32), 11,
                NSColor.secondaryLabelColor())
    view.setLineBreakMode_(0)  # wrap
    view.cell().setWraps_(True)
    parent.addSubview_(view)


def label(text, frame, size, color=None):
    view = NSTextField.labelWithString_(text)
    view.setFrame_(frame)
    view.setFont_(NSFont.systemFontOfSize_weight_(size, 0.5))
    view.setTextColor_(color or NSColor.labelColor())
    view.setLineBreakMode_(4)
    return view


class AppDelegate(NSObject):
    def applicationDidFinishLaunching_(self, _notification):
        self.controls = queue.Queue()
        self.option_down = False
        self.worker_started = False
        self.listening = not PREFS.boolForKey_("listening_paused")
        self.menu_only = PREFS.boolForKey_("menu_only")
        self.catalog = cached_models()
        self.fetch_generation = 0
        self.mode = NSUserDefaults.standardUserDefaults().stringForKey_("mode") or "ptt"
        if self.mode not in MODES:
            self.mode = "ptt"
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable
                 | NSWindowStyleMaskFullSizeContentView)
        self.panel = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, BASE_HEIGHT), style, NSBackingStoreBuffered, False
        )
        self.panel.setTitle_("Hey Jev")
        self.panel.setTitleVisibility_(1)  # hidden: the status itself is the headline
        self.panel.setTitlebarAppearsTransparent_(True)
        self.panel.setMovableByWindowBackground_(True)
        self.panel.setReleasedWhenClosed_(False)  # closing just hides it, the Dock icon brings it back
        self.on_top = NSUserDefaults.standardUserDefaults().boolForKey_("keep_on_top")
        self.panel.setLevel_(FLOATING if self.on_top else NORMAL)
        self._add_window_menu()

        background = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, BASE_HEIGHT))
        background.setAutoresizingMask_(18)  # grow with the window
        self.panel.setContentView_(glass_backdrop(self.panel, background))
        self.background = background
        self.timer_rows = []

        # Headline: a tinted symbol badge, the state, and one line of detail.
        self.badge = NSView.alloc().initWithFrame_(NSMakeRect(20, BASE_HEIGHT - 94, 44, 44))
        self.badge.setWantsLayer_(True)
        self.badge.layer().setCornerRadius_(22)
        self.badge_icon = NSImageView.alloc().initWithFrame_(NSMakeRect(10, 10, 24, 24))
        self.badge.addSubview_(self.badge_icon)
        self.status = text("Starting", NSMakeRect(76, BASE_HEIGHT - 74, WIDTH - 96, 26), 20, weight=0.3)
        self.detail = text("Loading Whisper…", NSMakeRect(76, BASE_HEIGHT - 96, WIDTH - 96, 20), 13,
                           NSColor.secondaryLabelColor())
        self.hint = text(hint(self.mode), NSMakeRect(0, 0, 10, 10), 12)  # kept for state, shown via detail
        for view in (self.badge, self.status, self.detail):
            view.setAutoresizingMask_(STICK_TOP)
            background.addSubview_(view)
        self.dot = self.badge  # updateStatus_ paints through _paint_state

        settings = symbol_button("gearshape", self, "showSettings:", "Settings", 17)
        settings.setFrame_(NSMakeRect(WIDTH - 42, BASE_HEIGHT - 30, 28, 28))  # level with the traffic lights
        settings.setAutoresizingMask_(STICK_TOP)
        background.addSubview_(settings)

        # Control bar: mode, voice, pause. Icons carry the meaning; tooltips and accessibility labels spell it out.
        self.mode_switch = NSSegmentedControl.segmentedControlWithLabels_trackingMode_target_action_(
            ["Hold Option", "Hey Jev"], 0, self, "modeChanged:"
        )
        self.mode_switch.setFrame_(NSMakeRect(16, 18, 184, 28))
        self.mode_switch.setSelectedSegment_(MODES.index(self.mode))
        self.mode_switch.setToolTip_("Hold right Option to talk, or say \u201cHey Jev\u201d")
        self.mute_button = symbol_button("speaker.wave.2.fill", self, "toggleVoice:", "Mute Jev's voice", 14)
        self.mute_button.setFrame_(NSMakeRect(212, 19, 26, 26))
        self.voice_slider = self._slider(NSMakeRect(240, 20, 96, 24))
        self.voice_slider.setControlSize_(1)
        self.voice_label = text("Voice", NSMakeRect(0, 0, 10, 10), 12)  # value lives in the slider tooltip
        self.pause_button = symbol_button("pause.fill", self, "toggleListening:", "Pause listening", 13)
        self.pause_button.setBordered_(True)
        self.pause_button.setBezelStyle_(7)  # circular
        self.pause_button.setFrame_(NSMakeRect(WIDTH - 50, 16, 34, 32))
        for view in (self.mode_switch, self.mute_button, self.voice_slider, self.pause_button):
            view.setAutoresizingMask_(STICK_BOTTOM)
            background.addSubview_(view)
        self._build_status_menu()
        self._sync_controls()
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.5, self, "tick:", None, True)

        screen = NSScreen.mainScreen().visibleFrame()
        self.panel.setFrameOrigin_(NSMakePoint(screen.origin.x + (screen.size.width - WIDTH) / 2,
                                               screen.origin.y + screen.size.height - 300))
        if self.menu_only:
            NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        else:
            self.showMain_(None)
        self.global_monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            NSEventMaskFlagsChanged, self._global_flags_changed
        )
        self.local_monitor = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            NSEventMaskFlagsChanged, self._local_flags_changed
        )
        if missing_secrets():
            self.updateStatus_({"state": "Starting", "detail": "Add your API keys to begin"})
            self.performSelector_withObject_afterDelay_("showSettings:", None, 0.3)
        else:
            self._start_worker()

    @objc.python_method
    def _global_flags_changed(self, event):
        self._handle_flags(event)

    @objc.python_method
    def _local_flags_changed(self, event):
        self._handle_flags(event)
        return event

    @objc.python_method
    def _handle_flags(self, event):
        if not self.listening or event.keyCode() != 61:
            return
        is_down = bool(event.modifierFlags() & NSEventModifierFlagOption)
        if is_down != self.option_down:
            self.option_down = is_down
            self.controls.put("press" if is_down else "release")

    @objc.python_method
    def _start_worker(self):
        if self.worker_started:
            return
        self.worker_started = True
        threading.Thread(target=self._run_assistant, daemon=True).start()

    @objc.python_method
    def _run_assistant(self):
        from siri import run_voice_assistant
        try:
            run_voice_assistant(self.notify, self.controls, self.mode, self.listening, ask=self.ask_confirm)
        except Exception as exc:
            self.notify("Something went wrong", str(exc))

    @objc.python_method
    def _add_window_menu(self):
        item = NSMenuItem.alloc().init()
        NSApp.mainMenu().addItem_(item)
        menu = NSMenu.alloc().initWithTitle_("Window")
        menu.addItemWithTitle_action_keyEquivalent_("Minimize", "performMiniaturize:", "m")
        self.on_top_item = menu.addItemWithTitle_action_keyEquivalent_("Keep on Top", "toggleOnTop:", "t")
        self.on_top_item.setTarget_(self)
        self.on_top_item.setState_(1 if self.on_top else 0)
        menu.addItemWithTitle_action_keyEquivalent_("Show Hey Jev", "showMain:", "1").setTarget_(self)
        item.setSubmenu_(menu)
        NSApp.setWindowsMenu_(menu)

    def toggleOnTop_(self, _sender):
        self.on_top = not self.on_top
        NSUserDefaults.standardUserDefaults().setBool_forKey_(self.on_top, "keep_on_top")
        self.panel.setLevel_(FLOATING if self.on_top else NORMAL)
        self.on_top_item.setState_(1 if self.on_top else 0)

    def showMain_(self, _sender):
        self.panel.deminiaturize_(None)
        self.panel.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    def applicationShouldHandleReopen_hasVisibleWindows_(self, _app, _visible):
        self.showMain_(None)
        return True

    def modeChanged_(self, sender):
        self.mode = MODES[sender.selectedSegment()]
        NSUserDefaults.standardUserDefaults().setObject_forKey_(self.mode, "mode")
        self.hint.setStringValue_(hint(self.mode))
        self.controls.put(("mode", self.mode))
        self._sync_controls()

    @objc.python_method
    def _slider(self, frame):
        slider = NSSlider.alloc().initWithFrame_(frame)
        slider.setMinValue_(0)
        slider.setMaxValue_(1)
        slider.setDoubleValue_(voice_output.volume())
        slider.setContinuous_(True)
        slider.setTarget_(self)
        slider.setAction_("voiceVolumeChanged:")
        slider.setAccessibilityLabel_("Voice output volume")
        return slider

    @objc.python_method
    def _build_status_menu(self):
        self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        self.status_item.button().setTitle_("Jev")
        self.status_item.button().setAccessibilityLabel_("Hey Jev controls")
        menu = NSMenu.alloc().initWithTitle_("Hey Jev")
        menu.setAutoenablesItems_(False)
        self.menu_status = menu.addItemWithTitle_action_keyEquivalent_("Starting", None, "")
        self.menu_status.setEnabled_(False)
        menu.addItem_(NSMenuItem.separatorItem())
        self.pause_item = menu.addItemWithTitle_action_keyEquivalent_("Pause listening", "toggleListening:", "")
        self.pause_item.setTarget_(self)
        self.mode_items = []
        for title in ("Hold right Option", "Hey Jev wake phrase"):
            item = menu.addItemWithTitle_action_keyEquivalent_(title, "menuMode:", "")
            item.setTarget_(self)
            item.setTag_(len(self.mode_items))
            self.mode_items.append(item)
        menu.addItem_(NSMenuItem.separatorItem())
        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 270, 62))
        self.menu_voice_label = label("Voice volume", NSMakeRect(18, 34, 235, 20), 12)
        view.addSubview_(self.menu_voice_label)
        self.menu_voice_slider = self._slider(NSMakeRect(18, 8, 235, 25))
        view.addSubview_(self.menu_voice_slider)
        item = NSMenuItem.alloc().init()
        item.setView_(view)
        menu.addItem_(item)
        self.mute_item = menu.addItemWithTitle_action_keyEquivalent_("Mute voice", "toggleVoice:", "")
        self.mute_item.setTarget_(self)
        hint = menu.addItemWithTitle_action_keyEquivalent_("Timer chimes remain audible", None, "")
        hint.setEnabled_(False)
        menu.addItem_(NSMenuItem.separatorItem())
        for title, action in (("Show status", "showMain:"), ("Settings…", "showSettings:")):
            menu.addItemWithTitle_action_keyEquivalent_(title, action, "").setTarget_(self)
        self.menu_only_item = menu.addItemWithTitle_action_keyEquivalent_("Menu bar only", "toggleMenuOnly:", "")
        self.menu_only_item.setTarget_(self)
        menu.addItem_(NSMenuItem.separatorItem())
        menu.addItemWithTitle_action_keyEquivalent_("Quit Hey Jev", "terminate:", "q").setTarget_(NSApp)
        self.status_item.setMenu_(menu)

    @objc.python_method
    def _sync_controls(self):
        self.mode_switch.setSelectedSegment_(MODES.index(self.mode))
        for i, item in enumerate(self.mode_items):
            item.setState_(int(MODES[i] == self.mode))
        self.pause_item.setTitle_("Pause listening" if self.listening else "Resume listening")
        self.pause_button.setImage_(symbol("pause.fill" if self.listening else "play.fill", 13))
        self.pause_button.setToolTip_("Pause listening" if self.listening else "Resume listening")
        self.pause_button.setAccessibilityLabel_(self.pause_button.toolTip())
        self.hint.setStringValue_(hint(self.mode) if self.listening else "Microphone paused · timers remain active")
        self.menu_only_item.setState_(int(self.menu_only))
        gain, mute = voice_output.volume(), voice_output.muted()
        self.voice_slider.setDoubleValue_(gain)
        self.menu_voice_slider.setDoubleValue_(gain)
        self.voice_label.setStringValue_(f"Voice {round(gain * 100)}%")
        self.voice_slider.setToolTip_(f"Jev's voice volume · {round(gain * 100)}%")
        self.menu_voice_label.setStringValue_(f"Voice volume · {round(gain * 100)}%" + (" · off" if mute else ""))
        self.mute_button.setImage_(symbol("speaker.slash.fill" if mute else "speaker.wave.2.fill", 14))
        self.mute_button.setToolTip_("Unmute Jev's voice" if mute else "Mute Jev's voice")
        self.mute_button.setAccessibilityLabel_(self.mute_button.toolTip())
        self.voice_slider.setEnabled_(not mute)
        self.mute_item.setState_(int(mute))

    def menuMode_(self, sender):
        self.mode_switch.setSelectedSegment_(sender.tag())
        self.modeChanged_(self.mode_switch)

    def toggleListening_(self, _sender):
        self.listening = not self.listening
        self.option_down = False
        PREFS.setBool_forKey_(not self.listening, "listening_paused")
        self.controls.put(("listening", self.listening))
        self._sync_controls()
        self.updateStatus_({"state": "Ready" if self.listening else "Paused", "detail": hint(self.mode) if self.listening else "Microphone paused. Current action may finish."})

    def voiceVolumeChanged_(self, sender):
        voice_output.configure(gain=sender.doubleValue())
        self._sync_controls()

    def toggleVoice_(self, _sender):
        voice_output.configure(mute=not voice_output.muted())
        self._sync_controls()

    def toggleMenuOnly_(self, _sender):
        self.menu_only = not self.menu_only
        PREFS.setBool_forKey_(self.menu_only, "menu_only")
        NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory if self.menu_only else NSApplicationActivationPolicyRegular)
        if self.menu_only:
            self.panel.orderOut_(None)
        else:
            self.showMain_(None)
        self._sync_controls()

    def showSettings_(self, _sender):
        try:
            self._show_settings()
        except Exception as exc:  # a Python error inside an AppKit callback would otherwise kill the app
            self.notify("Something went wrong", str(exc))

    @objc.python_method
    def _show_settings(self):
        if getattr(self, "settings_sheet", None):
            self.settings_sheet.makeKeyAndOrderFront_(None)
            NSApp.activateIgnoringOtherApps_(True)
            return
        sheet = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, PANE_W, PANE_H + FOOTER_H),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable, NSBackingStoreBuffered, False)
        sheet.setReleasedWhenClosed_(False)
        sheet.setDelegate_(self)  # the close button discards like Cancel, see windowWillClose_
        sheet.setToolbarStyle_(2)  # NSWindowToolbarStylePreference: icon tabs, like System Settings panes
        content = sheet.contentView()
        tabs = NSTabView.alloc().initWithFrame_(NSMakeRect(0, FOOTER_H, PANE_W, PANE_H))
        tabs.setTabViewType_(6)  # no tabs, no border: the toolbar picks the pane
        content.addSubview_(tabs)
        self.settings_tabs = tabs
        panes = {}
        for ident, title, _icon in SETTINGS_PANES:
            view = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, PANE_W, PANE_H))
            item = NSTabViewItem.alloc().initWithIdentifier_(ident)
            item.setLabel_(title)
            item.setView_(view)
            tabs.addTabViewItem_(item)
            panes[ident] = view
        toolbar = NSToolbar.alloc().initWithIdentifier_("HeyJevSettings")
        toolbar.setDelegate_(self)
        toolbar.setDisplayMode_(1)  # icon and label
        sheet.setToolbar_(toolbar)
        toolbar.setSelectedItemIdentifier_(SETTINGS_PANES[0][0])
        sheet.setTitle_(SETTINGS_PANES[0][1])

        # Providers: one source + its key for Jev, one provider + key for deeper answers, the voice key.
        p = panes["providers"]
        self.key_fields = {}

        def key_field(key):
            field = NSSecureTextField.alloc().initWithFrame_(NSMakeRect(0, 0, CONTROL_W, 22))
            field.setPlaceholderString_("Saved in Keychain" if get_secret(key) else "Paste key")
            field.setBezelStyle_(1)  # rounded
            self.key_fields[key] = field
            return field
        self.jev_provider = self._popup(None, ["OpenRouter", "TypeSafe"], NSMakeRect(0, 0, CONTROL_W, 24), "jevSourceChanged:")
        self.jev_provider.selectItemAtIndex_(0 if get_setting("JEV_PROVIDER") == "openrouter" else 1)
        jev_keys = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, CONTROL_W, 22))
        for key in ("JEV_OPENROUTER_API_KEY", "TYPESAFE_API_KEY"):
            jev_keys.addSubview_(key_field(key))  # stacked; only the selected source's key shows
        y = form_group(p, 20, "Jev decisions", [("Source", self.jev_provider), ("API key", jev_keys)])
        self.answer_provider = self._popup(None, ["Off", "OpenRouter"], NSMakeRect(0, 0, CONTROL_W, 24), "answerSourceChanged:")
        self.answer_provider.selectItemAtIndex_(0 if get_setting("ANSWER_PROVIDER") == "disabled" else 1)
        y = form_group(p, y, "Deeper answers", [("Provider", self.answer_provider),
                                                ("OpenRouter key", key_field("OPENROUTER_API_KEY"))])
        y = form_group(p, y, "Voice", [("Fish Audio key", key_field("FISH_AUDIO_API_KEY"))])
        from_env = [k for k in KEY_NAMES if os.getenv(k)]
        footnote(p, y, "Keys are stored in your Keychain. Leave a field empty to keep the saved key."
                 + (" A .env file is overriding: " + ", ".join(from_env) + "." if from_env else ""))
        self._sync_key_rows()

        # Deeper answers: model, then the advanced request controls.
        current = answer_settings()
        self.selected_model = current["model"]
        self.selected_metadata = current["metadata"]
        self.parameter_drafts = {self.selected_model: current["parameters"]}
        a = panes["answers"]
        self.model_search = NSSearchField.alloc().initWithFrame_(NSMakeRect(0, 0, CONTROL_W, 22))
        self.model_search.setPlaceholderString_("Search models")
        self.model_search.setDelegate_(self)
        self.model_popup = self._popup(None, [], NSMakeRect(0, 0, CONTROL_W, 24), "modelChanged:")
        self.refresh_button = NSButton.buttonWithTitle_target_action_("Refresh", self, "refreshModels:")
        self.catalog_message = text("Saved model selection available without refreshing.", NSMakeRect(0, 0, CONTROL_W, 16),
                                    11, NSColor.secondaryLabelColor())
        self.model_info = text("", NSMakeRect(0, 0, CONTROL_W, 16), 11, NSColor.secondaryLabelColor())
        y = form_group(a, 20, "Model", [("Search", self.model_search), ("Model", self.model_popup),
                                        ("Catalog", self.refresh_button)])
        a.addSubview_(self.catalog_message)
        self.catalog_message.setFrame_(NSMakeRect(GROUP_X + 14, y - 14, PANE_W - 2 * GROUP_X, 16))
        a.addSubview_(self.model_info)
        self.model_info.setFrame_(NSMakeRect(GROUP_X + 14, y + 2, PANE_W - 2 * GROUP_X, 16))
        self.parameter_fields = {}
        rows = []
        for key, (title, kind, low, high) in PARAMETERS.items():
            field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 110, 22))
            field.setPlaceholderString_("Default")
            field.setAlignment_(2)
            field.setToolTip_(f"{key}: {low:g} to {high:g}. Empty uses the default.")
            self.parameter_fields[key] = field
            rows.append((title, field))
        y = form_group(a, y + 28, "Advanced", rows, row_h=32)
        footnote(a, y, "Empty fields use Hey Jev's defaults (80 tokens for answers, 120 for reminders, minimal "
                       "reasoning where supported) and the provider's for the rest.")

        # Confirmations: one row per kind of action.
        c = panes["confirm"]
        policy = confirm_policy()
        self.policy_popups = {}
        rows = []
        for effect in EFFECTS:
            popup = self._popup(None, ["Ask first", "Automatic"], NSMakeRect(0, 0, 140, 24))
            popup.selectItemAtIndex_(0 if policy[effect] == "ask" else 1)
            popup.setAccessibilityLabel_(f"{EFFECT_LABELS[effect]} confirmation")
            self.policy_popups[effect] = popup
            rows.append((EFFECT_LABELS[effect], popup))
        y = form_group(c, 20, "Ask before doing", rows, row_h=34)
        footnote(c, y, "Applies to voice and typed commands. Ask first shows a pop-down from the menu bar.")
        tie_cell = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, CONTROL_W, 24))
        slider = NSSlider.alloc().initWithFrame_(NSMakeRect(0, 0, CONTROL_W - 90, 24))
        slider.setMinValue_(TIEBREAK_MIN)
        slider.setMaxValue_(TIEBREAK_MAX)
        slider.setDoubleValue_(tiebreak_threshold())
        slider.setTarget_(self)
        slider.setAction_("tiebreakChanged:")
        slider.setAccessibilityLabel_("Jev score needed to pick between duplicate apps")
        self.tiebreak_slider = slider
        self.tiebreak_value = text("", NSMakeRect(CONTROL_W - 84, 4, 84, 17), 13, NSColor.secondaryLabelColor())
        self.tiebreak_value.setAlignment_(2)
        for view in (slider, self.tiebreak_value):
            tie_cell.addSubview_(view)
        self.tiebreakChanged_(slider)
        y = form_group(c, y + 28, "Duplicate apps", [("Pick on its own at", tie_cell)], row_h=34)
        footnote(c, y, "A Jev score, not a guarantee. Below it, Jev asks which one. Quitting always asks.")

        # Transcription: backend and its state, then the transcript-only microphone test.
        h = panes["transcription"]
        self.backend_popup = self._popup(None, ["Local Whisper", "Apple on-device"], NSMakeRect(0, 0, CONTROL_W, 24),
                                         "backendChanged:")
        self.backend_popup.selectItemAtIndex_(BACKENDS.index(transcription_backend()))
        status_cell = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, CONTROL_W, 24))
        self.backend_status = text("", NSMakeRect(0, 4, CONTROL_W, 17), 13, NSColor.secondaryLabelColor())
        self.backend_status.setAlignment_(2)
        self.allow_button = NSButton.buttonWithTitle_target_action_("Allow…", self, "allowAppleSpeech:")
        self.allow_button.setFrame_(NSMakeRect(CONTROL_W - 90, 0, 90, 24))
        for view in (self.backend_status, self.allow_button):  # status text, or the Allow button when it's needed
            status_cell.addSubview_(view)
        y = form_group(h, 20, "Speech recognition", [("Recognizer", self.backend_popup), ("Status", status_cell)])
        self.backend_next = text("", NSMakeRect(GROUP_X + 14, y - 14, PANE_W - 2 * GROUP_X, 16), 11,
                                 NSColor.secondaryLabelColor())
        self.backend_restart = text("", NSMakeRect(GROUP_X + 14, y + 2, PANE_W - 2 * GROUP_X, 16), 11,
                                    NSColor.secondaryLabelColor())
        for view in (self.backend_next, self.backend_restart):
            h.addSubview_(view)
        phrase, aliases = wake_settings()
        self.wake_field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, CONTROL_W, 22))
        self.wake_field.setStringValue_(phrase)
        self.wake_field.setPlaceholderString_("Hey Jev")
        self.alias_field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, CONTROL_W, 22))
        self.alias_field.setStringValue_(", ".join(aliases))
        self.alias_field.setPlaceholderString_("Optional, comma-separated")
        self.alias_field.setToolTip_("Other ways the recognizer writes your phrase, from the microphone test. "
                                     "Only these exact spellings count.")
        y = form_group(h, y + 28, "Wake phrase", [("Phrase", self.wake_field), ("Also accept", self.alias_field)])
        footnote(h, y, "Used in Hey Jev mode. Test it below: the test shows what was heard and whether it matched.")
        y += 8
        self.test_button = NSButton.buttonWithTitle_target_action_("Test Microphone", self, "micTest:")
        self.test_result = text("—", NSMakeRect(0, 0, CONTROL_W + 60, 17), 13, NSColor.secondaryLabelColor())
        self.test_result.setAlignment_(2)
        y = form_group(h, y + 28, "Microphone test", [("Record 4 seconds", self.test_button), ("Heard", self.test_result)])
        footnote(h, y, "The test only shows what was heard; nothing runs. Apple dictation stays on this Mac: if it "
                       "isn't available it stays off and says why, and never sends your voice online.")
        self._show_backend()

        self.settings_message = text("", NSMakeRect(20, 18, PANE_W - 240, 20), 12, NSColor.systemRedColor())
        content.addSubview_(self.settings_message)
        for title, action, x in (("Cancel", "closeSettings:", PANE_W - 196), ("Save", "saveSettings:", PANE_W - 104)):
            button = NSButton.buttonWithTitle_target_action_(title, self, action)
            button.setFrame_(NSMakeRect(x, 14, 88, 28))
            button.setKeyEquivalent_("\r" if title == "Save" else "\x1b")
            content.addSubview_(button)
        self.settings_sheet = sheet
        self._filter_models()
        self._display_parameters()
        sheet.center()
        sheet.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)
        if get_secret("OPENROUTER_API_KEY"):
            self.refreshModels_(None)

    # NSToolbarDelegate: the pane switcher.
    def toolbarAllowedItemIdentifiers_(self, _toolbar):
        return [p[0] for p in SETTINGS_PANES]

    def toolbarDefaultItemIdentifiers_(self, _toolbar):
        return [p[0] for p in SETTINGS_PANES]

    def toolbarSelectableItemIdentifiers_(self, _toolbar):
        return [p[0] for p in SETTINGS_PANES]

    def toolbar_itemForItemIdentifier_willBeInsertedIntoToolbar_(self, _toolbar, ident, _insert):
        _i, title, icon = next(p for p in SETTINGS_PANES if p[0] == ident)
        item = NSToolbarItem.alloc().initWithItemIdentifier_(ident)
        item.setLabel_(title)
        item.setImage_(NSImage.imageWithSystemSymbolName_accessibilityDescription_(icon, title))
        item.setTarget_(self)
        item.setAction_("showPane:")
        return item

    def showPane_(self, item):
        ident = item.itemIdentifier()
        self.settings_tabs.selectTabViewItemWithIdentifier_(ident)
        self.settings_sheet.setTitle_(next(p[1] for p in SETTINGS_PANES if p[0] == ident))

    def jevSourceChanged_(self, _sender):
        self._sync_key_rows()

    def answerSourceChanged_(self, _sender):
        self._sync_key_rows()

    @objc.python_method
    def _sync_key_rows(self):
        typesafe = self.jev_provider.indexOfSelectedItem() == 1
        self.key_fields["JEV_OPENROUTER_API_KEY"].setHidden_(typesafe)
        self.key_fields["TYPESAFE_API_KEY"].setHidden_(not typesafe)
        self.key_fields["OPENROUTER_API_KEY"].setEnabled_(self.answer_provider.indexOfSelectedItem() == 1)

    @objc.python_method
    def _show_backend(self):
        """Status of the selected backend, what to do next, and whether a restart is needed to use it."""
        chosen = BACKENDS[self.backend_popup.indexOfSelectedItem()]
        self.allow_button.setHidden_(True)
        if chosen == "whisper":
            status, nxt = "Runs on this Mac", ""
        else:
            try:
                import speech_apple
                state, reason = speech_apple.status("en-US")
            except ImportError:
                state, reason = "missing_bindings", "Apple Speech support isn't installed in this build."
            status = {
                "ready": "Ready", "not_determined": "Needs permission", "denied": "Permission is off",
                "restricted": "Not allowed on this Mac", "unsupported_locale": "Not available for English (US)",
                "no_on_device": "Not available on-device",
                "unavailable": "Temporarily unavailable",
                "missing_bindings": "Not installed in this build",
            }.get(state, "Not ready")
            nxt = {"not_determined": "Click Allow, then approve the macOS prompt.",
                   "denied": "Turn on Hey Jev in System Settings, Privacy & Security, Speech Recognition.",
                   "restricted": "Speech recognition is restricted on this Mac.",
                   }.get(state, "" if state == "ready" else "Listening stays off with this choice until it's ready.")
            self.allow_button.setHidden_(state != "not_determined")
        self.backend_status.setStringValue_(status)
        self.backend_status.setHidden_(not self.allow_button.isHidden())
        self.backend_next.setStringValue_(nxt)
        from siri import STT
        names = {"whisper": "Local Whisper", "apple": "Apple on-device"}
        if STT["switching"]:
            now = "Switching transcription…"
        elif STT["backend"] is None:
            now = ""
        elif STT["blocked"]:
            now = f"In use: {names[STT['backend']]}, but listening is off. {STT['blocked']}"
        else:
            now = f"In use: {names[STT['backend']]}." + (" Save to switch." if STT["backend"] != chosen else "")
        self.backend_restart.setStringValue_(now)

    def backendChanged_(self, _sender):
        self._show_backend()

    def allowAppleSpeech_(self, _sender):
        try:
            import speech_apple
            speech_apple.request_access(
                lambda *_: self.performSelectorOnMainThread_withObject_waitUntilDone_("speechAccessDone:", None, False))
        except Exception as exc:
            self.backend_next.setStringValue_(f"Couldn't ask for access: {exc}")

    def speechAccessDone_(self, _payload):
        from siri import STT
        if self.worker_started and STT["backend"] == "apple" and STT["blocked"]:
            self.controls.put(("transcription", "apple"))  # permission changed: reload instead of asking for a restart
        if getattr(self, "settings_sheet", None):
            self._show_backend()

    def micTest_(self, _sender):
        if not self.worker_started:
            self.test_result.setStringValue_("Hey Jev isn't running yet.")
            return
        self.test_button.setEnabled_(False)
        self.test_result.setStringValue_("Listening for 4 seconds…")
        self.controls.put(("mic_test", lambda r: self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "micTestDone:", r, False)))

    def micTestDone_(self, result):
        if not getattr(self, "settings_sheet", None):
            return
        self.test_button.setEnabled_(True)
        if result.get("error"):
            self.test_result.setStringValue_(f"Test failed: {result['error']}")
        else:
            tested = result["wake"]  # the saved, running phrase the test listened for
            matched = f"heard “{tested}”" if result.get("wake_matched") else f"didn't hear “{tested}”"
            line = f"“{result['text'] or '(nothing)'}” · {matched} · {result['ms']} ms"
            if " ".join(self.wake_field.stringValue().split()) not in ("", tested):
                line += " · Save to test the new phrase"
            self.test_result.setStringValue_(line)
        self.test_result.setToolTip_(self.test_result.stringValue())  # long results and errors stay readable

    def tiebreakChanged_(self, sender):
        v = int(round(sender.doubleValue()))
        self.tiebreak_value.setStringValue_("Always ask" if v >= TIEBREAK_MAX else f"{v}")

    @objc.python_method
    def _popup(self, parent, titles, frame, action=None):
        popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(frame, False)
        popup.addItemsWithTitles_(titles)
        if action:
            popup.setTarget_(self)
            popup.setAction_(action)
        if parent is not None:
            parent.addSubview_(popup)
        return popup

    def windowWillClose_(self, notification):
        if notification.object() is getattr(self, "settings_sheet", None):
            self._discard_settings()

    @objc.python_method
    def _discard_settings(self):
        """Unsaved edits are dropped and any model fetch in flight is ignored when it lands."""
        self.fetch_generation += 1
        self.settings_sheet = None

    def closeSettings_(self, _sender):
        sheet = getattr(self, "settings_sheet", None)
        if sheet:
            self._discard_settings()
            sheet.orderOut_(None)  # hides without windowWillClose_, so no second discard

    @objc.python_method
    def _parameter_values(self):
        return {key: field.stringValue().strip() for key, field in self.parameter_fields.items() if field.isEnabled()}

    def saveSettings_(self, _sender):
        try:
            import wake  # checked first: a bad phrase must not leave a half-saved form
            phrase, aliases = wake.validate(self.wake_field.stringValue()), wake.parse_aliases(self.alias_field.stringValue())
            values = self._parameter_values()
            validate_parameters(values, self.selected_metadata)
            jev_provider = ("openrouter", "typesafe")[self.jev_provider.indexOfSelectedItem()]
            answer_provider = ("disabled", "openrouter")[self.answer_provider.indexOfSelectedItem()]
            required = ["FISH_AUDIO_API_KEY", "TYPESAFE_API_KEY" if jev_provider == "typesafe" else "JEV_OPENROUTER_API_KEY"]
            if answer_provider == "openrouter":
                required.append("OPENROUTER_API_KEY")
            if any(not self.key_fields[key].stringValue().strip() and not get_secret(key) for key in required):
                raise ValueError("Enter the keys required by your selected providers.")
            for key_name in KEY_NAMES:
                value = self.key_fields[key_name].stringValue().strip()
                if value:
                    save_secret(key_name, value)
            save_secret("JEV_PROVIDER", jev_provider)
            save_secret("ANSWER_PROVIDER", answer_provider)
            save_answer_settings(self.selected_model, values, self.selected_metadata)
            save_confirm_policy({e: ("ask", "auto")[p.indexOfSelectedItem()] for e, p in self.policy_popups.items()})
            backend = BACKENDS[self.backend_popup.indexOfSelectedItem()]
            save_transcription_backend(backend)
            if (phrase, aliases) != tuple(wake_settings()):
                save_wake_settings(phrase, aliases)
                if self.worker_started:
                    self.controls.put(("wake_phrase", phrase, aliases))  # applied live, old audio dropped
            from siri import STT
            if self.worker_started and (STT["backend"] != backend or STT["blocked"]):
                self.controls.put(("transcription", backend))  # live switch through the control queue
            save_tiebreak_threshold(self.tiebreak_slider.doubleValue())
            from siri import reload_keys
            reload_keys()
            warn = wake.short_warning(phrase)
            if warn:
                self.settings_message.setStringValue_(warn)  # saved and live; stays open so the warning is seen
                return
            self.closeSettings_(None)
            self._start_worker()
        except Exception as exc:
            self.settings_message.setStringValue_(str(exc))

    def refreshModels_(self, _sender):
        key = self.key_fields["OPENROUTER_API_KEY"].stringValue().strip() or get_secret("OPENROUTER_API_KEY")
        if not key:
            self.catalog_message.setStringValue_("Enter an OpenRouter key in Providers, then refresh.")
            return
        self.fetch_generation += 1
        generation = self.fetch_generation
        self.refresh_button.setEnabled_(False)
        self.catalog_message.setStringValue_("Loading models from OpenRouter…")
        def load():
            try:
                payload = {"generation": generation, "models": fetch_models(key)}
            except Exception:
                payload = {"generation": generation, "error": "Could not load models. Check the answer key or connection, then retry."}
            self.performSelectorOnMainThread_withObject_waitUntilDone_("modelsLoaded:", payload, False)
        threading.Thread(target=load, daemon=True).start()

    def modelsLoaded_(self, payload):
        if not getattr(self, "settings_sheet", None) or payload["generation"] != self.fetch_generation:
            return
        self.refresh_button.setEnabled_(True)
        if "error" in payload:
            self.catalog_message.setStringValue_(payload["error"])
            return
        self.catalog = payload["models"]
        self.catalog_message.setStringValue_(f"{len(self.catalog)} text models available. Selection applies to answers and reminders.")
        self._filter_models()
        match = next((m for m in self.catalog if m["id"] == self.selected_model), None)
        if match:
            self.parameter_drafts[self.selected_model] = self._parameter_values()
            self.selected_metadata = match
            self._display_parameters()

    def controlTextDidChange_(self, notification):
        if notification.object() == getattr(self, "model_search", None):
            self._filter_models()

    @objc.python_method
    def _filter_models(self):
        query = self.model_search.stringValue().lower().strip()
        models = {m["id"]: m for m in self.catalog}
        models.setdefault(self.selected_model, self.selected_metadata)
        self.filtered_models = [m for m in models.values() if query in (m["id"] + " " + m.get("name", "")).lower()]
        self.filtered_models.sort(key=lambda m: m["id"].lower())
        self.model_popup.removeAllItems()
        for model in self.filtered_models:
            self.model_popup.addItemWithTitle_(model["id"])
        ids = [m["id"] for m in self.filtered_models]
        self.model_popup.selectItemAtIndex_(ids.index(self.selected_model) if self.selected_model in ids else -1)
        self.model_popup.setEnabled_(bool(ids))
        self.model_popup.setToolTip_("No matches" if not ids else "Choose a model; filtering does not change the saved selection.")

    def modelChanged_(self, sender):
        index = sender.indexOfSelectedItem()
        if index < 0:
            return
        self.parameter_drafts[self.selected_model] = self._parameter_values()
        self.selected_metadata = self.filtered_models[index]
        self.selected_model = self.selected_metadata["id"]
        self._display_parameters()

    @objc.python_method
    def _display_parameters(self):
        supported = self.selected_metadata.get("supported_parameters", [])
        values = self.parameter_drafts.get(self.selected_model, {})
        for key, field in self.parameter_fields.items():
            field.setEnabled_(key in supported)
            field.setStringValue_(str(values.get(key, "")) if key in supported else "")
            field.setPlaceholderString_("Default" if key in supported else "N/A")
        context = self.selected_metadata.get("context_length")
        self.model_info.setStringValue_(f"Selected: {self.selected_model}" + (f" · {context:,} context tokens" if context else ""))

    @objc.python_method
    def ask_confirm(self, pending):
        """Engine worker thread: show (dict) or close (None) the confirmation pop-down."""
        self.performSelectorOnMainThread_withObject_waitUntilDone_("showConfirm:", pending or {}, False)

    def showConfirm_(self, pending):
        if getattr(self, "confirm_popover", None):
            self.confirm_popover.close()
            self.confirm_popover = None
        if not pending:
            return
        self.confirm_token = pending["token"]
        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 320, 118))
        view.addSubview_(label(pending["text"] + "?", NSMakeRect(16, 72, 290, 30), 16))
        who = "Typed command (jevctl)" if pending.get("source") == "cli" else "Voice command"
        view.addSubview_(label(who + " · cancels itself in 60 s", NSMakeRect(16, 50, 290, 20), 11,
                               NSColor.secondaryLabelColor()))
        for title, action, x, key in (("Cancel", "confirmCancel:", 112, "\x1b"), ("Confirm", "confirmYes:", 212, "\r")):
            button = NSButton.buttonWithTitle_target_action_(title, self, action)
            button.setFrame_(NSMakeRect(x, 10, 94, 32))
            button.setKeyEquivalent_(key)
            view.addSubview_(button)
        controller = NSViewController.alloc().init()
        controller.setView_(view)
        popover = NSPopover.alloc().init()
        popover.setContentViewController_(controller)
        popover.setBehavior_(0)  # application defined: stays until a button or the engine closes it
        NSApp.activateIgnoringOtherApps_(True)
        popover.showRelativeToRect_ofView_preferredEdge_(self.status_item.button().bounds(), self.status_item.button(), 1)
        self.confirm_popover = popover

    def confirmYes_(self, _sender):
        self._decide(True)

    def confirmCancel_(self, _sender):
        self._decide(False)

    @objc.python_method
    def _decide(self, yes):
        siri = sys.modules.get("siri")
        if siri and siri.ENGINE:
            siri.ENGINE.decide(getattr(self, "confirm_token", None), yes)  # a late click is a no-op
        self.showConfirm_({})

    @objc.python_method
    def notify(self, state, detail=""):
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "updateStatus:", {"state": state, "detail": detail}, False
        )

    def tick_(self, _timer):
        siri = sys.modules.get("siri")
        timers = siri.timer_snapshot()[:3] if siri else []
        if len(timers) != len(self.timer_rows):
            self._layout_timer_rows(len(timers))
        for (name_view, time_view), (name, left) in zip(self.timer_rows, timers):
            name_view.setStringValue_(name)
            m, sec = divmod(int(left + 0.999), 60)
            h, m = divmod(m, 60)
            time_view.setStringValue_(f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}")

    @objc.python_method
    def _layout_timer_rows(self, count):
        for name_view, time_view in self.timer_rows:
            name_view.removeFromSuperview()
            time_view.removeFromSuperview()
        self.timer_rows = []
        frame = self.panel.frame()
        height = BASE_HEIGHT + ROW * count + (8 if count else 0)
        top = frame.origin.y + frame.size.height
        self.panel.setFrame_display_animate_(NSMakeRect(frame.origin.x, top - height, frame.size.width, height), True, True)
        for i in range(count):
            y = 62 + ROW * (count - 1 - i)  # soonest on top, just above the control bar
            name_view = text("", NSMakeRect(76, y, WIDTH - 200, 20), 13, NSColor.secondaryLabelColor())
            time_view = text("", NSMakeRect(WIDTH - 118, y, 100, 20), 15, NSColor.systemTealColor())
            time_view.setFont_(NSFont.monospacedDigitSystemFontOfSize_weight_(15, 0.3))
            time_view.setAlignment_(2)  # right
            for v in (name_view, time_view):
                v.setAutoresizingMask_(STICK_BOTTOM)
                self.background.addSubview_(v)
            self.timer_rows.append((name_view, time_view))

    def updateStatus_(self, payload):
        state = str(payload["state"])
        detail = str(payload.get("detail", ""))
        if not self.listening and state in ("Ready", "Listening"):
            state, detail = "Paused", "Microphone paused. Current action may finish."
        self.menu_status.setTitle_(state)
        self.status_item.button().setToolTip_(f"Hey Jev: {state}")
        self.status.setStringValue_(state)
        self.detail.setStringValue_(detail)
        self._paint_state(state)

    @objc.python_method
    def _paint_state(self, state):
        color = STATUS_COLORS.get(state, NSColor.systemGrayColor())
        self.badge.layer().setBackgroundColor_(color.colorWithAlphaComponent_(0.18).CGColor())
        self.badge_icon.setImage_(symbol(STATE_SYMBOLS.get(state, "circle.fill"), 19, 0.3))
        self.badge_icon.setContentTintColor_(color)

    def applicationShouldTerminateAfterLastWindowClosed_(self, _application):
        return False  # keep listening with the window closed, the Dock icon reopens it

    def applicationWillTerminate_(self, _notification):
        siri = sys.modules.get("siri")
        if siri and siri.ENGINE:
            siri.ENGINE.shutdown()
        if siri and siri.BRIDGE:
            siri.BRIDGE.stop()
        voice_output.configure(mute=None)
        if getattr(self, "global_monitor", None):
            NSEvent.removeMonitor_(self.global_monitor)
        if getattr(self, "local_monitor", None):
            NSEvent.removeMonitor_(self.local_monitor)


def build_menu():
    """App and Edit menus, so Cmd+Q works and Cmd+V pastes into the key fields."""
    bar = NSMenu.alloc().init()
    app_item = NSMenuItem.alloc().init()
    bar.addItem_(app_item)
    app_menu = NSMenu.alloc().init()
    app_menu.addItemWithTitle_action_keyEquivalent_("Quit Hey Jev", "terminate:", "q")
    app_item.setSubmenu_(app_menu)
    edit_item = NSMenuItem.alloc().init()
    bar.addItem_(edit_item)
    edit = NSMenu.alloc().initWithTitle_("Edit")
    for title, action, key in (("Undo", "undo:", "z"), ("Cut", "cut:", "x"), ("Copy", "copy:", "c"),
                               ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")):
        edit.addItemWithTitle_action_keyEquivalent_(title, action, key)
    edit_item.setSubmenu_(edit)
    NSApp.setMainMenu_(bar)


def run_app():
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)  # shows in the Dock with a menu bar
    build_menu()
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.run()
