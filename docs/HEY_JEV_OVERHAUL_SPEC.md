# Hey Jev overhaul

Status: proposed overhaul; Astra owns documentation only, Buzz agents own implementation. Spec branch: `feat/menubar-model-settings`, based on `8c04746`.
Owner: Johnny's fork, `legionsound/hey-jev`. Actual checkout: `/Users/johnmeyer/Developer/Hey Jev`.

## Current architecture and command inventory

The app is Python with a native PyObjC status window and Keychain-backed provider keys. `app.py` starts `assistant_ui.py`; `siri.py` owns microphone capture, local Whisper transcription, typed Jev decisions, actions, timers, Fish speech and optional OpenRouter answers. The existing bundle uses alias mode and reads this checkout at launch.

Jev uses either OpenRouter's decisions API or TypeSafe direct. Its typed choice/score results classify intent and fixed arguments; they do not supply arbitrary URLs, text or executable code. Existing compound handling asks for two fixed action slots. This explains Johnny's failed “open Safari, then go to google.com”: opening Safari is supported; navigating is not.

Existing commands:

- Open or quit Spotify, Slack, Chrome, VS Code, Finder, Safari, Messages and Notes.
- System volume and Spotify volume: up, down, mute, unmute and five fixed levels.
- Media: play, pause, next and previous.
- Display: dark mode on, off and toggle.
- System: lock and sleep.
- Timers/reminders: set, inspect, cancel one/all, with spoken completion and window countdowns.
- Short scripted conversation or optional deeper answers through OpenRouter.

The existing deeper-answer model is fixed to Claude Haiku 4.5 in both answers and reminder preparation. Fish WAV files are cached and played through `afplay`. Right Option push-to-talk and wake phrase modes run beside the native UI. `siri.py --text` starts a fresh process for one text turn; no persistent command bridge exists yet.

## Goals

### Two co-equal foundations

Build reusable, discoverable Mac capabilities and a local command interface through which Codex/ChatGPT can command the running Hey Jev app. Neither is a late optional add-on. One running app owns state and execution: voice transcripts and typed/API text requests converge before Jev classification and deterministic planning, then use the same validated dispatcher and structured results. The bridge is a local integration point for a same-user Codex/ChatGPT client with local tool access, not a claim that a remote ChatGPT session can reach this Mac automatically.

### Native everyday controls

Provide a polished native status window and an always-discoverable menu bar dropdown. Menu bar-only mode is optional and persistent. Closing a window must not remove access to settings or Quit. Keep a separate full settings view for credentials, provider roles and model controls.

Everyday controls include listening mode, pause/resume, independent voice output gain and voice off/mute. Voice gain must not change system or Spotify volume. Mute stops current spoken playback and suppresses future speech generation/playback. Timer alert sounds may remain separate, but the UI and documentation must say so. Existing Keychain entries and separate provider routes must survive.

### OpenRouter model settings

Rename the provider label to OpenRouter. Fetch the full text-capable model catalog from `https://openrouter.ai/api/v1/models` after an answer key is available. Provide search, selection, refresh, loading/error states and an offline fallback preserving the saved model. Persist model selection and validated advanced overrides for both ordinary answers and reminder preparation.

Use model `supported_parameters`, context length and maximum completion metadata to enable controls and reject invalid inputs. Initial controls: output token limit, temperature, top P, top K, frequency penalty, presence penalty and seed. Blank sampling fields omit the argument. Preserve Haiku and the existing 80-token answer / 120-token reminder budgets until changed. Clearly explain that reasoning models may need larger budgets. Only request JSON mode for reminders where advertised. Do not expose a raw arbitrary JSON configuration or allow overriding credentials/endpoints.

Official references (verified 2026-09-24):

- https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties
- https://openrouter.ai/docs/api/api-reference/models/get-a-model-by-its-slug
- https://openrouter.ai/docs/api_reference/parameters

