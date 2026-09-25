"""Native status, menu bar controls and separate provider/model settings."""
import contextlib
import itertools
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
from model_settings import (PREFS, PARAMETERS, advanced_open, save_advanced_open, voice, save_voice,
                            app_folders, clean_app_folders, save_app_folders, JEV_MODELS, MODEL_ID_RE, jev_model,
                            save_jev_model, FISH_MODELS, fish_model, save_fish_model, WHISPER_SIZES, whisper_model,
                            save_whisper_model, OCR_LEVELS, ocr_level, save_ocr_level, MAX_APP_FOLDERS, TIEBREAK_MAX, TIEBREAK_MIN, answer_settings, cached_models, confirm_policy,
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


PANE_W, PANE_H, FOOTER_H = 580, 600, 56
TEACH_TAKES = 5
OPS = itertools.count(1)  # async Settings operations: ids are never reused, even across window sessions
GROUP_X, CONTROL_W = 20, 250
SCREEN_EFFECTS = frozenset({"click", "type", "submit", "scroll", "task", "in_task", "risky"})
SETTINGS_PANES = (("providers", "Providers", "key.fill"), ("answers", "Answers", "sparkles"),
                  ("voice", "Voice", "speaker.wave.2.fill"), ("confirm", "Confirmations", "checkmark.shield"),
                  ("apps", "Apps", "square.grid.2x2"), ("transcription", "Transcription", "waveform"))


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
    for i, (title, control, *tip) in enumerate(rows):  # an optional third item is the title's tooltip
        y = i * row_h
        if title:
            label_view = text(title, NSMakeRect(14, y + (row_h - 17) / 2, width - CONTROL_W - 40, 17), 13)
            if tip:
                label_view.setToolTip_(tip[0])
            box.addSubview_(label_view)
        f = control.frame()
        control.setFrame_(NSMakeRect(width - 14 - f.size.width, y + (row_h - f.size.height) / 2, f.size.width,
                                     f.size.height))
        box.addSubview_(control)
        if i:
            line = NSBox.alloc().initWithFrame_(NSMakeRect(14, y, width - 28, 1))
            line.setBoxType_(2)  # separator
            box.addSubview_(line)
    return top + row_h * len(rows) + 22


def content_bottom(view):
    """Lowest edge of a flipped view's visible subviews."""
    return max((v.frame().origin.y + v.frame().size.height for v in view.subviews() if not v.isHidden()), default=0)


def footnote(parent, top, value):
    view = text(value, NSMakeRect(GROUP_X + 6, top - 12, PANE_W - 2 * GROUP_X - 12, 32), 11,
                NSColor.secondaryLabelColor())
    view.setLineBreakMode_(0)  # wrap
    view.cell().setWraps_(True)
    parent.addSubview_(view)


def whisper_cached(size):
    """Whether faster-whisper's model files for this size are already on disk (no network)."""
    try:
        from huggingface_hub import try_to_load_from_cache
        return isinstance(try_to_load_from_cache(f"Systran/faster-whisper-{size}", "model.bin"), str)
    except Exception:
        return False


def label(text, frame, size, color=None):
    view = NSTextField.labelWithString_(text)
    view.setFrame_(frame)
    view.setFont_(NSFont.systemFontOfSize_weight_(size, 0.5))
    view.setTextColor_(color or NSColor.labelColor())
    view.setLineBreakMode_(4)
    return view


def numbers_window(facts):
    """A click-through window spanning the listed items, one small badge per item. Controls are tinted, OCR text grey."""
    items = facts["items"]
    top = AppKit.NSScreen.screens()[0].frame().size.height  # AX frames are top-left on the primary display
    xs = [i["frame"][0] for i in items]
    ys = [i["frame"][1] for i in items]
    x0, y0 = min(xs) - 34, min(ys) - 4  # room for a badge left of the leftmost item
    x1 = max(i["frame"][0] + 40 for i in items)
    y1 = max(i["frame"][1] + 24 for i in items)
    frame = NSMakeRect(x0, top - y1, x1 - x0, y1 - y0)
    win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(frame, 0, 2, False)
    win.setOpaque_(False)
    win.setBackgroundColor_(NSColor.clearColor())
    win.setIgnoresMouseEvents_(True)
    win.setLevel_(AppKit.NSStatusWindowLevel)
    win.setCollectionBehavior_(1 << 0 | 1 << 4)  # all spaces, stationary
    win.setReleasedWhenClosed_(False)
    for i in items:
        text = str(i["n"])
        badge = NSTextField.labelWithString_(text)
        badge.setFont_(NSFont.monospacedDigitSystemFontOfSize_weight_(11, 0.6))
        badge.setTextColor_(NSColor.whiteColor())
        badge.setAlignment_(1)
        badge.setWantsLayer_(True)
        tint = NSColor.systemGrayColor() if i["source"] == "ocr" else NSColor.controlAccentColor()
        badge.layer().setBackgroundColor_(tint.CGColor())
        badge.layer().setCornerRadius_(7)
        w = 10 + 7 * len(text)
        x = i["frame"][0] - x0 - w - 2  # just left of the item, so its label stays readable
        y = i["frame"][1] - y0 + max(0, (i["frame"][3] - 15) / 2)
        badge.setFrame_(NSMakeRect(x, (y1 - y0) - y - 15, w, 15))
        win.contentView().addSubview_(badge)
    return win


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
        op = getattr(self, "sample_op", None)
        if op is not None:  # a sample started before setup plays outside the speech owner: silence it before the mic
            op.cancel()     # opens (cancel stops it under voice_output's lock, and it can't start afterwards)
            self.sample_op = None
            if getattr(self, "settings_sheet", None):
                self.sample_button.setTitle_("Play Sample")
                self.sample_result.setStringValue_("Stopped: Hey Jev started listening.")
        self.worker_started = True
        threading.Thread(target=self._run_assistant, daemon=True).start()

    @objc.python_method
    def _run_assistant(self):
        from siri import run_voice_assistant
        try:
            run_voice_assistant(self.notify, self.controls, self.mode, self.listening, ask=self.ask_confirm,
                                show=self.show_numbers)
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
        self._sync_voice_pane()

    @objc.python_method
    def _sync_voice_pane(self):
        """Settings' Voice pane mirrors the same live gain and mute as the menu and status window."""
        if getattr(self, "settings_sheet", None):
            gain, mute = voice_output.volume(), voice_output.muted()
            self.settings_voice_slider.setDoubleValue_(gain)
            self.settings_voice_slider.setEnabled_(not mute)
            self.settings_voice_slider.setToolTip_(f"Jev's voice volume · {round(gain * 100)}%")
            self.settings_mute.setState_(int(mute))

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
            # Each pane scrolls when its content is taller than the window, so panes can grow without resizing it.
            scroll = AppKit.NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, PANE_W, PANE_H))
            scroll.setHasVerticalScroller_(True)
            scroll.setAutohidesScrollers_(True)
            scroll.setDrawsBackground_(False)
            scroll.setBorderType_(0)
            view = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, PANE_W, PANE_H))
            scroll.setDocumentView_(view)
            item = NSTabViewItem.alloc().initWithIdentifier_(ident)
            item.setLabel_(title)
            item.setView_(scroll)
            tabs.addTabViewItem_(item)
            panes[ident] = view
        self.pane_views = panes
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
            jev_keys.addSubview_(key_field(key))
            self.key_fields[key].setDelegate_(self)  # stacked, only the selected source's shows; edits clear a check
        jev_models = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, CONTROL_W, 22))
        self.jev_model_fields = {}
        for provider in ("openrouter", "typesafe"):
            field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, CONTROL_W, 22))
            field.setStringValue_(jev_model(provider))
            field.setPlaceholderString_(JEV_MODELS[provider])
            field.setToolTip_(f"Default: {JEV_MODELS[provider]}. Empty uses the default.")
            field.setDelegate_(self)
            self.jev_model_fields[provider] = field
            jev_models.addSubview_(field)
        check_cell = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, CONTROL_W, 24))
        self.jev_check_result = text("Not checked", NSMakeRect(0, 4, CONTROL_W - 86, 17), 13,
                                     NSColor.secondaryLabelColor())
        self.jev_check_result.setAlignment_(2)
        self.jev_check_button = NSButton.buttonWithTitle_target_action_("Check", self, "checkJev:")
        self.jev_check_button.setFrame_(NSMakeRect(CONTROL_W - 78, 0, 78, 24))
        self.jev_check_button.setToolTip_("Sends one short test request to the selected source with this key.")
        for view in (self.jev_check_result, self.jev_check_button):
            check_cell.addSubview_(view)
        self.ops = {}  # kind -> id of the one current check / voice lookup / app scan; late results for others drop
        self.jev_checked = None  # (source, typed key) the shown result belongs to
        y = form_group(p, 20, "Jev decisions", [("Source", self.jev_provider), ("API key", jev_keys),
                                                ("Model", jev_models), ("Connection", check_cell)])
        self.answer_provider = self._popup(None, ["Off", "OpenRouter"], NSMakeRect(0, 0, CONTROL_W, 24), "answerSourceChanged:")
        self.answer_provider.selectItemAtIndex_(0 if get_setting("ANSWER_PROVIDER") == "disabled" else 1)
        y = form_group(p, y, "Deeper answers", [("Provider", self.answer_provider),
                                                ("OpenRouter key", key_field("OPENROUTER_API_KEY"))])
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
        top = y + 28
        self.advanced_toggle = NSButton.alloc().initWithFrame_(NSMakeRect(GROUP_X + 2, top, 18, 18))
        self.advanced_toggle.setButtonType_(6)  # on/off
        self.advanced_toggle.setBezelStyle_(5)  # disclosure triangle
        self.advanced_toggle.setTitle_("")
        self.advanced_toggle.setTarget_(self)
        self.advanced_toggle.setAction_("toggleAdvanced:")
        self.advanced_toggle.setAccessibilityLabel_("Show advanced parameters")
        a.addSubview_(self.advanced_toggle)
        a.addSubview_(text("Advanced", NSMakeRect(GROUP_X + 22, top, 90, 18), 12, NSColor.secondaryLabelColor(), weight=0.3))
        self.advanced_summary = text("", NSMakeRect(GROUP_X + 110, top, PANE_W - 2 * GROUP_X - 116, 18), 12,
                                     NSColor.secondaryLabelColor())
        self.advanced_summary.setAlignment_(2)
        a.addSubview_(self.advanced_summary)
        self.advanced_view = FlippedView.alloc().initWithFrame_(NSMakeRect(0, top + 24, PANE_W, PANE_H - top - 24))
        a.addSubview_(self.advanced_view)
        y = form_group(self.advanced_view, 0, None, rows, row_h=32)
        footnote(self.advanced_view, y, "Empty fields use Hey Jev's defaults (80 tokens for answers, 120 for reminders, "
                                        "minimal reasoning where supported) and the provider's for the rest.")
        for field in self.parameter_fields.values():
            field.setDelegate_(self)  # keeps the collapsed summary current
        self.advanced_toggle.setState_(int(advanced_open()))
        self.advanced_view.setHidden_(not advanced_open())

        # Voice: Fish Audio key, the same live volume and mute as the menu, and a sample.
        v = panes["voice"]
        self.settings_voice_slider = self._slider(NSMakeRect(0, 0, CONTROL_W, 24))
        self.settings_mute = NSButton.checkboxWithTitle_target_action_("", self, "toggleVoice:")
        self.settings_mute.setAccessibilityLabel_("Mute Jev's voice")
        self.sample_button = NSButton.buttonWithTitle_target_action_("Play Sample", self, "playSample:")
        self.sample_button.setFrame_(NSMakeRect(0, 0, 120, 24))
        self.sample_result = text("", NSMakeRect(0, 0, 10, 10), 11, NSColor.secondaryLabelColor())
        self.sample_op = None
        self.voice_choices = [voice()]  # the saved voice first; Find adds the rest
        self.voice_popup = self._popup(None, [], NSMakeRect(0, 0, CONTROL_W, 24))
        self.voice_popup.setAccessibilityLabel_("Jev's voice")
        find_cell = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, CONTROL_W, 24))
        self.voice_search = NSSearchField.alloc().initWithFrame_(NSMakeRect(0, 1, CONTROL_W - 72, 22))
        self.voice_search.setPlaceholderString_("Empty lists your voices")
        self.voice_find = NSButton.buttonWithTitle_target_action_("Find", self, "findVoices:")
        self.voice_find.setFrame_(NSMakeRect(CONTROL_W - 64, 0, 64, 24))
        for view in (self.voice_search, self.voice_find):
            find_cell.addSubview_(view)
        self._fill_voices(self.voice_choices[0]["id"])
        self.fish_model_popup = self._popup(None, list(FISH_MODELS), NSMakeRect(0, 0, CONTROL_W, 24))
        self.fish_model_popup.selectItemWithTitle_(fish_model())
        self.fish_model_popup.setToolTip_(f"Fish Audio speech model. Default: {FISH_MODELS[0]}.")
        y = form_group(v, 20, "Fish Audio", [("API key", key_field("FISH_AUDIO_API_KEY")),
                                             ("Speech model", self.fish_model_popup),
                                             ("Voice cues", self._cue_mode_popup()), ("Voice", self.voice_popup),
                                             ("Find voices", find_cell)])
        self.voice_message = text("", NSMakeRect(GROUP_X + 14, y - 16, PANE_W - 2 * GROUP_X, 16), 11,
                                  NSColor.secondaryLabelColor())
        v.addSubview_(self.voice_message)
        footnote(v, y + 4, "Find with an empty box lists your own Fish voices; type a name to search public ones. "
                           "The sample uses the chosen voice. Save to make Jev use it.")
        y = form_group(v, y + 44, "Playback", [("Volume", self.settings_voice_slider), ("Mute", self.settings_mute),
                                               ("Sample", self.sample_button)])
        self.sample_result.setFrame_(NSMakeRect(GROUP_X + 14, y - 16, PANE_W - 2 * GROUP_X, 16))
        v.addSubview_(self.sample_result)
        footnote(v, y + 4, "Volume and mute apply right away and only affect Jev's voice. Timer chimes stay audible.")
        # Voice cues, shown only for Some: one checkbox per performance tag Fish can do.
        self.cue_view = FlippedView.alloc().initWithFrame_(NSMakeRect(0, y + 36, PANE_W, 10))
        v.addSubview_(self.cue_view)
        self.cue_boxes = {}
        rows = []
        for cue in voice_output.CUES["fish"]:
            box = NSButton.checkboxWithTitle_target_action_("", None, None)
            box.setState_(int(cue in self.cue_start["on"]))
            box.setAccessibilityLabel_(f"Perform {cue}")
            self.cue_boxes[cue] = box
            rows.append((cue.capitalize(), box))
        cy = form_group(self.cue_view, 0, "Cues to perform", rows, row_h=30)
        footnote(self.cue_view, cy, "Lines are written with cues like [chuckling]; unticked ones are left out. "
                                    "Applies from the next thing Jev says after Save.")
        self._show_cues()

        # Confirmations: one row per kind of action.
        c = panes["confirm"]
        policy = confirm_policy()
        self.policy_popups = {}
        groups = {"everyday": [], "screen": []}
        for effect in EFFECTS:
            popup = self._popup(None, ["Ask first", "Automatic"], NSMakeRect(0, 0, 140, 24))
            popup.selectItemAtIndex_(0 if policy.get(effect) == "ask" else 1)
            popup.setAccessibilityLabel_(f"{EFFECT_LABELS[effect]} confirmation")
            self.policy_popups[effect] = popup
            groups["screen" if effect in SCREEN_EFFECTS else "everyday"].append((EFFECT_LABELS[effect], popup))
        y = form_group(c, 20, "Ask before: everyday", groups["everyday"], row_h=30)
        if groups["screen"]:
            y = form_group(c, y - 4, "Ask before: screen and tasks", groups["screen"], row_h=30)
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

        # Apps: what discovery found, a background rescan, and extra folders to scan.
        ap = panes["apps"]
        found_cell = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, CONTROL_W + 60, 24))
        self.apps_found = text("Not scanned yet", NSMakeRect(0, 4, CONTROL_W - 76, 17), 13, NSColor.secondaryLabelColor())
        self.apps_found.setAlignment_(2)
        self.apps_refresh = NSButton.buttonWithTitle_target_action_("Refresh Apps", self, "refreshApps:")
        self.apps_refresh.setFrame_(NSMakeRect(CONTROL_W - 60, 0, 120, 24))
        for view in (self.apps_found, self.apps_refresh):
            found_cell.addSubview_(view)
        y = form_group(ap, 20, "Installed apps", [("Found", found_cell)])
        footnote(ap, y, "Hey Jev looks in Applications, running apps, Spotlight and the folders below. Refresh after "
                        "installing something new.")
        self.ocr_popup = self._popup(None, ["Accurate", "Fast"], NSMakeRect(0, 0, 140, 24))
        self.ocr_popup.selectItemAtIndex_(OCR_LEVELS.index(ocr_level()))
        self.ocr_popup.setAccessibilityLabel_("Screen text reading mode")
        y = form_group(ap, y + 44, "Screen reading", [("Read on-screen text", self.ocr_popup)])
        footnote(ap, y, "Accurate reads small and unusual text better; Fast answers \u201cwhat can I click\u201d sooner. "
                        "Reading stays on this Mac.")
        self.folder_drafts = list(app_folders())
        self.folder_top = y + 44
        self.folder_view = FlippedView.alloc().initWithFrame_(NSMakeRect(0, self.folder_top, PANE_W, PANE_H - self.folder_top))
        ap.addSubview_(self.folder_view)
        self._show_folders()

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
        model_cell = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, CONTROL_W, 24))
        self.whisper_popup = self._popup(None, [f"{s} · {mb}" for s, mb in WHISPER_SIZES.items()],
                                         NSMakeRect(0, 0, CONTROL_W, 24), "whisperChanged:")
        self.whisper_popup.selectItemAtIndex_(list(WHISPER_SIZES).index(whisper_model()))
        self.whisper_popup.setAccessibilityLabel_("Whisper model size")
        self.backend_model = text("", NSMakeRect(0, 4, CONTROL_W, 17), 13, NSColor.secondaryLabelColor())
        self.backend_locale = text("English (US)", NSMakeRect(0, 0, CONTROL_W, 17), 13, NSColor.secondaryLabelColor())
        for view in (self.backend_model, self.backend_locale):
            view.setAlignment_(2)
        for view in (self.whisper_popup, self.backend_model):  # Whisper's size picker, or Apple's note
            model_cell.addSubview_(view)
        y = form_group(h, 20, "Speech recognition", [("Recognizer", self.backend_popup), ("Model", model_cell),
                                                     ("Language", self.backend_locale), ("Status", status_cell)])
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
        reset = NSButton.buttonWithTitle_target_action_("Use \u201cHey Jev\u201d", self, "resetWake:")
        reset.setToolTip_("Put back the default phrase and clear the extra spellings. Save to apply.")
        teach = NSButton.buttonWithTitle_target_action_("Teach Jev\u2026", self, "teachWake:")
        teach.setToolTip_("Say your phrase 5 times; Hey Jev suggests spellings it heard that you can add.")
        wake_buttons = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, CONTROL_W, 28))
        for view, x in ((teach, 0), (reset, CONTROL_W - 124)):
            view.setFrame_(NSMakeRect(x, 0, 124 if view is reset else CONTROL_W - 132, 28))
            wake_buttons.addSubview_(view)
        y = form_group(h, y + 28, "Wake phrase", [("Phrase", self.wake_field), ("Also accept", self.alias_field),
                                                   ("", wake_buttons)])
        footnote(h, y, "1 to 4 words. A single short or common word can wake Hey Jev by accident.")
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
        self._fit_panes()
        self._filter_models()
        self._display_parameters()
        self._sync_voice_pane()
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
        self._clear_jev_check()

    def answerSourceChanged_(self, _sender):
        self._sync_key_rows()

    @objc.python_method
    def _sync_key_rows(self):
        typesafe = self.jev_provider.indexOfSelectedItem() == 1
        self.jev_model_fields["openrouter"].setHidden_(typesafe)
        self.jev_model_fields["typesafe"].setHidden_(not typesafe)
        self.key_fields["JEV_OPENROUTER_API_KEY"].setHidden_(typesafe)
        self.key_fields["TYPESAFE_API_KEY"].setHidden_(not typesafe)
        self.key_fields["OPENROUTER_API_KEY"].setEnabled_(self.answer_provider.indexOfSelectedItem() == 1)

    @objc.python_method
    def _show_backend(self):
        """Status of the selected backend, what to do next, and whether a restart is needed to use it."""
        chosen = BACKENDS[self.backend_popup.indexOfSelectedItem()]
        self.allow_button.setHidden_(True)
        from siri import STT
        self.whisper_popup.setHidden_(chosen != "whisper")
        self.backend_model.setHidden_(chosen == "whisper")
        if chosen == "whisper":
            size = list(WHISPER_SIZES)[self.whisper_popup.indexOfSelectedItem()]
            self.whisper_note = (f"Whisper {size} is downloaded." if whisper_cached(size) else
                                 f"Whisper {size} downloads about {WHISPER_SIZES[size]} the next time it loads.")
            if size != whisper_model():
                self.whisper_note += " Save to switch; it reloads without a restart."
            if STT["switching"]:
                status = "Loading…"
            elif STT["backend"] == "whisper" and not STT["blocked"]:
                status = "Loaded, runs on this Mac"
            else:
                status = "Loads when listening starts"
            nxt = self.whisper_note
        else:
            self.backend_model.setStringValue_("Managed by macOS, on-device only")
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

    def whisperChanged_(self, _sender):
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
        op = self.ops["mic"] = next(OPS)
        self.controls.put(("mic_test", lambda r: self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "micTestDone:", {"op": op, "result": r}, False)))

    def micTestDone_(self, payload):
        if not getattr(self, "settings_sheet", None) or payload["op"] != self.ops.get("mic"):
            return  # closed, or a test from an earlier window: never touches this one
        result = payload["result"]
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

    def checkJev_(self, _sender):
        provider = ("openrouter", "typesafe")[self.jev_provider.indexOfSelectedItem()]
        key_name = "TYPESAFE_API_KEY" if provider == "typesafe" else "JEV_OPENROUTER_API_KEY"
        key = self.key_fields[key_name].stringValue().strip() or get_secret(key_name)
        model = self.jev_model_fields[provider].stringValue().strip() or JEV_MODELS[provider]
        if not MODEL_ID_RE.fullmatch(model):
            self.jev_check_result.setTextColor_(NSColor.systemRedColor())
            self.jev_check_result.setStringValue_("That model name isn't valid.")
            return
        generation = self.ops["check"] = next(OPS)
        self.jev_checked = self._jev_draft()
        self.jev_check_button.setEnabled_(False)
        self.jev_check_result.setTextColor_(NSColor.secondaryLabelColor())
        self.jev_check_result.setStringValue_("Checking…")

        def run():
            try:
                import siri
                payload = {"generation": generation, "ms": siri.check_jev(provider, key, model)}
            except Exception as exc:  # check_jev words its errors; anything else stays short and key-free
                payload = {"generation": generation, "error": str(exc) if isinstance(exc, ValueError)
                           else f"Check failed: {type(exc).__name__}"}
            self.performSelectorOnMainThread_withObject_waitUntilDone_("jevChecked:", payload, False)
        threading.Thread(target=run, daemon=True).start()

    def jevChecked_(self, payload):
        if not getattr(self, "settings_sheet", None) or payload["generation"] != self.ops.get("check"):
            return
        self.jev_check_button.setEnabled_(True)
        if self.jev_checked != self._jev_draft():  # the source or key changed meanwhile: the result isn't theirs
            return self._clear_jev_check()
        ok = "error" not in payload
        self.jev_check_result.setTextColor_(NSColor.systemGreenColor() if ok else NSColor.systemRedColor())
        self.jev_check_result.setStringValue_(f"Connected · {payload['ms']} ms" if ok else payload["error"])
        self.jev_check_result.setToolTip_(self.jev_check_result.stringValue())

    @objc.python_method
    def _jev_draft(self):
        provider = ("openrouter", "typesafe")[self.jev_provider.indexOfSelectedItem()]
        key_name = "TYPESAFE_API_KEY" if provider == "typesafe" else "JEV_OPENROUTER_API_KEY"
        return (provider, self.key_fields[key_name].stringValue().strip(),
                self.jev_model_fields[provider].stringValue().strip())

    @objc.python_method
    def _clear_jev_check(self):
        """A shown result stops applying once the source or key changes; a check in flight is dropped."""
        self.ops.pop("check", None)
        self.jev_checked = None
        self.jev_check_button.setEnabled_(True)
        self.jev_check_result.setTextColor_(NSColor.secondaryLabelColor())
        self.jev_check_result.setStringValue_("Not checked")
        self.jev_check_result.setToolTip_(None)

    def toggleAdvanced_(self, sender):
        is_open = bool(sender.state())
        self.advanced_view.setHidden_(not is_open)
        save_advanced_open(is_open)
        self._fit_panes()

    @objc.python_method
    def _cue_mode_popup(self):
        self.cue_start = voice_output.cues("fish")
        popup = self._popup(None, ["All", "Some", "None"], NSMakeRect(0, 0, CONTROL_W, 24), "cueModeChanged:")
        popup.selectItemAtIndex_(voice_output.CUE_MODES.index(self.cue_start["mode"]))
        popup.setToolTip_("Performance tags in Jev's lines, like [chuckling] or [sighing]. Default: All.")
        popup.setAccessibilityLabel_("Fish voice cues")
        self.cue_mode = popup
        return popup

    def cueModeChanged_(self, _sender):
        self._show_cues()

    @objc.python_method
    def _show_cues(self):
        self.cue_view.setHidden_(voice_output.CUE_MODES[self.cue_mode.indexOfSelectedItem()] != "some")
        self._fit_panes()

    @objc.python_method
    def _cue_choice(self):
        mode = voice_output.CUE_MODES[self.cue_mode.indexOfSelectedItem()]
        return mode, [c for c, box in self.cue_boxes.items() if box.state()] if mode == "some" else []

    @objc.python_method
    def _fit_panes(self):
        """Size each pane's scrolling content to what it holds: exactly the window when it fits, taller when not."""
        for view in (getattr(self, "advanced_view", None), getattr(self, "folder_view", None),
                     getattr(self, "cue_view", None)):
            if view is not None:
                f = view.frame()
                view.setFrameSize_((f.size.width, content_bottom(view)))
        for view in getattr(self, "pane_views", {}).values():
            bottom = content_bottom(view)
            view.setFrameSize_((PANE_W, PANE_H if bottom <= PANE_H else bottom + 16))

    @objc.python_method
    def _sync_advanced_summary(self):
        n = sum(1 for f in self.parameter_fields.values() if f.isEnabled() and f.stringValue().strip())
        self.advanced_summary.setStringValue_("Defaults" if not n else f"{n} override{'s' * (n != 1)}")

    def playSample_(self, _sender):
        if self.sample_op is not None:
            self.sample_op.cancel()  # this sample only; Jev's own speech is untouched
            self.sample_op = None
            self.sample_button.setTitle_("Play Sample")
            self.sample_result.setStringValue_("Stopped.")
            return
        if voice_output.muted() or voice_output.volume() == 0:
            self.sample_result.setStringValue_("Jev's voice is muted. Unmute or raise the volume to hear it.")
            return
        key = self.key_fields["FISH_AUDIO_API_KEY"].stringValue().strip() or get_secret("FISH_AUDIO_API_KEY")
        if not key:
            self.sample_result.setStringValue_("Enter a Fish Audio key first.")
            return
        import siri
        report = lambda op_id, state, text: self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "sampleState:", {"op": op_id, "state": state, "text": text}, False)
        op = self.sample_op = siri.SampleOp(key, self._chosen_voice()["id"], report,
                                                     model=self.fish_model_popup.titleOfSelectedItem())
        self.sample_button.setTitle_("Stop")
        self.sample_result.setStringValue_("Getting the sample…")
        if self.worker_started:  # through the speech owner: the mic pauses, so the wake listener can't hear it
            self.controls.put(("voice_sample", op))
        else:  # nothing is listening yet, so there is no floor to take
            threading.Thread(target=siri.play_sample, args=(op, contextlib.nullcontext), daemon=True).start()

    def sampleState_(self, payload):
        op = self.sample_op
        if not getattr(self, "settings_sheet", None) or op is None or payload["op"] != op.id:
            return
        self.sample_result.setStringValue_(payload["text"])
        if payload["state"] == "done":
            self.sample_op = None
            self.sample_button.setTitle_("Play Sample")

    @objc.python_method
    def _fill_voices(self, selected_id):
        self.voice_popup.removeAllItems()
        for v in self.voice_choices:
            self.voice_popup.addItemWithTitle_(v["title"] + (f" · {v['author']}" if v.get("author") else ""))
        ids = [v["id"] for v in self.voice_choices]
        self.voice_popup.selectItemAtIndex_(ids.index(selected_id) if selected_id in ids else 0)

    @objc.python_method
    def _chosen_voice(self):
        return self.voice_choices[max(0, self.voice_popup.indexOfSelectedItem())]

    def findVoices_(self, _sender):
        key = self.key_fields["FISH_AUDIO_API_KEY"].stringValue().strip() or get_secret("FISH_AUDIO_API_KEY")
        query = self.voice_search.stringValue()
        generation = self.ops["voices"] = next(OPS)
        self.voice_find.setEnabled_(False)
        self.voice_message.setStringValue_("Looking up voices…")

        def run():
            try:
                import siri
                payload = {"generation": generation, "voices": siri.fish_voices(key, query), "query": query}
            except Exception as exc:
                payload = {"generation": generation, "error": str(exc) if isinstance(exc, ValueError)
                           else f"Couldn't list voices ({type(exc).__name__})."}
            self.performSelectorOnMainThread_withObject_waitUntilDone_("voicesLoaded:", payload, False)
        threading.Thread(target=run, daemon=True).start()

    def voicesLoaded_(self, payload):
        if not getattr(self, "settings_sheet", None) or payload["generation"] != self.ops.get("voices"):
            return
        self.voice_find.setEnabled_(True)
        if "error" in payload:
            self.voice_message.setStringValue_(payload["error"])
            return
        chosen = self._chosen_voice()
        found = [v for v in payload["voices"] if v["id"] != chosen["id"]]
        self.voice_choices = [chosen] + found  # the current choice stays, results follow
        self._fill_voices(chosen["id"])
        where = f"matching \u201c{payload['query'].strip()}\u201d" if payload["query"].strip() else "of your own"
        self.voice_message.setStringValue_(f"{len(found)} voices {where}." if found else f"No voices {where}.")

    @objc.python_method
    def _show_folders(self):
        """Rebuild the extra-folders group from the unsaved drafts."""
        for view in list(self.folder_view.subviews()):
            view.removeFromSuperview()
        rows = []
        home = os.path.expanduser("~")
        for i, path in enumerate(self.folder_drafts):
            remove = NSButton.buttonWithTitle_target_action_("Remove", self, "removeAppFolder:")
            remove.setTag_(i)
            remove.setAccessibilityLabel_(f"Remove {path}")
            full = "~" + path[len(home):] if path.startswith(home + "/") else path
            parent = os.path.basename(os.path.dirname(path.rstrip("/")))
            rows.append((f"{os.path.basename(path.rstrip('/')) or path}  \u00b7  {parent}" if parent else full, remove, full))
            remove.setToolTip_(f"Remove {full}")
        add = NSButton.buttonWithTitle_target_action_("Add Folder…", self, "addAppFolder:")
        add.setEnabled_(len(self.folder_drafts) < MAX_APP_FOLDERS)
        rows.append(("" if self.folder_drafts else "None added", add))
        y = form_group(self.folder_view, 0, "Extra app folders", rows, row_h=34)
        footnote(self.folder_view, y, "Saved with Save; Refresh Apps then includes them. Symlinks are followed; "
                                      "Finder aliases aren't.")
        self._fit_panes()

    def addAppFolder_(self, _sender):
        panel = AppKit.NSOpenPanel.openPanel()
        panel.setCanChooseDirectories_(True)
        panel.setCanChooseFiles_(False)
        panel.setAllowsMultipleSelection_(False)
        panel.setPrompt_("Add")
        if panel.runModal() == 1:  # NSModalResponseOK
            self._add_folder(panel.URL().path())

    @objc.python_method
    def _add_folder(self, path):
        try:
            self.folder_drafts = clean_app_folders(self.folder_drafts + [path])
            self.settings_message.setStringValue_("")
        except ValueError as exc:
            self.settings_message.setStringValue_(str(exc))
        self._show_folders()

    def removeAppFolder_(self, sender):
        if 0 <= sender.tag() < len(self.folder_drafts):
            del self.folder_drafts[sender.tag()]
        self._show_folders()

    def refreshApps_(self, _sender):
        generation = self.ops["apps"] = next(OPS)
        self.apps_refresh.setEnabled_(False)
        self.apps_found.setStringValue_("Scanning…")

        def run():  # recursive walks and a 10 s Spotlight timeout: never on the UI thread
            try:
                import app_catalog
                payload = {"generation": generation, "count": app_catalog.refresh(),
                           "misses": sorted(app_catalog.last_source_misses())}
            except Exception as exc:
                payload = {"generation": generation, "error": f"Scan failed ({type(exc).__name__})."}
            self.performSelectorOnMainThread_withObject_waitUntilDone_("appsRefreshed:", payload, False)
        threading.Thread(target=run, daemon=True).start()

    def appsRefreshed_(self, payload):
        if not getattr(self, "settings_sheet", None) or payload["generation"] != self.ops.get("apps"):
            return
        self.apps_refresh.setEnabled_(True)
        if "error" in payload:
            self.apps_found.setStringValue_(payload["error"])
            return
        names = {"mdfind": "Spotlight", "running": "running apps", "folders": "extra folders"}
        missed = [names.get(m, m) for m in payload["misses"]]
        self.apps_found.setStringValue_(f"{payload['count']} apps" + (f" · {', '.join(missed)} unavailable" if missed else ""))
        self.apps_found.setToolTip_(self.apps_found.stringValue())

    # Teach Jev: 5 transcript-only takes through the mic test, then spellings the user may add.
    def teachWake_(self, _sender):
        import wake
        phrase, aliases = wake_settings()
        if not self.worker_started:
            self.settings_message.setStringValue_("Hey Jev isn't running yet.")
            return
        try:
            unsaved = (wake.validate(self.wake_field.stringValue()), wake.parse_aliases(self.alias_field.stringValue())) \
                != (phrase, list(aliases))
        except ValueError:
            unsaved = True
        if unsaved:
            self.settings_message.setStringValue_("Save first: Teach Jev listens for the saved phrase.")
            return
        self.teach = {"op": next(OPS), "phrase": phrase, "aliases": list(aliases), "texts": [], "skipped": 0, "take": 0}
        self._show_teach_sheet()
        self._teach_next()

    @objc.python_method
    def _show_teach_sheet(self):
        panel = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 420, 300), NSWindowStyleMaskTitled, NSBackingStoreBuffered, False)
        panel.setReleasedWhenClosed_(False)
        content = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, 420, 300))
        panel.setContentView_(content)
        content.addSubview_(text("Teach Jev your wake phrase", NSMakeRect(20, 18, 380, 20), 15, weight=0.3))
        self.teach_status = text("", NSMakeRect(20, 46, 380, 56), 13, NSColor.secondaryLabelColor())
        self.teach_status.setLineBreakMode_(0)
        self.teach_status.cell().setWraps_(True)
        content.addSubview_(self.teach_status)
        self.teach_list = FlippedView.alloc().initWithFrame_(NSMakeRect(20, 108, 380, 140))
        content.addSubview_(self.teach_list)
        self.teach_boxes = []
        self.teach_add = NSButton.buttonWithTitle_target_action_("Add Selected", self, "teachAdd:")
        self.teach_add.setFrame_(NSMakeRect(300, 254, 104, 28))
        self.teach_add.setEnabled_(False)
        self.teach_close = NSButton.buttonWithTitle_target_action_("Stop", self, "teachClose:")
        self.teach_close.setFrame_(NSMakeRect(196, 254, 96, 28))
        self.teach_close.setKeyEquivalent_("\x1b")
        for view in (self.teach_add, self.teach_close):
            content.addSubview_(view)
        self.teach_sheet = panel
        self.settings_sheet.beginSheet_completionHandler_(panel, None)

    @objc.python_method
    def _teach_next(self):
        t = self.teach
        t["take"] += 1
        self.teach_status.setStringValue_(f"Say \u201c{t['phrase']}\u201d once, then wait ({t['take']} of {TEACH_TAKES})\u2026")
        op = t["op"]
        self.controls.put(("mic_test", lambda r: self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "teachTake:", {"op": op, "result": r}, False)))

    def teachTake_(self, payload):
        t = getattr(self, "teach", None)
        if t is None or payload["op"] != t["op"] or not getattr(self, "settings_sheet", None):
            return  # stopped, closed, or from an earlier session
        r = payload["result"]
        if r.get("error") or not (r.get("text") or "").strip():
            t["skipped"] += 1
        else:
            t["texts"].append(r["text"])
        if t["take"] < TEACH_TAKES:
            self._teach_next()
        else:
            self._teach_finish()

    @objc.python_method
    def _teach_finish(self):
        import wake
        t = self.teach
        result = wake.learn(t["phrase"], t["aliases"], t["texts"])
        t["done"] = True
        heard, matched = result["takes"], result["matched"]
        skipped = f" {t['skipped']} take{'s' * (t['skipped'] != 1)} skipped (nothing heard or the mic was busy)." \
            if t["skipped"] else ""
        if not heard:
            line = "Nothing was heard." + skipped
        elif matched == heard and not result["candidates"]:
            line = f"Hey Jev already hears you: {matched} of {heard} takes matched." + skipped
        else:
            line = f"Heard it {matched} of {heard} takes." + skipped
            line += " Tick any spelling that was really you, then Add Selected." if result["candidates"] else \
                " No new spellings to suggest."
        self.teach_status.setStringValue_(line)
        for i, c in enumerate(result["candidates"][:5]):
            box = NSButton.checkboxWithTitle_target_action_(f"\u201c{c['alias']}\u201d \u00d7{c['count']}", self,
                                                           "teachTicked:")
            box.setFrame_(NSMakeRect(0, i * 28, 380, 22))
            box.setState_(0)  # never pre-checked
            self.teach_list.addSubview_(box)
            self.teach_boxes.append((box, c["alias"]))
        self.teach_close.setTitle_("Close")

    def teachTicked_(self, _sender):
        self.teach_add.setEnabled_(any(b.state() for b, _ in self.teach_boxes))

    def teachAdd_(self, _sender):
        import wake
        picked = [alias for box, alias in self.teach_boxes if box.state()]
        if not picked:
            return
        try:
            current = wake.parse_aliases(self.alias_field.stringValue())
        except ValueError:
            current = []
        have = {" ".join(wake.words(a)) for a in current + [self.teach["phrase"]]}
        added, full = [], 0
        for alias in picked:
            if " ".join(wake.words(alias)) in have:
                continue
            if len(current) >= wake.MAX_ALIASES:
                full += 1
                continue
            current.append(alias)
            added.append(alias)
            have.add(" ".join(wake.words(alias)))
        self.alias_field.setStringValue_(", ".join(current))
        self._end_teach()
        msg = f"Added {len(added)} spelling{'s' * (len(added) != 1)}. Save to apply." if added else "Nothing new to add."
        if full:
            msg += f" {full} didn't fit: up to {wake.MAX_ALIASES} extra spellings."
        self.settings_message.setStringValue_(msg)

    def teachClose_(self, _sender):
        self._end_teach()

    @objc.python_method
    def _end_teach(self):
        """Stop or close: later takes are ignored, and nothing heard is kept."""
        self.teach = None
        sheet = getattr(self, "teach_sheet", None)
        self.teach_sheet = None
        self.teach_boxes = []  # recognized spellings go with the sheet; only ones added to the form remain
        self.teach_status = self.teach_list = self.teach_add = self.teach_close = None
        if sheet is not None:
            if getattr(self, "settings_sheet", None):
                self.settings_sheet.endSheet_(sheet)
            sheet.orderOut_(None)

    def resetWake_(self, _sender):
        import wake
        self.wake_field.setStringValue_(wake.DEFAULT)
        self.alias_field.setStringValue_("")
        self.settings_message.setStringValue_("Reset to \u201cHey Jev\u201d. Save to apply.")

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
        self.ops = {}
        self._end_teach()
        if getattr(self, "sample_op", None) is not None:
            self.sample_op.cancel()
        self.sample_op = None
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
            folders = clean_app_folders(self.folder_drafts)  # checked before anything is written
            jev_models = {p: f.stringValue().strip() or JEV_MODELS[p] for p, f in self.jev_model_fields.items()}
            if not all(MODEL_ID_RE.fullmatch(m) for m in jev_models.values()):
                raise ValueError("Jev model: letters, numbers and . _ : / + - only.")
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
            for provider, model in jev_models.items():
                if model != jev_model(provider):
                    save_jev_model(provider, model)
            mode, on = self._cue_choice()
            start = self.cue_start
            if (mode, on) != (start["mode"], start["on"] if start["mode"] == "some" else []):
                voice_output.set_cues("fish", mode, on)  # the next spoken line follows it
            if self.fish_model_popup.titleOfSelectedItem() != fish_model():
                save_fish_model(self.fish_model_popup.titleOfSelectedItem())
            if OCR_LEVELS[self.ocr_popup.indexOfSelectedItem()] != ocr_level():
                save_ocr_level(OCR_LEVELS[self.ocr_popup.indexOfSelectedItem()])
            size = list(WHISPER_SIZES)[self.whisper_popup.indexOfSelectedItem()]
            whisper_changed = size != whisper_model()
            if whisper_changed:
                save_whisper_model(size)
            from siri import STT
            if self.worker_started and (STT["backend"] != backend or STT["blocked"]
                                        or (whisper_changed and backend == "whisper")):
                self.controls.put(("transcription", backend))  # live switch or reload through the control queue
            save_tiebreak_threshold(self.tiebreak_slider.doubleValue())
            if folders != app_folders():
                save_app_folders(folders)
            chosen = self._chosen_voice()
            if chosen["id"] != voice()["id"]:
                save_voice(chosen["id"], chosen["title"])  # the next reply speaks with it
            from siri import reload_keys
            reload_keys()
            warn = wake.short_warning(phrase)
            self._start_worker()
            if warn:
                self.settings_message.setStringValue_(warn)  # window stays open so the warning is visible
            else:
                self.closeSettings_(None)
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
        elif notification.object() in getattr(self, "parameter_fields", {}).values():
            self._sync_advanced_summary()
        elif notification.object() in (self.key_fields.get("JEV_OPENROUTER_API_KEY"),
                                       self.key_fields.get("TYPESAFE_API_KEY"),
                                       *getattr(self, "jev_model_fields", {}).values()):
            self._clear_jev_check()

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
        self._sync_advanced_summary()

    @objc.python_method
    def show_numbers(self, facts):
        """Engine worker thread: badge each listed item with its number, over the window, for a few seconds."""
        self.performSelectorOnMainThread_withObject_waitUntilDone_("showNumbers:", facts or {}, False)

    def showNumbers_(self, facts):
        for w in getattr(self, "number_windows", []):
            w.orderOut_(None)
        self.number_windows = [numbers_window(facts)] if facts.get("items") else []
        for w in self.number_windows:
            w.orderFrontRegardless()
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(12.0, self, "hideNumbers:", None, False)

    def hideNumbers_(self, _timer):
        for w in getattr(self, "number_windows", []):
            w.orderOut_(None)
        self.number_windows = []

    @objc.python_method
    def ask_confirm(self, pending):
        """Engine worker thread: show (dict) or close (None) the confirmation pop-down."""
        self.performSelectorOnMainThread_withObject_waitUntilDone_("showConfirm:", pending or {}, False)

    def showConfirm_(self, pending):
        if getattr(self, "confirm_popover", None):
            self.confirm_popover.close()
            self.confirm_popover = None
            prior, self.confirm_prior = getattr(self, "confirm_prior", None), None
            import screen
            screen.end_handoff()
            front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
            ours = front is not None and front.processIdentifier() == AppKit.NSRunningApplication.currentApplication() \
                .processIdentifier()
            if prior is not None and ours and not prior.isTerminated():
                prior.activateWithOptions_(0)  # we still hold the foreground only because of the pop-down: give it back
        if not pending:
            return
        front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        me = AppKit.NSRunningApplication.currentApplication()
        self.confirm_prior = front if front is not None and front.processIdentifier() != me.processIdentifier() else None
        import screen
        screen.set_handoff(self.confirm_prior.processIdentifier() if self.confirm_prior is not None else None)
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
        if siri and getattr(self, "settings_sheet", None):  # recognizer loading state stays live in Settings
            seen = tuple(siri.STT.values())
            if seen != getattr(self, "stt_seen", None):
                self.stt_seen = seen
                self._show_backend()
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
