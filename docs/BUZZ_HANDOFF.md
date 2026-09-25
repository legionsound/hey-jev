# Buzz implementation handoff

> **Historical note (2026-09-25 release):** this document was written before the build and is kept for context only. Do not extend it. Current behavior is described in [FEATURES.md](FEATURES.md), [ARCHITECTURE.md](ARCHITECTURE.md), and the authoritative [ENGINE_CONTRACT.md](ENGINE_CONTRACT.md).


Prepared 2026-09-24. **Astra owns spec/design only from this point. Johnny will use agents in a Buzz project on his Claude subscription for implementation.** The existing Buzz project now has a populated native Git repository; one agent performed Git setup only. Feature implementation has not started in this Astra task. No further feature code is authorized in this Astra task.

## Product goal

**Two co-equal foundations:** reusable discoverable Mac capabilities, and a local text/programmatic bridge through which Codex/ChatGPT can command the single running Hey Jev app. Voice transcripts and typed/API text requests converge before Jev classification and deterministic planning, then share one serial dispatcher, state, validation, confirmation and structured results. Build the smallest bridge as foundation work, not a late optional feature. Existing `--text` starts a fresh one-shot process and does not share running app/timer state.


On a user's Mac, discover installed apps and machine-specific targets programmatically, maintain a local catalog and give Jev only the relevant bounded choices. Jev remains a fixed-question classifier; Python owns argument extraction, entity resolution, permission checks, trusted execution and verification. No hardcoded app-name whitelist. Arbitrary cursor/vision control and an LLM command planner remain outside the core.

Discovery does not magically create adapters or prove safety. The catalog must distinguish metadata found, trusted operation available, permission state, target observed now and action actually verified. Basic app opening can be generic across discovered apps. Browser/app-specific operations can still use adapters keyed by bundle identity; that is different from limiting the app opener to eight names.

## Read first

1. [HEY_JEV_OVERHAUL_SPEC.md](HEY_JEV_OVERHAUL_SPEC.md): feature scope, acceptance, native-versus-SwiftUI recommendation.
2. [COMMAND_ARCHITECTURE.md](COMMAND_ARCHITECTURE.md): actual command path/inventory, fixed-classifier design, configurable wake phrase.
3. [CAPABILITY_EFFORT_MAP.md](CAPABILITY_EFFORT_MAP.md): atomic controls, effort/permissions, local discovery/catalog lifecycle and proposed workstreams.

Newer user refinements govern older suggestions: stay deterministic, discover app targets, use Johnny as the main end-to-end tester and keep automated checks minimal but meaningful. No general browser agent, no broad framework and no upstream PR before acceptance.

## Repository and preservation

- Actual Git checkout: `/Users/johnmeyer/Developer/Hey Jev`. `/Users/johnmeyer/Developer/Jev` is the original project folder, **not** the implementation checkout.
- Fork: `https://github.com/legionsound/hey-jev.git` (`origin`). Original: `https://github.com/henryklunaris/hey-jev.git` (`upstream`).
- Verified original implementation base: `8c04746`, branch `feat/provider-roles`.
- Spec/design branch: `feat/menubar-model-settings`. Its commits after the base are documentation only. Use the latest remote head for these documents.
- **Incomplete code snapshot:** `wip/native-controls-incomplete`, commit `6b9fe892dbc788a4fd2ab47bcd0ab1a236921055`. Pushed to the fork solely for preservation/handoff. It is based on documentation commit `a76b8cf`; the newer handoff/discovery docs are on the spec branch.
- The WIP snapshot contains only the four source-file changes listed below. It was created with a separate Git index; the active branch/index/local working files were not reset, switched, cleaned or overwritten. The local checkout still shows those same four source changes as uncommitted.

**Remote/isolated workers:** begin from the latest spec branch in an isolated checkout. The native-work owner may cherry-pick `6b9fe89` into its feature branch to inspect/finish the WIP. Other workers should not cherry-pick it casually because it changes `siri.py`. Do not repeat that cherry-pick in the dirty shared checkout; it already contains the patch. The integrator decides which implementation branch owns these edits.

