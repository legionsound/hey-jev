# Features

What Hey Jev can do in this release, grounded in the code in this folder.
For exact behavior rules see `ENGINE_CONTRACT.md`.

## Apps (`app_catalog.py`, `actions.py`)

- Opens and quits installed Mac apps, matched against what is actually
  installed (Applications folders, running apps, Spotlight, plus extra
  folders from Settings > Apps). Names are never guessed.
- "Mac whisper" finds MacWhisper (space-insensitive match).
- Two matching copies: Jev breaks the tie from what is running / opened
  last, above the Settings confidence slider (default 85%). Below it, or
  for quit, it asks which.
- Quit only terminates processes whose bundle path equals the resolved
  path, and reports honestly when an app will not quit.

## Volume, music, system (`actions.py`)

- Exact system volume ("set volume to 40 percent", relative up/down),
  mute/unmute, per-app Spotify volume.
- Spotify play / pause / next / previous via AppleScript. "Pause the
  video", "resume the clip" (a video word and no music word, as a request
  of its own) press the player control on screen instead.
- Dark mode on/off (read back after setting), lock screen, sleep the Mac
  (lock and sleep report `unverified`: there is no readback).

## Web (`url_adapter.py`, `actions.py`)

- "Go to google.com" opens the URL and verifies the result where the
  browser allows it (Chrome: tab id + strict URL comparison after
  documented normalizations; Safari and others: `unverified`).
- "Go to YouTube" resolves a dotless name through a curated site map;
  unknown names are never guessed.
- "Search Google for …" opens a literal search URL.
- Naming a browser ("… in Safari") wins. Naming a browser Hey Jev does
  not drive refuses the navigation (`unsupported_browser`) — nothing
  opens. Otherwise: only Chrome reports a completed
  load (unique tab id + URL match); Safari opens a new tab and reports
  `unverified`; other browsers are opened and reported `unverified`.
  Page content and load state are not checked.

## Screen control (`screen.py`)

- `screen.list`: reads the front window through Accessibility (Apple
  Vision OCR adds only text lines no control covers). Items carry their
  source (`ax`, `ocr`, `ax+ocr`), capped at 60, numbered in reading order.
- `screen.press`: presses a named button/link/tab, or "click 4" for the
  numbered overlay (menu bar > Show what Jev sees). Numbers bind to the
  list visible when speech started; a changed list refuses (`screen_changed`).
  Several matches ask "which one?" by place ("top left or bottom right"),
  never by name. Inside a web page in Chrome, Brave, Edge, Arc or an
  Electron app, the control is focused and sent Return (Space for a
  checkbox, radio or switch) as that app's own key, because Chromium
  ignores `AXPress` on page content; the key goes only once the app reports
  that exact control focused. A bare clickable `div` that no keyboard can
  reach doesn't react and stays `unverified`. A press is verified when the
  control changes, disappears, or changes its name (Pause becomes Play).
  "Click …" is always a click, however odd the name.
- `screen.type`: types literal text into a named or focused field via
  `AXSelectedText` (no keystrokes, nothing submitted), replacing the
  current selection. Password fields are
  never typed into or read. Verified only when the field holds exactly the
  old text with the selected range replaced by the typed text.
- `screen.submit`: "press return" sends `AXConfirm` to the focused
  element. Verified only when the field goes away or its text changes;
  that never means a message or purchase was accepted.
- `screen.scroll`: native scroll bars move by fraction and verify by
  value readback; web areas use `AXScrollToVisible` and always report
  `unverified`.
- `pointer.click`: a real click where the pointer already is (left, right,
  double). Always `unverified`; refused over Hey Jev's own windows,
  overlays, or menus.
- `screen.pick`: "click the third video", "the video in the bottom-right",
  "the Blender video" — Jev answers which controls match the noun (one
  batched yes/no per control card, threshold 0.65). A card is the
  control's name, the words right around it (a video's channel, a
  product's price) and where it sits; a card ends at the next like item,
  so rows never borrow each other's words. The code then counts in one
  reading order or takes the nearest to the named place. On a web page
  a video, result or link pick counts only the page, never the browser's
  tabs; "the first tab" still means the browser's. Words heard that appear
  on a card, spacing and case aside ("network Chuck" is NetworkChuck),
  settle that card's name (whole words only: "apple" never matches
  Pineapple) unless Jev is sure that card isn't one, and Jev still judges
  whether it is a video at all. If an item Jev wasn't sure about could
  change the answer, it asks, likeliest first. A cut-short read
  is retried once with a longer walk; still cut short, it asks you to
  scroll or name it. The pick runs as an ordinary press with
  identity re-check and readback.
- Answering "which one?": for 45 seconds after Hey Jev asks, reply "the
  first one", "the second", "the last one", "number 2", or words from one
  option's name. That exact control is pressed if it's still there,
  unchanged. "Stop", "cancel" or "none of them" withdraw the question;
  anything else is a new command. A reply you started saying before the
  question was asked never counts as its answer.
- Direct commands ("scroll down", "click here") skip classification and
  run directly, because their words leave nothing for Jev to choose.

## Multi-step tasks (`task.py`)

- "Take over: …" / "Work on: …" hands Jev a goal in the current app. One
  approval per task by default (category `task`); with `in_task`
  Automatic that OK covers its clicks/typing/Return, with Ask first each
  step asks. Shared 120 s deadline, at most 10 s per Jev call, stops when
  progress stalls. `done` is verified only against an explicit
  "until you see X" that appeared during the task; anything else ends
  `unverified`.

## Voice and listening (`siri.py`, `speech_apple.py`, `wake.py`)

- Transcription on the Mac: faster-whisper or Apple on-device dictation
  (Settings > Transcription, switchable live, with mic test).
- Custom wake phrase ("Hey Jev" default), applied live; short phrases
  warn about accidental triggers. "Teach Jev your wake phrase" learns
  spellings from a few repetitions.
- Fish Audio voice with All / Some / None cue control; fixed replies cached.
- Saying "stop" cancels the running request and anything queued (only
  what has not been dispatched yet; an already-sent effect is reported
  as sent).

## Command line (`jevctl`, `bridge.py`)

- `jevctl command --text "…"`, `--id`, `--wait`, `status`, `cancel`,
  `trials --last N`. Same planner, queue, and confirmation rules as voice.
- Results per step: only `completed` means the app saw the result;
  `unverified` = acted but could not check; `unknown` = may or may not
  have happened. Never retries; re-sending an id returns the stored result.

## Confirmations and safety (`engine.py`, `actions.py`)

- Every action belongs to an effect category; Settings > Confirmations
  maps each to Ask first or Automatic. Ask-first defaults: quit, lock,
  sleep, click, type, submit, task, risky. Reading the screen (`look`)
  has no effect and never asks.
- Risky-labeled controls (buy, send, delete, pay, submit, post, share,
  install, sign out, similar) ask by default — but setting `risky` to
  Automatic overrides this. That list only adds confirmation; it
  never proves other clicks harmless.
- Multi-step requests stop at the first step that did not verifiably
  complete (`unverified` stops too); the rest are `skipped`.
- Request ids are remembered for the whole app run (full bodies 10 min);
  re-sending returns the stored result, never re-runs. Nothing retries
  side effects.

## Diagnostics (`diagnostics.py`, `trials.py`)

- Every request stage logged as one JSON line to
  `~/Library/Logs/Hey Jev/requests.jsonl`, keys and secrets redacted,
  field values and screen text never logged.
- `jevctl trials` summarizes recent requests from that log.
