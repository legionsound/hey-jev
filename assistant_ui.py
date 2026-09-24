"""Native status, menu bar controls and separate provider/model settings."""
import queue
import sys
import threading

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
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskFullSizeContentView,
    NSWindowStyleMaskTitled,
)
from AppKit import NSPopover, NSViewController
from Foundation import NSObject, NSTimer, NSUserDefaults
from actions import EFFECT_LABELS, EFFECTS
from model_settings import (PREFS, PARAMETERS, answer_settings, cached_models, confirm_policy, fetch_models,
                            save_answer_settings, save_confirm_policy, save_transcription_backend,
                            transcription_backend, validate_parameters, BACKENDS)
import voice_output
from secrets_store import KEY_NAMES, get_secret, get_setting, missing_secrets, save_secret


BASE_HEIGHT, ROW = 250, 24
STICK_TOP, STICK_BOTTOM = 8, 32  # NSViewMinYMargin, NSViewMaxYMargin
NORMAL, FLOATING = 0, 3  # NSNormalWindowLevel, NSFloatingWindowLevel
HINTS = {"ptt": "Hold right Option to talk", "wake": "Say \u201cHey Jev\u201d, then your command"}
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
            NSMakeRect(0, 0, 460, 250), style, NSBackingStoreBuffered, False
        )
        self.panel.setTitle_("Hey Jev")
        self.panel.setTitlebarAppearsTransparent_(True)
        self.panel.setMovableByWindowBackground_(True)
        self.panel.setReleasedWhenClosed_(False)  # closing just hides it, the Dock icon brings it back
        self.on_top = NSUserDefaults.standardUserDefaults().boolForKey_("keep_on_top")
        self.panel.setLevel_(FLOATING if self.on_top else NORMAL)
        self._add_window_menu()

        background = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, 460, 250))
        background.setMaterial_(NSVisualEffectMaterialHUDWindow)
        background.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        background.setState_(NSVisualEffectStateActive)
        background.setAutoresizingMask_(18)  # grow with the window
        self.panel.setContentView_(background)
        self.background = background
        self.timer_rows = []

        self.dot = label("●", NSMakeRect(25, 170, 24, 30), 18, NSColor.systemOrangeColor())
        self.status = label("Starting", NSMakeRect(55, 171, 375, 30), 22)
        self.detail = label("Loading Whisper…", NSMakeRect(27, 135, 405, 30), 14, NSColor.secondaryLabelColor())
        self.hint = label(HINTS[self.mode], NSMakeRect(27, 12, 405, 22), 12, NSColor.tertiaryLabelColor())
        for view in (self.dot, self.status, self.detail, self.hint):
            background.addSubview_(view)
        for view in (self.dot, self.status, self.detail):
            view.setAutoresizingMask_(STICK_TOP)
        self.hint.setAutoresizingMask_(STICK_BOTTOM)

        settings = NSButton.buttonWithTitle_target_action_("Settings…", self, "showSettings:")
        settings.setFrame_(NSMakeRect(350, 212, 98, 24))  # top right, in line with the title bar
        settings.setBezelStyle_(1)  # rounded, so the title shows (9 is the "?" help button)
        settings.setControlSize_(1)  # small
        settings.setFont_(NSFont.systemFontOfSize_(11))
        settings.setAutoresizingMask_(STICK_TOP)
        background.addSubview_(settings)

        self.mode_switch = NSSegmentedControl.segmentedControlWithLabels_trackingMode_target_action_(
            ["Hold Option", "Hey Jev"], 0, self, "modeChanged:"
        )
        self.mode_switch.setControlSize_(1)
        self.mode_switch.setFont_(NSFont.systemFontOfSize_(11))
        self.mode_switch.setFrame_(NSMakeRect(24, 92, 275, 28))
        self.mode_switch.setSelectedSegment_(MODES.index(self.mode))
        self.mode_switch.setAutoresizingMask_(STICK_BOTTOM)
        background.addSubview_(self.mode_switch)
        self.pause_button = NSButton.buttonWithTitle_target_action_("Pause", self, "toggleListening:")
        self.pause_button.setFrame_(NSMakeRect(320, 91, 115, 30))
        background.addSubview_(self.pause_button)
        self.voice_label = label("Voice", NSMakeRect(27, 57, 85, 22), 12)
        background.addSubview_(self.voice_label)
        self.voice_slider = self._slider(NSMakeRect(110, 55, 205, 25))
        background.addSubview_(self.voice_slider)
        self.mute_button = NSButton.buttonWithTitle_target_action_("Mute voice", self, "toggleVoice:")
        self.mute_button.setFrame_(NSMakeRect(320, 51, 115, 30))
        background.addSubview_(self.mute_button)
        self._build_status_menu()
        self._sync_controls()
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.5, self, "tick:", None, True)

        screen = NSScreen.mainScreen().visibleFrame()
        self.panel.setFrameOrigin_(NSMakePoint(screen.origin.x + (screen.size.width - 460) / 2,
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
        self.hint.setStringValue_(HINTS[self.mode])
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
        self.pause_button.setTitle_("Pause" if self.listening else "Resume")
        self.hint.setStringValue_(HINTS[self.mode] if self.listening else "Microphone paused · timers remain active")
        self.menu_only_item.setState_(int(self.menu_only))
        gain, mute = voice_output.volume(), voice_output.muted()
        self.voice_slider.setDoubleValue_(gain)
        self.menu_voice_slider.setDoubleValue_(gain)
        self.voice_label.setStringValue_(f"Voice {round(gain * 100)}%")
        self.menu_voice_label.setStringValue_(f"Voice volume · {round(gain * 100)}%" + (" · off" if mute else ""))
        self.mute_button.setTitle_("Voice off" if mute else "Mute voice")
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
        self.updateStatus_({"state": "Ready" if self.listening else "Paused", "detail": HINTS[self.mode] if self.listening else "Microphone paused. Current action may finish."})

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
            NSMakeRect(0, 0, 680, 620), NSWindowStyleMaskTitled, NSBackingStoreBuffered, False)
        sheet.setTitle_("Hey Jev Settings")
        sheet.setReleasedWhenClosed_(False)
        content = sheet.contentView()
        tabs = NSTabView.alloc().initWithFrame_(NSMakeRect(18, 80, 644, 520))
        content.addSubview_(tabs)
        providers, answers, confirms, hearing = (NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 610, 480)) for _ in range(4))
        for title, view in (("Providers & keys", providers), ("Deeper answers", answers), ("Confirmations", confirms),
                            ("Transcription", hearing)):
            item = NSTabViewItem.alloc().initWithIdentifier_(title)
            item.setLabel_(title)
            item.setView_(view)
            tabs.addTabViewItem_(item)
        providers.addSubview_(label("Your providers", NSMakeRect(24, 419, 560, 30), 22))
        providers.addSubview_(label("Keys stay in Keychain. Leave a field blank to keep its saved key.",
                                    NSMakeRect(24, 390, 560, 24), 12, NSColor.secondaryLabelColor()))
        providers.addSubview_(label("Jev decisions", NSMakeRect(24, 313, 160, 24), 13))
        self.jev_provider = self._popup(providers, ["OpenRouter", "TypeSafe direct"], NSMakeRect(194, 310, 385, 28))
        self.jev_provider.selectItemAtIndex_(0 if get_setting("JEV_PROVIDER") == "openrouter" else 1)
        providers.addSubview_(label("Deeper answers", NSMakeRect(24, 157, 160, 24), 13))
        self.answer_provider = self._popup(providers, ["Disabled", "OpenRouter"], NSMakeRect(194, 154, 385, 28))
        self.answer_provider.selectItemAtIndex_(0 if get_setting("ANSWER_PROVIDER") == "disabled" else 1)
        self.key_fields = {}
        for title, key, y in (("Voice: Fish Audio", "FISH_AUDIO_API_KEY", 349),
                              ("Jev: OpenRouter", "JEV_OPENROUTER_API_KEY", 266),
                              ("Jev: TypeSafe", "TYPESAFE_API_KEY", 222),
                              ("Answer: OpenRouter", "OPENROUTER_API_KEY", 108)):
            providers.addSubview_(label(title, NSMakeRect(24, y + 2, 169, 24), 13))
            field = NSSecureTextField.alloc().initWithFrame_(NSMakeRect(194, y, 385, 28))
            field.setPlaceholderString_("Already configured" if get_secret(key) else "Paste key")
            providers.addSubview_(field)
            self.key_fields[key] = field
        providers.addSubview_(label("Model and request controls are in the Deeper answers tab.",
                                    NSMakeRect(24, 63, 560, 24), 12, NSColor.secondaryLabelColor()))
        providers.addSubview_(label("An existing .env still takes priority over Keychain.",
                                    NSMakeRect(24, 37, 560, 24), 12, NSColor.secondaryLabelColor()))

        current = answer_settings()
        self.selected_model = current["model"]
        self.selected_metadata = current["metadata"]
        self.parameter_drafts = {self.selected_model: current["parameters"]}
        answers.addSubview_(label("Deeper answers", NSMakeRect(24, 429, 360, 30), 22))
        self.refresh_button = NSButton.buttonWithTitle_target_action_("Refresh models", self, "refreshModels:")
        self.refresh_button.setFrame_(NSMakeRect(433, 427, 155, 32))
        answers.addSubview_(self.refresh_button)
        self.model_search = NSSearchField.alloc().initWithFrame_(NSMakeRect(24, 388, 560, 28))
        self.model_search.setPlaceholderString_("Search all text models by name or model ID")
        self.model_search.setDelegate_(self)
        answers.addSubview_(self.model_search)
        self.model_popup = self._popup(answers, [], NSMakeRect(24, 349, 560, 28), "modelChanged:")
        self.catalog_message = label("Saved model available offline. Refresh to load the catalog.", NSMakeRect(24, 320, 560, 22), 11, NSColor.secondaryLabelColor())
        answers.addSubview_(self.catalog_message)
        self.model_info = label("", NSMakeRect(24, 295, 560, 22), 11, NSColor.secondaryLabelColor())
        answers.addSubview_(self.model_info)
        answers.addSubview_(label("Advanced request controls", NSMakeRect(24, 258, 560, 26), 16))
        self.parameter_fields = {}
        for i, (key, (title, kind, low, high)) in enumerate(PARAMETERS.items()):
            col, row = i % 2, i // 2
            x, y = 24 + col * 286, 211 - row * 43
            answers.addSubview_(label(title, NSMakeRect(x, y + 2, 164, 22), 12))
            field = NSTextField.alloc().initWithFrame_(NSMakeRect(x + 164, y, 103, 26))
            field.setToolTip_(f"{key}: {low:g} to {high:g}. Blank uses the default.")
            answers.addSubview_(field)
            self.parameter_fields[key] = field
        answers.addSubview_(label("Blank: provider defaults; token limit: 80 answers / 120 reminders.",
                                  NSMakeRect(24, 46, 575, 20), 11, NSColor.secondaryLabelColor()))
        answers.addSubview_(label("Reasoning models may need more tokens. Unsupported controls are disabled.",
                                  NSMakeRect(24, 23, 580, 20), 11, NSColor.secondaryLabelColor()))
        confirms.addSubview_(label("Ask before doing", NSMakeRect(24, 429, 560, 30), 22))
        confirms.addSubview_(label("Applies to voice and typed commands alike. Ask first shows a pop-down from the menu bar.",
                                   NSMakeRect(24, 400, 575, 22), 12, NSColor.secondaryLabelColor()))
        policy = confirm_policy()
        self.policy_popups = {}
        for i, effect in enumerate(EFFECTS):
            y = 350 - i * 36
            confirms.addSubview_(label(EFFECT_LABELS[effect], NSMakeRect(24, y + 2, 250, 24), 13))
            popup = self._popup(confirms, ["Ask first", "Automatic"], NSMakeRect(300, y, 200, 28))
            popup.selectItemAtIndex_(0 if policy[effect] == "ask" else 1)
            popup.setAccessibilityLabel_(f"{EFFECT_LABELS[effect]} confirmation")
            self.policy_popups[effect] = popup
        hearing.addSubview_(label("Transcription", NSMakeRect(24, 429, 560, 30), 22))
        hearing.addSubview_(label("What turns your voice into text. Typed and jevctl commands don't use it.",
                                  NSMakeRect(24, 400, 575, 22), 12, NSColor.secondaryLabelColor()))
        hearing.addSubview_(label("Backend", NSMakeRect(24, 352, 160, 24), 13))
        self.backend_popup = self._popup(hearing, ["Local Whisper", "Apple on-device"], NSMakeRect(194, 349, 385, 28),
                                         "backendChanged:")
        self.backend_popup.selectItemAtIndex_(BACKENDS.index(transcription_backend()))
        self.backend_status = label("", NSMakeRect(24, 305, 575, 22), 12)
        self.backend_next = label("", NSMakeRect(24, 280, 575, 22), 12, NSColor.secondaryLabelColor())
        self.backend_restart = label("", NSMakeRect(24, 255, 575, 22), 12, NSColor.systemOrangeColor())
        for view in (self.backend_status, self.backend_next, self.backend_restart):
            hearing.addSubview_(view)
        self.allow_button = NSButton.buttonWithTitle_target_action_("Allow Apple dictation", self, "allowAppleSpeech:")
        self.allow_button.setFrame_(NSMakeRect(24, 205, 200, 32))
        hearing.addSubview_(self.allow_button)
        hearing.addSubview_(label("Microphone test", NSMakeRect(24, 160, 560, 24), 16))
        hearing.addSubview_(label("Records 4 seconds with the backend in use and shows the text. Nothing is run.",
                                  NSMakeRect(24, 136, 575, 20), 11, NSColor.secondaryLabelColor()))
        self.test_button = NSButton.buttonWithTitle_target_action_("Test microphone", self, "micTest:")
        self.test_button.setFrame_(NSMakeRect(24, 96, 170, 32))
        hearing.addSubview_(self.test_button)
        self.test_result = label("", NSMakeRect(204, 101, 395, 22), 12)
        hearing.addSubview_(self.test_result)
        hearing.addSubview_(label("Apple dictation runs only on this Mac. If on-device recognition isn't available,",
                                  NSMakeRect(24, 60, 575, 20), 11, NSColor.secondaryLabelColor()))
        hearing.addSubview_(label("it stays off and says why. It never sends your voice to Apple or switches backends by itself.",
                                  NSMakeRect(24, 40, 575, 20), 11, NSColor.secondaryLabelColor()))
        self._show_backend()
        self.settings_message = label("", NSMakeRect(25, 48, 630, 24), 12, NSColor.systemRedColor())
        content.addSubview_(self.settings_message)
        for title, action, x in (("Cancel", "closeSettings:", 457), ("Save", "saveSettings:", 556)):
            button = NSButton.buttonWithTitle_target_action_(title, self, action)
            button.setFrame_(NSMakeRect(x, 10, 100, 32))
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

    @objc.python_method
    def _show_backend(self):
        """Status of the selected backend, what to do next, and whether a restart is needed to use it."""
        chosen = BACKENDS[self.backend_popup.indexOfSelectedItem()]
        self.allow_button.setHidden_(True)
        if chosen == "whisper":
            status, nxt = "Local Whisper: runs on this Mac. Loads when Hey Jev starts.", ""
        else:
            try:
                import speech_apple
                state, reason = speech_apple.status("en-US")
            except ImportError:
                state, reason = "missing_bindings", "Apple Speech support isn't installed in this build."
            status = "Apple on-device: ready." if state == "ready" else f"Apple on-device: not ready. {reason}"
            nxt = {"not_determined": "Click Allow Apple dictation, then approve the macOS prompt.",
                   "denied": "Turn on Hey Jev in System Settings, Privacy & Security, Speech Recognition.",
                   "restricted": "Speech recognition is restricted on this Mac.",
                   }.get(state, "" if state == "ready" else "Listening stays off with this choice until it's ready.")
            self.allow_button.setHidden_(state != "not_determined")
        self.backend_status.setStringValue_(status)
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
            self.test_result.setStringValue_(f"Heard “{result['text'] or '(nothing)'}” in {result['ms']} ms")

    @objc.python_method
    def _popup(self, parent, titles, frame, action=None):
        popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(frame, False)
        popup.addItemsWithTitles_(titles)
        if action:
            popup.setTarget_(self)
            popup.setAction_(action)
        parent.addSubview_(popup)
        return popup

    def closeSettings_(self, _sender):
        if getattr(self, "settings_sheet", None):
            self.fetch_generation += 1
            self.settings_sheet.orderOut_(None)
            self.settings_sheet = None

    @objc.python_method
    def _parameter_values(self):
        return {key: field.stringValue().strip() for key, field in self.parameter_fields.items() if field.isEnabled()}

    def saveSettings_(self, _sender):
        try:
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
            from siri import STT
            if self.worker_started and (STT["backend"] != backend or STT["blocked"]):
                self.controls.put(("transcription", backend))  # live switch through the control queue
            from siri import reload_keys
            reload_keys()
            self.closeSettings_(None)
            self._start_worker()
        except Exception as exc:
            self.settings_message.setStringValue_(str(exc))

    def refreshModels_(self, _sender):
        key = self.key_fields["OPENROUTER_API_KEY"].stringValue().strip() or get_secret("OPENROUTER_API_KEY")
        if not key:
            self.catalog_message.setStringValue_("Enter an answer key in Providers & keys, then refresh.")
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
            y = 130 + ROW * (count - 1 - i)  # soonest on top, just above the bottom row
            name_view = label("", NSMakeRect(27, y, 280, 20), 13, NSColor.secondaryLabelColor())
            time_view = label("", NSMakeRect(310, y, 98, 20), 15, NSColor.systemTealColor())
            time_view.setFont_(NSFont.monospacedDigitSystemFontOfSize_weight_(15, 0.4))
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
        self.dot.setTextColor_(STATUS_COLORS.get(state, NSColor.labelColor()))

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