This WIP commit was chosen over a local-only patch because workers on another runtime/checkout need a recoverable reference. It is explicitly incomplete, not a release, acceptance claim or permission to overwrite credentials. No upstream PR exists.

### Verified Buzz topology

The separate managed checkout is `/Users/johnmeyer/.buzz/REPOS/74d22bbe06b4ca0468da56ccc8159826814b792a4769fafd0b0ed66f4c615031--hey-jev`. Its `origin` is the existing Buzz repository; `github` is Johnny's GitHub fork; `upstream` is the original GitHub repository. The four branches `main`, `feat/provider-roles`, `feat/menubar-model-settings` and `wip/native-controls-incomplete` were published with the same Git history. The canonical dirty checkout above remains untouched. Use isolated implementation branches from the latest spec branch. Buzz reviews are native reviews; GitHub publication and any eventual upstream PR are separate explicit operations.

## Incomplete source files

| File | Draft content | Evidence and remaining work |
| --- | --- | --- |
| `assistant_ui.py` | Native menu bar dropdown, optional menu-only mode, status controls, pause/mute/gain, separate tabbed settings, searchable model popup and async catalog refresh | Compiles. Not launched or visually inspected after edits. Check PyObjC callbacks, actual frames/timer layout, activation/reopen/quit behavior, loading/failure UX and keyboard/accessibility interactions. |
| `model_settings.py` | Shared non-secret NSUserDefaults suite, answer model/parameter persistence, numeric validation, catalog/cache and answer payload helper | Compiles. Live public catalog checked separately, but no focused tests yet exercise this helper. Check metadata validation, offline behavior, unavailable models, parameter limits and models advertising only `max_completion_tokens`. Avoid silently losing a token budget when a model lacks `max_tokens`. |
| `voice_output.py` | `NSSound` playback, locks, live gain and mute/stop using shared preferences | Compiles only. Actual threaded playback, gain during playback, mute, stopping/failure and audio-device behavior are unverified. Do not claim reliable playback before a local audio trial. |
| `siri.py` | Calls shared answer payload for answers/reminders; voice-output routing; listening pause and recording-generation invalidation | Compiles; existing three provider tests pass. New behavior is not tested. Review recording/transcription races and pause/resume state; errors from new paths; all call sites. Background `warm_cache()` still generates audio even when voice is muted, despite the proposed suppression goal. Resolve that deliberately. |

No expanded app discovery, Safari navigation, targeted typing, capability catalog, recipe runner, local text bridge or configurable wake phrase has been implemented. Do not infer any of those from the documents.

Existing provider/Keychain code was not changed. Keychain service is `com.jevsiri.keys`; secrets remain hidden. Provider choices remain separately stored; `.env` takes priority. New WIP non-secret preferences use `com.heyjev.preferences`. Do not copy/log keys or `.env` in handoffs, tests, commits or agent prompts.

## Tests and live evidence already obtained

- Python compilation passed for `assistant_ui.py`, `siri.py`, `model_settings.py`, `voice_output.py`.
- `.venv/bin/python -m unittest test_providers`: three existing tests passed. These prove only the tested provider routing behavior, not the new UI/audio/model controls.
- Read-only OpenRouter requests returned HTTP 200 for `/api/v1/models` and `/api/v1/model/anthropic/claude-haiku-4.5`; catalog returned 458 models with parameter metadata at that time. This was not a live chat-generation test.
- Inspected Safari's installed scripting dictionary for tabs, URL properties, JavaScript and search commands. No navigation or JavaScript-permission trial occurred.
- The existing app process was running before edits. It was not restarted to load them. The alias bundle reads this checkout at startup, so a later restart would load the incomplete local source.
- No new UI pixel review, voice audition, microphone trial, app inventory validation, bridge benchmark or Johnny acceptance occurred.

## Practical first implementation assignments

Do not start every item in the effort map. Use two builder streams at most initially, with one explicit integrator; follow Johnny's actual Buzz setup decisions.

