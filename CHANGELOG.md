# Changelog

All notable changes to this fork of [henryklunaris/hey-jev](https://github.com/henryklunaris/hey-jev). The fork starts from upstream `c3a0176` (timers and reminders, 2026-09-22).

## 0.3.0 (2026-09-25)

A ground-up rework of how commands run, plus screen control. Everything a command does now goes through one engine that plans, asks where you told it to, acts, and then checks the result before saying it worked.

### Command engine

- **One engine for voice and the command line.** `engine.py` runs every request on one serial queue, whether it came from the mic or from `jevctl`. The old `handle()` / `decide()` / `ACTIONS` path is gone.
- **Results you can trust.** Each step ends as `completed` (the app saw the result), `unverified` (done, but nothing to check), `unknown` (may or may not have happened), `failed`, `skipped`, `unsupported`, `needs_clarification` or `needs_confirmation`. Multi-step requests stop at the first step that did not verifiably work and report what already happened.
- **No repeated side effects.** A request id is remembered for the whole app run; re-sending it returns the stored result instead of running it again. Full result bodies are kept ten minutes, then dropped while the id record stays.
- **Confirmations.** Every action belongs to an effect category (open, quit, media, volume, click, type, and so on). Settings > Confirmations sets each one to *Ask first* or *Automatic*. Asks appear as a pop-down from the menu bar and apply to voice and `jevctl` alike.
- **Spoken stop.** Saying "stop" cancels the running request and anything queued behind it.
- **Diagnostics.** Every request stage is logged as one JSON line to `~/Library/Logs/Hey Jev/requests.jsonl`, with keys and secrets redacted.

### `jevctl` command line

- `jevctl command`, `status` and `cancel` send text into the running app over a Unix socket that only your user account can use, and print a JSON result per step.
- `jevctl trials` summarises recent requests from the diagnostic log.

### Apps

- **Installed-app catalog.** App names are matched against what is actually installed (Applications folders, running apps, Spotlight, plus folders you add in Settings), never guessed.
- **Duplicate apps.** When two apps match, Jev breaks the tie using which copy is running or was opened last. A Settings slider (default 85%) sets how sure it must be; below that, or for quit, it asks.
- **Names without spaces.** "Mac whisper" finds MacWhisper.
- Quit uses a bounded helper and reports honestly when an app will not quit.

### Web

- **Websites.** "Go to google.com" opens it and verifies the result where the browser allows: Chrome checks the tab actually loaded that address (strict comparison, redirects handled by policy); Safari opens a new tab and reports `unverified`.
- **Site names.** "Go to YouTube" opens the curated site for a name with no dot.
- **Google search.** "Search Google for …" opens a literal search.
- **Browser choice.** Naming a browser ("… in Safari") wins; unsupported browsers say which ones work.

### Screen control (new)

- **See and press controls.** Hey Jev reads the front window through Accessibility (with Apple's on-device text recognition as a fallback) and can press a named button, link or tab.
- **Type and submit.** Types literal text into a named or focused field and presses Return, checking the field holds exactly what was typed. Password fields are never typed into or read.
- **Scroll and click at the pointer**, acting only on what it can see and prove.
- **Pick by position or description.** "Click the third video", "the video in the bottom-right", "the YouTube video by …": Jev picks among control cards (name, nearby text, place), counting in reading order.
- **Implied presses.** A request no built-in action covers can ask Jev which on-screen control does it, above a confidence gate.
- **Show what Jev sees.** A live overlay (menu bar > Show what Jev sees) numbers what is on screen, so you can say "click 4". A spoken number is bound to the list that was visible when you started speaking and is refused if that list changed while you spoke.
- Privacy: only control names go to Jev. Screen text is shared only where Accessibility vouches it is ordinary text, and field values are never logged.

### Multi-step tasks (new)

- "Take over: …" or "Work on: …" hands Jev a goal in the current app. It decides one step at a time on a pinned window, with one approval per task by default (fully configurable), a shared time limit, and stops when progress stalls.

### Voice and listening

- **Apple on-device dictation** as an alternative to Whisper (Settings > Transcription), switchable live, with a mic test.
- **Custom wake phrase**, applied live, with a warning for short phrases that may trigger by accident.
- **Teach Jev your wake phrase.** Say it a few times and approve the spellings it heard.
- **Voice cues.** Choose whether the Fish voice performs cues like chuckling, laughing and sighing: All, Some (pick each) or None.
- **Exact volume.** "Set volume to 40 percent", "turn it up by 10".
- Named recipes: stored multi-step shortcuts run as one request.
- Answers know the Mac's local date and time.

### Settings and window

- Settings rebuilt in the System Settings style: panes for Providers, Answers, Voice, Confirmations, Apps and Transcription, in that order. Pane edits are save-gated (Cancel/close discards); the voice volume slider and mute control — status window, menu bar, and Settings > Voice — apply immediately and are not undone by Cancel.
- Every model the app uses (Jev, answers, Whisper, Fish voice model, OCR level) is a setting, defaulting to the original values.
- Status window redesigned with Liquid Glass.
- Menu bar icon is now a person-speaking symbol.
- App renamed to just "Hey Jev" (bundle id unchanged).

### Development

- Test suite (stdlib `unittest`) grew from none to several hundred tests covering the engine, planner, actions, screen control, settings and speech.
- Status of this release: the integrated features passed their automated checks, but the final live trial in the running app is still pending.
- Design notes kept in `docs/`; the ones written before the build are marked historical.