Live unauthenticated checks returned HTTP 200, 458 catalog models, `supported_parameters` and per-model limits. The single-model endpoint is `/api/v1/model/{author}/{slug}`. Catalog requests omit pagination to request the full list. API credentials must only reach intended fixed HTTPS hosts; reject redirects rather than forwarding secrets.

### Bounded multi-step commands

A short utterance must produce a bounded sequence of explicit actions, not raw shell commands. First acceptance case: “open Safari, then go to google.com.” Add exact URL/text extraction for supported grammar and preserve literal supplied text. Handle opening an app, navigating a supplied HTTP(S) website, typing supplied text, and a concrete supported subsequent browser action.

Each step needs explicit preconditions, bounded readiness waits, observed success/failure and stop-on-failure behavior. Validate URLs and action arguments before execution. Do not silently fall back from a partially executed plan into the old compound path and repeat actions. Confirmation is required before consequential actions such as submitting a message, purchasing or deleting. Unsupported actions must remain unsupported instead of guessing where to click.

Keep responsibilities clear:

- Jev: fast typed intent/choice decisions and existing bounded command routing.
- Deterministic code: exact extraction where grammar is unambiguous, validation, plans, execution and verification.
- Optional generative model: only when arbitrary structured language interpretation is actually necessary; never treat generated code or text as execution authority.
- Browser automation: a verified Safari subset, not arbitrary website control. Investigate native AppleScript navigation and the limits of text entry/site actions without enabling new browser permissions silently.

### Foundational local command bridge

Implement a minimal persistent local interface and CLI client for sending text commands into the single running app. The selected architecture is a standard-library Unix domain socket server inside the existing app process plus a tiny `jevctl` CLI, in a private same-user directory with restrictive permissions and peer-user verification. No unauthenticated TCP listener. Bound request size, queue length and deadlines; reject malformed or unauthorized requests before classification. Do not start a second assistant, model engine or agent framework per request.

Voice transcripts and bridge text enter one request queue before Jev classification, deterministic argument resolution and planning. Serialize command execution against the same app/timer state. Return request-correlated structured completion, failure, unsupported, clarification or confirmation results with observed facts. An acknowledgement is not completion. A caller timeout does not prove cancellation; never blindly retry a possibly executed action. Neither caller identity nor an API field bypasses validation, target checks or consequential-action confirmation. Confirmation must refer to the pending concrete action in the running app.

