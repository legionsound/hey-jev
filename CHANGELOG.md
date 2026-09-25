# Changelog

All notable changes to this fork of [henryklunaris/hey-jev](https://github.com/henryklunaris/hey-jev). The fork starts from upstream `c3a0176` (timers and reminders, 2026-09-22).

## Unreleased

New since 0.3.0, plus fixes from its first live trial.

- **Write into fields with Apple's on-device model.** "Write a reply saying I'll be late", "draft a thank-you note to Sam in the message field", "reply saying sounds good". Apple's model on this Mac writes the text from your request, the app, the field's name and the text shown on screen (never what's typed in fields or passwords), then it's typed and checked like your own words. The confirmation shows the draft, and exactly that draft is what gets typed. No cloud fallback: if Apple Intelligence isn't available it says so. "Write hello" and anything quoted are still typed word for word.
- **A second look before consequential clicks.** When a click or Return would otherwise run without asking (clicks set to Automatic, or a step inside a task), Jev is asked whether it would send, delete, buy, share, merge, approve or similar. If so, or if that check fails, it asks first. It can only add a question, never skip one, and it's off when Risky buttons is set to Automatic.
- **The Jev cursor.** A second, click-through pointer with a small J glides to each control just before Hey Jev acts on it, then fades. Your real pointer never moves. Toggle it from the menu bar (Show Jev cursor).
- **Every visible window.** The numbered overlay and "what can I click?" now cover every window on screen, not just the front one. Numbers run on across windows, "click 4" presses number 4 in whichever window it belongs to, and a Chromium window behind the front one is raised before its control is pressed (and the reply says so). Controls covered by a window on top are refused. Typing into a field in a back window is refused.
- **Whole pages.** A screen read now keeps up to 500 items instead of 60, so long web pages are no longer cut off after the toolbar. Jev's picks are sent in batches.
- **Pick by description.** "Play the video by Frame Set" or "the Full Tilt video": each card on screen carries its nearby text, and Jev decides which card matches. For 45 seconds after Hey Jev asks "which one?", you can answer with a place or a description.
- **More answer providers.** Settings > Answers now offers Off, OpenRouter, Apple (on this Mac or Private Cloud Compute, macOS 26+, via the `helpers/heyjev-fm` Swift helper) and full Claude Code or Codex sessions over ACP. Agent permission requests appear in the confirmation drop-down; Hey Jev never approves them itself. No provider silently falls back to another.
- **Settings > Permissions.** One pane lists Microphone, Dictation, Accessibility, Screen Recording and each Automation target, with live status and Request / Open Settings buttons.
- **Permissions survive rebuilds.** The app is now signed with a stable developer certificate, so rebuilding it no longer silently drops the Accessibility and dictation grants.
- **Clicks on web pages land in Chrome and other Chromium apps.** Chromium ignores `AXPress` on page content, so every page click did nothing. Page controls are now focused and sent Return (Space for checkboxes, radios and switches) as that app's own key, only once the app reports that exact control focused and only while the step is still in time. The pointer never moves.
- **Picks count the page.** "The first video" no longer counts browser tabs named after videos, and a cold page that reads short is read once more with a longer walk. "The first tab" still means the browser's tabs.
- **Heard names match whatever the spacing.** "The Network Chuck video" finds NetworkChuck. Only whole words match, and Jev still decides whether a matching card is a video. An unsure card still makes it ask.
- **"Pause the video" presses the player on screen**; "pause the music" still goes to Spotify.
- **"Which one?" by place**, never by name, and "click <odd name>" is always a click.
- **Pause becoming Play counts as a verified press.**

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
- App icon replaced with the approved frosted voice-and-screen design; editable SVG source included in assets/.
- App renamed to just "Hey Jev" (bundle id unchanged).

### Development

- Test suite (stdlib `unittest`) grew from none to several hundred tests covering the engine, planner, actions, screen control, settings and speech.
- Design notes kept in `docs/`; the ones written before the build are marked historical.
