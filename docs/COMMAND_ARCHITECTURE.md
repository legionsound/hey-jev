# Hey Jev: what it controls, and how to grow it

Status: discussion checkpoint, 2026-09-24. Major implementation is paused at Johnny's request. This document describes the actual source, then proposals. It does not claim the proposed capabilities exist.

## The short answer

Hey Jev currently understands flexible wording for a small fixed collection of actions. It is not yet a general computer-use agent. Python is not the limiting factor: the missing pieces are broader action adapters, argument extraction, target selection and a reliable step runner.

We do **not** need to hardcode every sentence. We do need to implement trustworthy capabilities. A reusable “open app” capability can accept many installed apps; “navigate browser” can accept many URLs; a recipe can combine them. Finding an unfamiliar button or deciding what a new website means is a separate semantic/visual problem.

The first control increments should cover any discovered installed app, then Johnny's exact Safari example with URL readback and honest failure reporting. Keep Jev as a fixed-question classifier and Python as the deterministic executor. General visual computer use is outside the core design.

## Actual path through the app

```mermaid
flowchart TD
    Mic[Microphone: sounddevice] --> Whisper[Local faster-whisper small.en]
    Whisper --> Text[Transcript]
    CLI[siri.py --text] --> Text
    Text --> Handle[handle]
    Handle --> Jev[Jev typed choices and confidence]
    Jev --> Decide[decide / pick_action / sub_action]
    Decide --> Split[Optional second Jev call: first and second action slots]
    Split --> Dispatch[Sequential dispatcher]
    Decide --> Dispatch
    Dispatch --> Native[AppleScript or fixed subprocess]
    Dispatch --> Timer[Python timer state and duration parser]
    Decide --> Answer[Optional OpenRouter answer]
    Native --> Reply[Scripted reply]
    Timer --> Reply
    Answer --> Fish[Fish TTS or cached WAV]
    Reply --> Fish
    Fish --> Playback[Voice playback]
```

1. In the GUI, `NSEvent` watches right Option and sends press/release events to a Python queue. Terminal push-to-talk uses `pynput`. `Recorder` captures mono 16 kHz input through `sounddevice`.
2. Push-to-talk transcribes after release. Wake mode segments speech using a volume threshold and silence; local Whisper transcribes those segments. A regular expression accepts “Hey Jev” and several similar transcriptions. Saying the wake phrase alone arms a six-second follow-up window.
3. `run_voice_assistant()` calls `handle(transcript)`. `--text` calls the same `handle()` directly, skipping Whisper and microphone startup. A text invocation does not start the persistent timer-alert loop, so it is not equivalent to sending a command into the running app.
4. `jev()` sends the transcript and predefined questions to OpenRouter `/api/alpha/decisions` (`typesafe/jev-1.13`) or TypeSafe `/v1/systemone` (`jev-latest`). It returns typed choices, rubric scores and yes/no probabilities, not arbitrary action code or URL/text strings.
5. `decide()`, `pick_action()` and `sub_action()` convert those results into internal action tuples. Most action confidence checks use 0.65; target selection can use 0.5. A compound request causes a second Jev call with FIRST/SECOND copies of the fixed questions.
6. The dispatcher calls `ACTIONS[action](argument)` or `run_timer(action, original_text)`. Timers use deterministic duration/reminder parsing from the transcript. Other arguments currently come from fixed choices.
7. Scripted text describes the result, or a deeper-answer request generates one short sentence. Fish creates a WAV, which is cached. The committed baseline plays it with `afplay`; the uncommitted UI work switches spoken playback to native `NSSound` for live independent gain.

Source anchors: `siri.py`: `QUESTIONS`, `jev`, `ACTIONS`, `parse_duration`, `decide`, `split_actions`, `handle`, `Recorder`, `run_voice_assistant`, `main`. `assistant_ui.py`: key event monitors and controls queue.

## Exact current inventory and mechanisms