The version-1 text-only JSON envelope, per-step results, request-ID ledger, bounded queue, cancellation/replay semantics and socket startup/shutdown rules are defined in [the selected technical contract](COMMAND_ARCHITECTURE.md#selected-transport-and-client-contract). Typed actions remain internal; an optional future MCP adapter would wrap the same client.

Existing `siri.py --text` is a one-shot process with no shared running state or persistent timer loop. Preserve or clearly document compatibility, but do not treat it as this bridge. The new CLI talks to the running instance and reports unavailable when it is absent. Measure latency only after the shared path works; do not claim faster/cheaper operation without a comparable baseline.

## Sequencing and acceptance

1. Agree a tiny shared action/result contract and one queue/dispatcher owner; preserve existing user state.
2. Implement the running-app text bridge and converge voice/text before classification. Prove one simple `app.open` command through both inputs with the same validation, executor and observed result. Use an existing supported app for this first slice.
3. Grow installed-app discovery and deterministic target resolution behind that same action. Then add short stop-on-failure sequences, starting with Safari open/navigation. Exercise each through voice and the bridge.
4. Finish native menu/settings, configurable wake phrase, model settings and independent playback as separate slices sharing the same running engine. Verify both deeper-answer paths and real UI/audio behavior.
5. Expand one concrete recipe from Johnny's trial. Record verified results, remaining limits and focused checks; publish through the agreed Buzz/GitHub workflow.

Acceptance checks:

- Window and menu bar-only modes reopen status/settings and Quit reliably; preferences survive restart.
- Mode changes and pause discard stale recording/transcription work; microphone capture stops while paused.
- Voice gain affects only voice playback; mute acts immediately, skips future TTS, and does not change saved API credentials.
- Model search/selection and parameter validation work; fetch failure leaves settings usable and keeps the prior selection.
- Both OpenRouter answer call sites use the saved model and supported overrides. Default behavior remains unchanged until configured.
- Existing provider routing checks continue to pass.
- Safari open then navigate produces a verified URL or a precise failure. Multi-step execution stops after a failed step.
- Exact text is preserved within documented supported syntax. Generic site automation is not claimed without verified selectors and success evidence.
- Both voice and a local Codex/ChatGPT client open the same app through one running dispatcher and return equivalent structured outcomes. Timer/app state is shared; simultaneous requests execute serially.
- Local bridge rejects wrong-user/malformed/oversized requests, reports completion/errors, and does not bypass validation or consequential-action confirmation. Client timeouts do not trigger replay.
- Tests do not use or print production keys; visual/audio checks distinguish programmatic evidence from human audition.

## Constraints and open decisions

- No upstream license was evident. Do not treat this fork as a separate redistributable product.
- No upstream PR until Johnny tries and accepts the changes. Authorized workflow is implement, test, commit and push to his fork.
- Preserve unrelated state, keys and provider settings. Never print credentials.
- Hindsight service was unavailable at inspection; current source files and the explicit task brief govern this implementation.
- Generic website interaction depends on permissions, DOM access and site semantics. The exact verified subset will be recorded here after investigation.
- Listening pause does not promise cancellation of actions already executing. UI and documentation must state the boundary.
- Menu design uses native Cocoa controls and existing dependencies. Do not add speculative agent frameworks or arbitrary Mac execution.
- The bridge is required foundation work; exact transport details and latency claims remain provisional until tested.

## Implementation evidence

Pending. Record built, tested and unverified outcomes separately before handoff.

## Architecture recommendation

**Near term: keep Python and use native AppKit through PyObjC.** `NSStatusItem`, native controls and `NSPopover` can provide a polished compact menu experience with a separate settings window. PyObjC calls the same native framework; Python does not impose a visual quality ceiling. Use a native dropdown for this iteration and keep controls compact. A custom popover is possible if interaction testing shows the menu is too constrained. Do not rewrite the working voice engine merely to change its presentation.

**Long term: prefer a SwiftUI/AppKit shell with a separate Python engine if this becomes a maintained, distributed macOS product.** SwiftUI `MenuBarExtra` with window style and a `Settings` scene make the desired utility structure idiomatic. AppKit remains available for finer control. A narrow local command/status interface can later connect that shell to the existing Whisper/Jev/Fish engine. First stabilize that boundary; do not port model inference and provider logic just to obtain SwiftUI views.

| Concern | Python + native AppKit | SwiftUI/AppKit shell + Python engine |
| --- | --- | --- |
| UI polish | Native views, menus and popovers; manual layout and callback code need care | Declarative state/layout and tooling make iteration easier; polish still needs design/testing |
| macOS permissions | Current bundle identity and microphone/Accessibility/Automation grants can be retained | Shell/helper identity and ownership of microphone/automation must be designed; a new signed binary may prompt again |
| Packaging | Existing alias bundle is local-development packaging, not a self-contained signed release | Native shell is straightforward to sign, but shipping/versioning the Python runtime and engine still takes work |
| Background audio/voice | Existing recorder, Whisper and Fish remain intact; native playback can be used directly | Keep engine off the main actor; choose explicit ownership of capture/playback and lifecycle |
| Maintenance | Fewest moving parts now; PyObjC signatures and manual view state are less ergonomic | Clear typed UI/state and Xcode tools; an IPC protocol plus two languages adds operational surface |
| Migration risk | Low, preserves tested routes and keys | Moderate/high until helper startup, crash recovery, permissions and upgrades are tested |

SwiftUI is easier to maintain for a larger native utility, not a technical prerequisite for a BetterDisplay-style experience. A full Swift engine rewrite is not recommended without evidence that Python startup, distribution or runtime costs dominate. The proposed local bridge is useful now and can inform a future engine boundary, but must not grow into a speculative framework in this overhaul.

Apple references: https://developer.apple.com/documentation/appkit/nsstatusitem ; https://developer.apple.com/documentation/appkit/nspopover ; https://developer.apple.com/documentation/swiftui/menubarextra ; https://developer.apple.com/documentation/swiftui/settings .

## Discussion checkpoint: implementation paused

Johnny requested an architecture/capability rundown before further major coding. See [COMMAND_ARCHITECTURE.md](COMMAND_ARCHITECTURE.md) for the exact speech-to-action path, command mechanisms, current failure behavior, theoretical reach, recipe/registry proposal and human-testing sequence. Expanded Safari/typing/bridge implementation has not started. Original-scope UI/model/playback edits are uncommitted and only compile/provider checks have run; they are not yet a verified usable build. Continue only after Johnny has discussed the rundown. Prefer small increments with Johnny as the primary end-to-end tester and minimal necessary automated checks.


## Scope refinement: deterministic control and wake phrase

Johnny clarified that expanded control stays within the Jev fixed-question/classifier plus Python harness pattern. Generative command planning, arbitrary cursor movement and visual clicking are outside the current core. Optional OpenRouter deeper answers remain separate. Prefer reusable action types with deterministic argument resolution over exhaustive command/app lists.

Prioritize opening any discovered installed app: runtime bundle discovery plus Spotlight/running-app sources, normalized name matching, ambiguity handling and native launch verification. No app-per-choice Jev schema or maintained app database is required. Known-bundle-ID Launch Services APIs help resolve/launch apps; they are not themselves a complete installed-app enumeration API. See [the capability design](COMMAND_ARCHITECTURE.md#proposed-core-fixed-jev-questions-dynamic-arguments) for discovery limits, schema scaling, noncoder recipes and deterministic menu/button targeting.

Add a **user-configurable wake phrase** to Settings. Preserve “Hey Jev” and push-to-talk defaults. Store a non-secret preference, match normalized complete phrase tokens at transcript start, escape literal input, and use the same setting for the Whisper prompt and UI hints. Prefer live application through the worker controls queue, clearing old armed/queued transcription state. Default-only aliases preserve current recognition tolerance; arbitrary custom phrases do not inherit Jev aliases. Short/common phrases can false-trigger, and the current English-focused Whisper model cannot guarantee every language/name. See [wake phrase design](COMMAND_ARCHITECTURE.md#proposed-configurable-wake-phrase) for UI, persistence, update behavior and small human-centered acceptance checks.

These are proposed requirements, not implemented behavior. Discussion pause remains in effect; no major coding resumes until Johnny has reviewed the design.

The discussion also includes [CAPABILITY_EFFORT_MAP.md](CAPABILITY_EFFORT_MAP.md): a proposed control vocabulary, qualitative effort estimates, native permissions and limits, human trials and optional parallel workstreams. Implementation remains paused; no Buzz channel or parallel workers were created.

## Implementation handoff and machine discovery

Johnny has selected **Astra for spec/design only** and agents in a Buzz project on his Claude subscription for implementation. No feature coding resumes in this task. See [BUZZ_HANDOFF.md](BUZZ_HANDOFF.md) for preserved WIP code, branches, exact evidence and practical assignments.

One co-equal foundation is the running-app bridge described above. The other is a machine-derived local catalog: discover apps/declared interfaces/device metadata, refresh it, resolve targets locally and send Jev only relevant bounded choices. Do not hardcode app names or confuse metadata discovery with permission, a usable adapter or verified completion. Live menus/fields/DOM targets require on-demand observation and revalidation. The [catalog design](CAPABILITY_EFFORT_MAP.md#central-product-design-discover-this-mac-then-offer-bounded-choices) specifies sources, opt-ins, privacy, refresh/rebuild, candidate size and evidence states.

For recoverable cross-runtime handoff, the four pre-existing incomplete source edits were preserved and pushed as `6b9fe89` on `wip/native-controls-incomplete`. The active spec branch and local working edits were left intact. This is an explicitly incomplete snapshot, not a finished build. Further commits on the spec branch remain documentation only; no upstream PR was opened.
