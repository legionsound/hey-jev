# Buzz implementation handoff

Prepared 2026-09-24. **Astra owns spec/design only from this point. Johnny will use agents in a Buzz project on his Claude subscription for implementation.** This task has not created that project/channel or started its agents. No further feature code is authorized in this Astra task.

## Product goal

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

**Native owner:** finish/review the WIP menu/settings/model/audio slice; add configurable wake phrase using the documented live-update design; launch the real app and give Johnny one coherent trial. Keep API/provider roles and Keychain entries intact. A native AppKit dropdown/popover is possible now; SwiftUI rewrite is not required.

**Discovery/control owner:** implement local installed-app inventory and deterministic name resolution; expose generic open/activate by resolved app identity. Add one Safari navigate adapter after that works. Do not dump the whole catalog into Jev or autogenerate executable adapters from scripting metadata. Work in separate modules to avoid competing `siri.py` edits.

**Integrator (can also be one builder):** owns `siri.py`, classifier questions, bounded argument parsing, shared execution queue/lock, action/result contract and honest step failures. Remove false compound-success behavior and add bounded native-call timeouts. Integrate one usable increment at a time. Add the persistent text bridge only once the common dispatcher and result contract exist.

Suggested small contract: action name, typed arguments, concrete target reference, timeout and result state/detail/observed facts. Each trusted adapter provides its own readiness/success check and effect/confirmation category. Use simple Python data structures; a framework is unnecessary.

Do not run multiple live GUI/audio workers concurrently. One controls the desktop for a bounded trial while others work offline. Preserve bundle identity/permissions; Johnny handles unexpected consent/authentication challenges.

## Human trial order

1. Native menu/settings open correctly; volume changes only voice; mute and pause do what labels promise; status/settings reopen in menu-only mode; custom phrase and push-to-talk work; another model answers a question.
2. Open three installed apps missing from the original eight-name list. Try duplicate/ambiguous names and an unavailable app. No silent wrong-target execution.
3. “Open Safari, then go to google.com.” Verify location and report any failure precisely. A failed second step must not claim the whole sequence succeeded.
4. One concrete Johnny-selected recipe, such as Google search or a named app menu operation. Expand only from the trial result.
5. Persistent text bridge with same-user access, serialized execution, explicit results and measured latency. No faster/cheaper claim without a baseline.

Per slice, leave one small meaningful offline check for tricky validation/failure behavior; use existing routing tests and Johnny's end-to-end trial. Avoid building a large speculative suite before he can try the feature.

## Git and release policy

Use focused implementation commits, push to Johnny's fork, and state what was actually tested. WIP preservation is not acceptance. Do not open upstream PRs until Johnny has tried and accepted the result. Upstream has no evident license; do not describe the fork as a separately redistributable product or begin distribution work without resolving that boundary.

Astra's implementation role is closed. It may clarify/update design if Johnny asks. Buzz workers own future coding under Johnny's direction. No Buzz channel was created by this task.
