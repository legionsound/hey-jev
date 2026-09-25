# Architecture

How the pieces fit. The formal behavior spec is `ENGINE_CONTRACT.md`;
this file is the map, not the law. Where they disagree, the contract wins.

## Pipeline

```text
mic ─► Whisper / Apple dictation ─► wake phrase ─► planner ─► engine ─► actions ─► readback ─► reply (Fish voice)
                                                       ▲
                                    jevctl ─► socket ──┘
```

1. **Hear** (`siri.py`, `speech_apple.py`, `wake.py`). On-device
   transcription; in wake mode only phrases starting with the wake phrase
   are submitted. A FIFO turn worker holds the microphone floor only
   while speaking. "Stop" phrases with work in flight are handled at once
   on the listener: heard-but-unsubmitted turns are dropped and every
   running/queued engine request is cancelled.
2. **Plan** (`planner.py`). Text split into at most 5 ordered clauses on
   sequencing words (`then`, `and then`, `after that`, `, and`; a bare
   "and" never splits). Each clause gets one Jev call that picks the
   action type only; Python extracts arguments (app span, URL, level,
   duration). Direct screen commands ("scroll down", "click here") skip
   classification. If Jev says compound but the split finds one clause,
   the request asks for "X, then Y" instead of guessing.
3. **Run** (`engine.py`). One serial worker for voice and CLI. Asks first
   where policy says to, runs each action under a deadline, polls the
   readback until proof or timeout. Stops at the first step that is not
   `completed` (including `unverified`).
4. **Reply** (`siri.py`, `voice_output.py`). The spoken line comes from
   what actually happened. Mute/lock/sleep speak a short "about to" line
   before running (speech cannot follow them); that line never claims
   completion. `unverified` is spoken as "sent, couldn't check".

## Module ownership

| File | Owns |
| --- | --- |
| `actions.py` | Action registry, native adapters (osascript/subprocess), effect categories, confirmation text |
| `planner.py` | Text → ordered steps: clause split, Jev classify per clause, argument extraction |
| `engine.py` | Serial worker, queue, ledger, confirmation gate, step runner, results |
| `bridge.py` | Unix socket server. Validates and enqueues only |
| `jevctl` | CLI client (command / status / cancel / trials) |
| `app_catalog.py` | Installed-app discovery and name resolution |
| `screen.py` | Accessibility/OCR observation, press/type/submit/scroll/click, overlay support |
| `task.py` | Multi-step "take over" goals on a pinned window |
| `url_adapter.py` | URL normalization, site-name map, Chrome/Safari open + verify |
| `siri.py` | Microphone, transcription, speech, UI wiring; submits text, speaks results |
| `assistant_ui.py` | Status window, menu bar, Settings panes, confirmation popover |
| `model_settings.py` | Preferences (policies, models, folders, wake) on NSUserDefaults |
| `secrets_store.py` | API keys in the Mac Keychain |
| `diagnostics.py`, `trials.py` | JSONL request log, recent-request summaries |
| `timers.py`, `recipes.py` | Timer/reminder store, named multi-step recipe shortcuts |

## Key mechanisms

- **Action entry** (`actions.py: entry`): each action declares `effect`
  (policy category), `resolve` (deterministic args → target/choices/none),
  `run` (perform; errors after dispatch prove nothing → `unknown`),
  `verify` (polled readback → done/wait/failed/unverified; `None` only
  where no readback exists), `proves` (what the readback actually
  establishes), and a timeout.
- **Native calls**: every native call in `run`/`verify` is a subprocess
  with `timeout = deadline - now`; at the deadline the worker kills and
  reaps its own subprocess and reports `unknown`. Apple Events go through
  `osascript` (Spotify, volume, dark mode, Chrome tabs) and System Events
  keystrokes; direct AX work goes through ApplicationServices
  (`screen.py`), screenshots/capture through Quartz, app identity through
  AppKit (`NSWorkspace`, `NSRunningApplication`).
- **Ledger and replay** (`engine.py`): ids reserved under one lock before
  enqueue; same id + same text returns the existing status; same id +
  different text is `id_conflict`. Full results kept 10 minutes, then a
  small id record for the rest of the run (cap 10,000; overflow → `busy`,
  never evict). Restart changes the instance; old ids are
  `unknown_outcome`, never safe-to-retry.
- **Bridge** (`bridge.py`): `~/Library/Application Support/Hey Jev/run/jev.sock`,
  0700 dir / 0600 socket, peer uid must equal ours (`getpeereid`), `flock`
  before bind, 16 KiB request cap, 5 s send bound. Ops: `command`,
  `status`, `cancel`. If the app cannot own the lock + socket it exits
  before creating microphone/voice.
- **Confirmation gate** (`engine.py` + `actions.py: describe`): Ask-first
  steps block the worker on a menu-bar popover naming the concrete action
  and target. Confirm / Cancel / request-cancel / 60 s timeout consume
  one record with a single compare-and-set; first wins. After Confirm the
  target is re-resolved; any change fails with `target_changed`.
- **Pending-effect hold**: if an earlier step's native effect (e.g. a
  screen press) outlived its deadline and may still land, every later
  dispatch — any action family, including queued requests — waits up to
  10 s for it to settle, then fails without dispatching
  (`an earlier action hasn't finished`). The earlier step stays `unknown`.

## Screen stack detail (`screen.py`)

Accessibility walk with node/time caps (ported from
typesafe-computer-use, MIT — see `ax_walk.py` and
`LICENSES/typesafe-computer-use.txt`) lists declared controls; Apple
Vision OCR on a window capture adds only text lines no control covers.
Press identity is exact (pid + start time, window, element); OCR-only
items are never pressable. Type flows through `AXSelectedText` (no
keystrokes); submit through `AXConfirm`; scroll through native scroll-bar
values or `AXScrollToVisible`; pointer clicks through Quartz CGEvents at
the current pointer position.
