# Configuration

All settings live in the Settings window (System Settings style) and in
the status window / menu bar. Settings panes: Providers, Answers, Voice,
Confirmations, Apps, Transcription — in that order
(`assistant_ui.py: SETTINGS_PANES`).

## Settings panes (save-gated)

The six Settings panes apply on save: closing or Cancel discards
unsaved changes. (The status-window voice volume slider and mute button
are the exception: they apply immediately, live.)

### Providers

- Jev provider: OpenRouter or direct TypeSafe key, plus the Jev model id
  per provider (`model_settings.py: save_jev_model`). Stored in Keychain
  via `secrets_store.py`.
- First launch opens Settings and asks for keys. Keys are stored in the
  Mac Keychain, never in files or logs.

### Answers

- Optional spoken answers to questions ("who wrote Hamlet?", "what time
  is it?") via Claude Haiku through OpenRouter. Answers know the Mac's
  local date and time.
- Answer model, budgets: the old 80/120 budgets are preserved until
  changed; user overrides apply to both paths
  (`model_settings.py: save_answer_settings`).

### Voice

- Fish Audio voice id + title (`save_voice`), Fish model
  (`s2.1-pro-free` default; `save_fish_model`).
- Voice cues: All, Some (pick each), or None — whether the voice
  performs cues like chuckling, laughing, sighing.
- Status-window volume slider and mute apply immediately and sync to the
  menu bar slider; they are not part of the save-gated panes.

### Confirmations

- One Ask first / Automatic switch per effect category. Categories
  (`actions.py: EFFECTS`): open, navigate, media, volume, display,
  timer, scroll, click, type, submit, task, in_task, risky, quit, lock,
  sleep. `look` (reading the screen) has no effect and is not a setting.
- Ask-first defaults: quit, lock, sleep, click, type, submit, task,
  risky. The rest default Automatic.
- Duplicate-app tiebreak threshold: 50–100, default 85. At 100 it always
  asks. The threshold is a model score, not a correctness claim.

### Apps

- Extra folders to search for installed apps (`save_app_folders`), beyond
  the built-in Applications folders, running apps, and Spotlight.
- Advanced-open toggle (`save_advanced_open`).

### Transcription

- Backend: faster-whisper or Apple on-device dictation
  (`save_transcription_backend`), switchable live, with a mic test.
- Whisper model size (`save_whisper_model`, default small.en ≈ 480 MB
  one-time download); Apple dictation skips the download.
- OCR level for screen reading (`save_ocr_level`).
- Wake phrase + aliases (`save_wake_settings`), applied live.

## Permissions (first launch)

1. Microphone (macOS prompt).
2. Accessibility: System Settings > Privacy & Security > Accessibility —
   needed for the right-Option key and all screen control.
3. Automation: first control of Spotify, Safari, Chrome, or System Events.
4. Screen Recording (optional): lets Vision OCR read on-screen text that
   Accessibility does not expose.

## Files and storage

- Keys: Mac Keychain (`secrets_store.py`).
- Preferences (policies, models, folders, wake): NSUserDefaults
  (`model_settings.py: PREFS`).
- Diagnostic log: `~/Library/Logs/Hey Jev/requests.jsonl` (keys
  redacted, no field values or screen text).
- Bridge socket: `~/Library/Application Support/Hey Jev/run/jev.sock`
  (mode 0700 dir, 0600 socket, same-uid peers only).