| Command family | Supported arguments/behavior | Actual mechanism | Current verification/limit |
| --- | --- | --- | --- |
| Open/quit app | Spotify, Slack, Google Chrome, Visual Studio Code, Finder, Safari, Messages, Notes | Open: `open -a` subprocess. Quit: application AppleScript via `osascript` | Open polls application `is running` for up to five seconds; it does not raise if that wait expires. Quit has no state readback. No installed-app discovery. |
| Mac volume | Up/down by 20; mute/unmute; silent/quiet/medium/loud/max = 0/25/50/75/100 | Standard Additions AppleScript volume commands | Relative changes read initial volume; no post-write verification. Arbitrary exact percentages are not supported. |
| Spotify volume | Same levels and 20-point changes; unmute sets 50 | Spotify AppleScript `sound volume` property | Does not restore previous volume after mute; no post-write readback. |
| Media | Play, pause, next, previous | Spotify AppleScript | **Spotify only**, not system-wide media keys. Play retries 12 times with 0.5-second waits until `player state` says playing. Other actions have no completion readback. |
| Dark mode | On/off/toggle | `System Events` appearance preferences through AppleScript | No UI clicking; no readback after setting. |
| Lock | Lock screen | `System Events` sends Control-Command-Q | This is keyboard automation, dependent on appropriate permissions. No lock-state verification. |
| Sleep | Sleep now | Fixed `pmset sleepnow` subprocess | No completion readback. |
| Timer | Set duration; check soonest; cancel most recently created; cancel all | Python list/lock, number-word normalization and regex duration parsing | In-memory only, lost on quit. Timer loop checks every 0.25 seconds. Supports seconds/minutes/hours and several number phrases, not arbitrary calendar scheduling. |
| Reminder | Timer plus reminder text after “to” | Same parser; optional OpenRouter request writes label/alert; Fish pre-renders it | Existing extraction lowercases and normalizes number words, so it is **not** an exact-text parser suitable for arbitrary typing. |
| Timer completion | Chime, spoken alert and UI state | `afplay` Glass sound, then Fish voice | No external notification delivery guarantee. |
| Conversation/questions | Scripted chat or optional short generated answer | Fixed reply strings or OpenRouter chat completions | Generated answers are not executed as commands. |

`osascript` and other commands are invoked as argument lists, without `shell=True`. Current application names and most command arguments come from fixed tables. This is a useful boundary to preserve when adding free-form text and URLs.

## Why Safari opened but did not navigate

The current schema knows `app=safari` and `app_action=open`. It has no navigation action, URL field, browser tab target or page-readiness check. The second compound slot can only choose from the same existing actions. It cannot represent “go to google.com.” A larger or different answer model alone will not add that capability.

There is another reliability issue: if splitting fails to produce two actions, `split_actions()` falls back to collecting confident actions by fixed target order. The dispatcher catches an action error and keeps going. If one of two actions succeeds, it can still say “Done, both of them.” Those behaviors must change for a dependable sequence. They are more urgent than a sophisticated planner.

## How far this architecture can reach

| Capability | Practical route | What still needs implementation |
| --- | --- | --- |
| Open any installed app | Discover installed apps and resolve names to stable bundle IDs; use Launch Services or `open` | Candidate lookup, ambiguity handling and launch verification. No need to add every app to source manually. |
| App-specific operations | Native app scripting dictionary, URL scheme, supported CLI or API | One adapter per meaningful capability. Apps expose different operations. |
| Menus/buttons | macOS Accessibility API; sometimes System Events UI scripting | Read the target app's accessible elements, resolve a unique role/name, execute supported action and read back state. Some custom controls expose little or nothing. |
| Type specified text | Set a supported accessibility text value, or controlled keystrokes/paste into an explicitly identified field | Focus/target checks, Unicode handling, secure-field exclusions and text readback. Blind typing into whichever app is frontmost is not robust. |
| Navigate Safari | Safari AppleScript tab/document URL property | Validated HTTP(S) argument, explicit tab policy, readiness timeout and observed URL. Redirects need sensible success handling. URL readback does not prove a page fully rendered. |
| Fill a known website field | Safari DOM JavaScript with a known selector, or accessible field targeting | Site-specific target and postcondition. Safari JavaScript from Apple Events is a separate user-controlled developer permission. |
| Search a website | Known search URL template or a tested field-and-submit recipe | Query encoding and result/URL verification. A URL recipe is often simpler than clicking a form. |
| Click an unfamiliar website's “right” control | Semantic reasoning over DOM/accessibility state, or a vision-driven loop | A separate observation/action system. Jev can rank supplied choices, but cannot choose elements it has never been shown. |
| Arbitrary multi-step task | Planner over known capabilities, with observation between steps | Target/state model, bounded retries, interruption, confirmation and failure reporting. Not achieved by adding more `if` statements alone. |

The installed Safari scripting dictionary advertises tabs, URL properties, `do JavaScript` and `search the web`. That proves an API surface exists, not that this app has permission or that any particular website interaction is verified. No browser permission was changed during this inspection.

