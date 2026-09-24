# Hey Jev overhaul

Status: implementation in progress on `feat/menubar-model-settings`, based on `8c04746`.
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

### Local Codex bridge

Assess a persistent local interface for submitting text commands to the running app. Implement the smallest robust bridge if it removes repeated initialization and supports actual use. Prefer a same-user Unix socket with restrictive permissions over a network listener. Serialize commands with microphone/timer work, bound request size and timeouts, expose structured results, and make confirmation/unsupported behavior explicit. Do not create an unauthenticated TCP endpoint.

Retain `--text` compatibility. Add a CLI client if a persistent bridge is built. Measure observed end-to-end latency and report actual API usage where available. Do not claim faster/cheaper than Codex computer use without a comparable measured baseline.

## Sequencing and acceptance

1. Commit this specification and preserve existing user state.
2. Implement model settings and shared request construction; verify both deeper-answer paths with offline tests and live catalog readback.
3. Implement native menu/status/settings controls and independent playback; inspect actual UI and test gain/mute with local audio, without touching user keys.
4. Audit the command path, implement bounded Safari plans and error reporting, and test the concrete Safari/google.com case live.
5. Implement/evaluate persistent local bridge; test permissions, malformed requests, serialization, responses and latency.
6. Update this file and README with verified results and remaining limits. Run meaningful tests, commit focused changes and push to Johnny's fork.

Acceptance checks:

- Window and menu bar-only modes reopen status/settings and Quit reliably; preferences survive restart.
- Mode changes and pause discard stale recording/transcription work; microphone capture stops while paused.
- Voice gain affects only voice playback; mute acts immediately, skips future TTS, and does not change saved API credentials.
- Model search/selection and parameter validation work; fetch failure leaves settings usable and keeps the prior selection.
- Both OpenRouter answer call sites use the saved model and supported overrides. Default behavior remains unchanged until configured.
- Existing provider routing checks continue to pass.
- Safari open then navigate produces a verified URL or a precise failure. Multi-step execution stops after a failed step.
- Exact text is preserved within documented supported syntax. Generic site automation is not claimed without verified selectors and success evidence.
- Local bridge only accepts same-user bounded requests, reports completion/errors, and does not bypass consequential-action confirmation.
- Tests do not use or print production keys; visual/audio checks distinguish programmatic evidence from human audition.

## Constraints and open decisions

- No upstream license was evident. Do not treat this fork as a separate redistributable product.
- No upstream PR until Johnny tries and accepts the changes. Authorized workflow is implement, test, commit and push to his fork.
- Preserve unrelated state, keys and provider settings. Never print credentials.
- Hindsight service was unavailable at inspection; current source files and the explicit task brief govern this implementation.
- Generic website interaction depends on permissions, DOM access and site semantics. The exact verified subset will be recorded here after investigation.
- Listening pause does not promise cancellation of actions already executing. UI and documentation must state the boundary.
- Menu design uses native Cocoa controls and existing dependencies. Do not add speculative agent frameworks or arbitrary Mac execution.
- Persistent bridge design and latency claims remain provisional until tested.

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