**Native owner:** finish/review the WIP menu/settings/model/audio slice as a polished grouped/collapsible Settings window covering transcription, Jev provider/key, deeper answers/provider/model/advanced arguments, voice provider/gain/mute, wake/listening and app/menu behavior; add configurable wake phrase using the documented live-update design; launch the real app and give Johnny one coherent trial. Keep API/provider roles and Keychain entries intact. Add selectable Apple on-device transcription beside faster-whisper using the spec's availability/locale/permission gates and no-cloud-fallback rule. Trial SFSpeechRecognizer through PyObjC before considering a Swift helper. Ensure one recorder/backend and generation-safe live switching, or show an honest restart requirement. A native AppKit dropdown/popover is possible now; SwiftUI rewrite is not required.

**Discovery/control owner:** implement local installed-app inventory and deterministic name resolution; expose generic open/activate by resolved app identity. Add one Safari navigate adapter after that works. Do not dump the whole catalog into Jev or autogenerate executable adapters from scripting metadata. Work in separate modules to avoid competing `siri.py` edits.

**Integrator (can also be one builder):** owns `siri.py`, classifier questions, bounded argument parsing, shared execution queue/lock, action/result contract and honest step failures. Remove false compound-success behavior and add bounded native-call timeouts. Integrate one usable increment at a time. Build the minimal persistent text bridge alongside the common dispatcher and result contract as the first shared execution slice. Voice and local API text converge before classification; prove both through one simple app-open action before broadening discovery or sequences.

Suggested small contract: request ID and bounded command text; internal action name, typed arguments, concrete target reference and timeout; result request ID/state/detail/observed facts. Distinguish receipt from completion, and return clarification/confirmation states explicitly. Use the selected `jevctl` CLI and in-process same-user Unix socket, with restrictive permissions and peer-user verification. Follow [the version-1 protocol and lifecycle contract](COMMAND_ARCHITECTURE.md#selected-transport-and-client-contract), including text-only external commands, request-ID status/cancellation, bounded replay protection and explicit crash limitations. Bound payloads, queue capacity and waits; do not replay after caller timeout or bypass validation/confirmation. See the foundational interface in COMMAND_ARCHITECTURE.md. Each trusted adapter provides its own readiness/success check and effect/confirmation category. Use simple Python data structures; a framework is unnecessary.

Do not run multiple live GUI/audio workers concurrently. One controls the desktop for a bounded trial while others work offline. Preserve bundle identity/permissions; Johnny handles unexpected consent/authentication challenges.

## Human trial order

1. Agree the tiny action/result contract. In one running app, “open Safari” through voice and the local CLI reaches the same classifier/dispatcher/executor and returns an observed result. Confirm shared state, serial execution and unavailable/malformed-request behavior. No second assistant process.
2. Open three discovered installed apps missing from the old list through both entry points. Try duplicate/ambiguous names and an unavailable app. No silent wrong-target execution.
3. “Open Safari, then go to google.com” through voice and bridge. Verify location and precise failure reporting; stop after a failed step. A programmatic request cannot bypass consequential-action confirmation.
4. Inspect grouped/collapsible Settings and keyboard navigation. Actually transcribe with each available backend, test unavailable/permission states and offline Apple recognition, switch mid-utterance without stale/duplicate commands, audition voice volume/mute/sample stop, and verify provider/key preservation, model/advanced arguments, reopening, custom phrase and push-to-talk. WIP compilation does not establish these results.
5. One concrete Johnny-selected recipe. Measure bridge latency after correctness; no faster/cheaper claim without a baseline.

Per slice, leave one small meaningful offline check for tricky validation/failure behavior; use existing routing tests and Johnny's end-to-end trial. Avoid building a large speculative suite before he can try the feature.

## Git and release policy

Use focused implementation commits, push to Johnny's fork, and state what was actually tested. WIP preservation is not acceptance. Do not open upstream PRs until Johnny has tried and accepted the result. Upstream has no evident license; do not describe the fork as a separately redistributable product or begin distribution work without resolving that boundary.

Astra's implementation role is closed. It may clarify/update design if Johnny asks. Buzz workers own future coding under Johnny's direction. Buzz Git setup is complete; future implementation remains under Johnny's direction.
