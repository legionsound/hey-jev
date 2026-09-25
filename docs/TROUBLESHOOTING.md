# Troubleshooting

## App will not start / no window

- Check the diagnostic log tail:
  `tail ~/Library/Logs/Hey\ Jev/requests.jsonl`.
- If the bridge socket is already owned (another copy running), the app
  stops before creating microphone/voice, by design (`bridge.py`). Look
  for a second Hey Jev process and quit it; do not delete the socket dir
  by hand (stale sockets are reclaimed under lock automatically).

## Microphone hears nothing

- Settings > Transcription: run the mic test. Switch backends (Whisper
  vs Apple dictation) to isolate.
- macOS System Settings > Privacy & Security > Microphone: Hey Jev
  allowed?
- Wake mode only acts on phrases starting with the wake phrase. Try
  Hold-Option mode (hold right Option, talk, release) to bypass wake.

## Wake phrase triggers too much / too little

- Settings > Transcription: avoid short phrases (the UI warns). Teach
  Jev your wake phrase: say it a few times and approve the spellings.

## Screen control cannot see / press

- Accessibility must be on (System Settings > Privacy & Security >
  Accessibility). Without it, no screen control works.
- Optional Screen Recording lets Vision OCR read on-screen text that
  Accessibility does not expose. It never makes OCR-only text pressable:
  OCR-only items stay `not_a_control`.
- Switch already on but Hey Jev still says it's off: the switch can
  belong to an older build (unsigned builds change identity on every
  rebuild). Select Hey Jev, click −, add it again. Screen Recording
  also needs Hey Jev quit and reopened. "Show what Jev sees" shows
  which one is missing, with Open Settings and Recheck; each toggle-on
  and Recheck logs both permissions and the code signature to
  `requests.jsonl` (stage `permissions`). See DEVELOPMENT.md to sign
  local builds so grants survive rebuilds.
- "Click 4" refused with `screen_changed`: the window changed while you
  spoke. Re-open "Show what Jev sees" and say the number again.
- Clicks over Hey Jev's own windows, the overlay, or menus are refused
  by design.

## A step says `unverified` / `unknown`

- `unverified` = the app acted but has no readback proving it (Safari
  navigation, lock, sleep, pointer clicks, web scrolls). Say it again
  only if you want it re-done: nothing auto-retries.
- `unknown` = the deadline hit or the helper died after dispatch; it may
  or may not have happened. Check yourself before re-asking (e.g. is the
  app actually open?), because re-sending the same id returns the stored
  result — to run again, use a new request.
- `target_changed`: the confirmed target moved or vanished between
  confirmation and dispatch. Confirm again on the fresh target.

## Confirmations never appear / appear too often

- Settings > Confirmations: check the category (click, type, submit,
  task, risky default Ask first; open, navigate, media, volume, display,
  timer, scroll default Automatic).
- Risky labels (buy, send, delete…) ask by default; setting `risky` to
  Automatic overrides this.
- A picked quit target always asks, whatever the quit policy.

## `jevctl` issues

- `jevctl status <id>` for the stored result; same id + same text
  returns it, same id + different text is `id_conflict` (use a new id).
- `unknown_outcome`: the app restarted since that id (instance changed);
  it never means safe to retry.
- Socket is same-user only; `wait` must be a finite non-negative number.

## Voice / answers sound wrong or cost too much

- Fixed replies are cached. For current
  rates see the providers' pricing pages (Fish Audio, OpenRouter,
  TypeSafe) — do not rely on numbers quoted in older docs.
- Voice cues (chuckling etc.): Settings > Voice > All / Some / None.

## Tests fail on a fresh checkout

- Run the suite with the checkout's venv, which has PyObjC:
  `.venv/bin/python -m unittest discover -q`
  from the repo root. Stock system python3 ships without AppKit/Foundation
  (unless PyObjC is installed globally) and every
  macOS-dependent test module fails to import there — that is
  environmental, not a code regression.