Apple references: [Accessibility elements](https://developer.apple.com/documentation/applicationservices/axuielement_h), [Safari developer settings](https://developer.apple.com/documentation/safari-developer-tools/developer-settings). Installed evidence: `/Applications/Safari.app/Contents/Resources/Safari.sdef`.

## Must every command be hardcoded?

**No. Implement capabilities once; supply arguments and compose recipes.** The existing `ACTIONS` dictionary is already a small action registry. Start by extending it with a few metadata fields rather than introducing an agent framework.

A capability needs:

- A stable action name and typed arguments, such as `browser.navigate(url)` or `app.open(bundle_id)`.
- Preconditions, such as “Safari has a usable tab” or “the named field exists exactly once.”
- One executor using a native API or a fixed, safely parameterized script.
- A timeout and postcondition, with a result of completed, failed, unsupported or awaiting confirmation.
- An effect classification: ordinary reversible control versus sending, purchasing, deleting or another consequential action.

A recipe is data composing these capabilities. Illustrative only; no recipe runner exists yet:

```json
{
  "name": "Open a website in Safari",
  "arguments": {"url": "http_url"},
  "steps": [
    {"action": "app.open", "bundle_id": "com.apple.Safari"},
    {"action": "browser.navigate", "browser": "safari", "url": "$url"}
  ]
}
```

For noncoders, Johnny can describe or demonstrate a workflow and have Codex create a reviewed recipe. A later small form could expose supported steps and arguments. Do not build that editor before a handful of recipes prove useful. Recipes must not contain arbitrary Python, shell or AppleScript supplied by speech; trusted adapters own executable code.

Jev can select a capability or recipe from known candidates and score ambiguity. Deterministic extraction handles URLs and explicitly delimited text. Generative command planning is outside the current design. If the deterministic grammar cannot extract arguments unambiguously, ask for a clearer command rather than introducing an LLM planner. The optional deeper-answer LLM remains separate from control and must not rewrite command arguments. Whisper itself can mishear speech, so “exact” voice text means preserving the transcript and allowing review where exact wording matters; a typed bridge can preserve bytes directly.

For consequential steps, show the concrete target/action/content for confirmation before executing. Ordinary app opening and navigation should not acquire unnecessary confirmation prompts. Unknown/ambiguous targets should return a specific question or failure, not a guessed click.

## Shortest robust path after discussion

1. Finish and visually check the existing menu bar/model settings work. Let Johnny try volume, mute, settings and model choice first.
2. Add installed-app discovery and deterministic name resolution to `app.open`; then add `browser.navigate` as a short sequence. Support “open Safari, then go to google.com.” Keep transcript extraction narrow and explicit. Check launch failure, URL readback and stop on failure. Johnny tries it in his actual Safari session.
3. Add one useful text/search recipe Johnny chooses, such as a Google search using its encoded search URL. For literal field entry, choose one named field/site and verify the value. Do not promise generic form completion.
4. Add a same-user Unix socket and a small CLI only when sending repeated text turns into the running app is useful. `--text` already skips Whisper; the persistent benefit is shared app/timer state, structured completion and avoiding process/import/key-read overhead, not magical inference acceleration. Serialize requests through the existing busy mechanism and avoid replay after timeout. Benchmark actual turns before making a speed/cost comparison.
5. Extend the lightweight capability/recipe registry as concrete needs accumulate. A SwiftUI shell can come later without replacing the Python engine.

Minimal checks per increment: one small offline check for parsing/validation/failure behavior, existing provider routing tests, and Johnny's actual end-to-end trial. Avoid a large test harness that delays trying the feature. Do not ask Johnny to evaluate an unlaunched UI as if it were ready.

## What Johnny should try first

After the native build has passed a basic launch check: open the menu, change voice gain during a reply, mute/unmute, pause/resume, reopen full settings in menu bar-only mode, pick a different model, and ask a short question. Then try the Safari sentence once that capability is implemented. A failure should name the failed step; it should never claim both steps succeeded when only Safari opened.

Decision for this discussion: is Safari navigation plus a Google-search recipe the right first computer-control increment, or does Johnny want a different concrete typing/site workflow first? Implementation waits for that discussion rather than assuming general browser control is the next step.

## Current implementation checkpoint

- Committed spec: `HEY_JEV_OVERHAUL_SPEC.md`, with native-versus-SwiftUI architecture comparison. Documentation commits: `7a7e7ee`, `09f530c`.
- Uncommitted original-scope edits: `assistant_ui.py`, `siri.py`, new `model_settings.py`, new `voice_output.py`. These cover native controls, model catalog/settings, shared answer payloads and listening/playback handling.
- Checks completed: Python compilation for edited modules; three existing provider tests passed; live read-only OpenRouter catalog and single-model requests returned HTTP 200.
- Not yet checked: actual new UI launch/pixels, live voice gain/mute, new parameter/recording behavior. These edits are **not ready for user acceptance** and have not replaced the running process.
- Expanded Safari, typing, registry and local bridge implementations: **not started**.
- No keys changed, no upstream PR opened. Major implementation paused for Johnny's discussion.


## Proposed core: fixed Jev questions, dynamic arguments

This section supersedes any earlier suggestion of generative command planning for the current scope. Johnny wants broad deterministic Mac voice control through **Jev classification plus a Python harness**. Arbitrary cursor movement and visual clicking are outside the core. The design below is proposed, not implemented.

### Open any installed app without a list of app choices in Jev

Yes: one reusable `app.open` action can resolve an app name from the transcript against the Mac's actual apps. Jev only needs to classify “open an application”; it does not need an answer choice for every app or its version.

Proposed discovery and resolution:

1. Build an in-memory inventory from standard application roots (`/Applications`, `/System/Applications`, `~/Applications`, including app-containing subfolders) plus Spotlight results for application bundles and currently running apps. Do not descend into app bundles to expose internal helpers as ordinary apps. Read bundle path, localized display name/name, filename and bundle identifier. This is a derived inventory, not a maintained database.
2. Refresh lazily on a failed match and through an explicit refresh action. Newly installed apps should work without editing code. Spotlight can be disabled or stale; standard-root scanning provides a fallback. Apps on external volumes or in unusual locations need indexing or a user-selected search folder/path. “Any installed app” means any discovered, launchable app, not a guarantee that every hidden bundle anywhere on disk will be found.
3. For the classified clause, extract the app-name span using bounded syntax such as “open/launch/start <app>.” Normalize case, Unicode and harmless punctuation/spacing for comparison while retaining the original string. Match against discovered names. Use a unique exact name first; a unique well-defined shortened name can be supported. Fuzzy suggestions are acceptable; silent fuzzy execution is not.
4. Ambiguous names or duplicate installations yield explicit choices, for example two versions of Ableton Live. Optional user aliases such as “my editor” can map to a selected app. Aliases are preferences, not a required catalog of all apps.
5. Launch the resolved application URL through `NSWorkspace` and verify the resulting running application by bundle identity/path. A bundle ID can help resolution, but a path distinguishes duplicate installations. Await launch completion; do not confuse “process running” with “document ready.”

Launch Services/NSWorkspace supply app resolution and launching. Public APIs such as `urlForApplication(withBundleIdentifier:)` and `LSCopyApplicationURLsForBundleIdentifier` take a **known bundle ID**; they are not a magical name-to-complete-inventory API. Combine supported discovery sources rather than depending on private Launch Services database dumps. A short-lived in-memory lookup is sufficient; no SQLite database or cloud catalog is needed.

Examples: “open Blender,” “launch DaVinci Resolve,” and “open Safari” all use the same action type. “Open Google” may require clarification between Chrome and another Google app. “Open the thing I used yesterday” is semantic/history resolution and outside this deterministic first pass.

Apple references: [NSWorkspace](https://developer.apple.com/documentation/appkit/nsworkspace), [Launch Services lookup by bundle ID](https://developer.apple.com/documentation/coreservices/1449290-lscopyapplicationurlsforbundleid), [bundle names and identifiers](https://developer.apple.com/library/archive/documentation/General/Reference/InfoPlistKeyReference/Articles/CoreFoundationKeys.html).

### Scale action types, not exhaustive sentences or entities

Keep three separate pieces:

- **Action type:** a bounded operation such as open app, navigate browser, change volume, choose menu item or run recipe. Jev classifies this from flexible language.
- **Arguments/entities:** app identity, URL, amount, text or menu path. Python extracts and resolves these from the transcript and current system state.
- **Executor:** fixed trusted code validates preconditions, performs the operation and observes its result. A new app name or URL changes arguments, not executable code.

Start with a compact schema while it remains fast and accurate. Do not ask every app-specific question on every request as the capability set grows. If measured schema size/accuracy warrants it, use a small top-level domain choice (apps, browser, audio, system, timer, recipe), followed by fixed questions for that domain. That adds a model round trip, so it is a measured tradeoff, not an automatic improvement.

For many recipes, deterministic name/alias/keyword lookup can produce a small candidate set before Jev chooses among candidates. Include “none/unclear”; retrieval can miss the right recipe and must not force a bad match. No embedding database is necessary initially. Separate clauses deterministically only for supported sequencing syntax, protecting quoted text and URLs; classify each clause with the same question schema or use bounded slots. Never split arbitrary dictated text on every “and.”

This does not provide unrestricted language understanding. Commands with unresolved pronouns, missing arguments or unsupported ordering need a concise clarification. The benefit is broad reusable control with explicit limits and predictable execution.

### Noncoder recipes

A recipe adds a name, aliases, typed inputs and an ordered list of existing actions. Johnny can say “make a recipe that opens my browser and searches this site”; Codex authors the data and Johnny tries it. Later, a simple step picker can expose the same fields without editing JSON.

Recipes cannot invent new capabilities. A new target-specific operation may need a small adapter once. Reusing that adapter for many arguments/workflows does not require source changes. Static validation checks that steps exist, argument types match and each step has a bounded completion condition. Runtime execution stops at the first failure and reports which step failed. Confirmation attaches to a concrete consequential step, not every recipe or ordinary action.

### What “click this or that” can mean without visual computer use

- “Choose File > New Window in Safari”: an explicit menu path can be resolved through app scripting or Accessibility. It is a deterministic target, not a screen coordinate.
- “Press the Refresh button in this window”: possible if Accessibility exposes one unique matching button and the window is identified. Inspect its supported actions and invoke the press action; verify the expected change where observable.
- “Run my named shortcut”: a registered, user-chosen macOS Shortcut can be an adapter/recipe. Its actual effects determine whether confirmation is needed; a Shortcut name is not proof that an operation is harmless.
- “Click this”: insufficient without an explicit focus/selection reference. Do not infer a target from the cursor or screenshot in the core path.
- “Click the second blue thing” or “find whichever button completes this new website”: visual/semantic interpretation, outside scope.

Accessibility permission enables an API, not universal understanding. Missing labels, duplicate names, custom canvas controls, changing menus, hidden elements and stale focus are real limits. Known website DOM selectors also remain target-specific. A deterministic operation can return “target not uniquely identified” without becoming a visual agent.

## Proposed configurable wake phrase

Johnny can choose his own spoken wake phrase in Settings. This is proposed, not implemented. Preserve “Hey Jev” as the default and preserve push-to-talk independently.

- Add a plain-text Wake phrase field, explanatory hint, Apply/Save behavior and a “Use Hey Jev” reset. Persist it as a non-secret preference shared by the app's runtime, not in Keychain. Reject empty/whitespace-only values; accept user-chosen phrases rather than a fixed list. State that the current `small.en` recognizer is English-focused, so arbitrary non-English phrases are not guaranteed.
- Match against locally produced transcripts, not raw audio. Normalize case, Unicode, spacing and punctuation in a defined way, then match complete phrase tokens at the start of an utterance. Never interpret user input as a regular expression; never match a short phrase inside a larger word. Preserve the remainder of the transcript as the command.
- Keep the current Jev/Jeff/Jeb-style recognition aliases only for the default phrase. A custom phrase should not silently inherit “Hey Jev” aliases. Custom aliases can be a later explicit preference if Johnny's chosen phrase needs them.
- Update the Whisper wake prompt, readiness text, mode labels/help and menu/status hints from the same preference. The custom phrase followed by a command works in one utterance; the phrase alone retains the existing six-second follow-up behavior.
- Prefer live updates: Save sends a configuration change through the controls queue. Clear the armed follow-up window, discard queued old-phrase segments and invalidate transcriptions begun under the previous phrase. No app restart or Whisper-model reload should be necessary. Apply new configuration between turns; do not reinterpret an already executing command.
- Explain practical limits beside the field: very short/common phrases trigger more easily in conversation or media; unusual names can be misheard; a distinctive multiword phrase is generally easier to distinguish. Do not prohibit a short choice merely because it is less reliable. This remains transcript matching, not a trained always-on wake-word detector, and it does not promise an accuracy rate before Johnny tests his chosen phrase.

Small acceptance check: default phrase, one custom phrase, punctuation/case, no partial-word match, phrase-only follow-up and live change rejecting queued old-phrase work. Johnny then tries his own phrase in his room and confirms that push-to-talk still works. No generative planner or general browser control is needed for this feature.
