# Deterministic Mac control: capability and effort map

Discussion draft, 2026-09-24. **Implementation remains paused.** This is a design/effort estimate, not a claim that these controls are built. See [COMMAND_ARCHITECTURE.md](COMMAND_ARCHITECTURE.md) for current code paths and [HEY_JEV_OVERHAUL_SPEC.md](HEY_JEV_OVERHAUL_SPEC.md) for the complete brief.

## Recommendation

Build a small reusable control vocabulary, not a catalog of every sentence or a general computer-use agent. Jev classifies action types. Python discovers entities, extracts arguments, executes trusted adapters and checks results. This can cover a broad range of useful Mac operations without an LLM planner or vision loop.

First control slice: **open any discovered installed app**, then **open Safari and navigate to a supplied URL**. Add one actual Johnny workflow after he tries those. Keep the native menu/settings work as a separate slice. A fleet of agents is premature before these contracts and first trials; two workers plus one integrator could help afterward.

## Estimate assumptions

Effort labels compare implementation scope, not delivery dates or benchmarked agent performance:

- **S:** one focused edit-and-human-trial cycle, using an API already present or a well-understood native primitive.
- **M:** several focused cycles; discovery, state management, target resolution or permissions need integration.
- **L:** an open-ended family of adapters/platform edge cases. Narrow the supported subset before committing to it.

A prototype means one happy path on Johnny's Mac. A robust version means bounded failures and useful coverage for an explicitly named subset, not universal support across every app, OS version and permission state. Estimates assume the existing Python environment works, Johnny is available for permission/behavior trials, and no sandboxed App Store distribution is required. Signing/distribution and OS-wide compatibility testing are separate work. These estimates are engineering judgment, not measured timelines; they should not be summed into a project quote.

## Atomic blocks

All proposed blocks below are feasible **without an LLM/vision loop when their arguments and targets are explicit**. Ambiguous names, missing targets and unsupported UI must fail or ask for clarification.

| Block and current state | Reusable core vs adapter | Prototype / robust effort | Permissions and ceiling | Johnny's useful trial |
| --- | --- | --- | --- | --- |
| **Discover and open apps.** Current: eight fixed app names; `open -a`, weak wait. | Generic inventory from standard roots, Spotlight and running apps; normalize bundle names, resolve a unique URL, launch through NSWorkspace. Optional aliases, no app-per-choice Jev schema or maintained DB. | **M / M.** Discovery and duplicate handling are more work than launching. | Ordinary app discovery/launch usually needs no Accessibility grant. Unindexed/nonstandard/external locations and duplicate versions need explicit handling. Running process does not mean document/UI ready. | Open an app absent from the old list, an app with spaces/version in its name, and an intentionally ambiguous name. |
| **Activate and quit apps.** Current: fixed-name AppleScript quit; no readback. | Generic running-app resolution and activation/normal termination. Per-app wait policy when save dialogs intervene. | **S / M.** Reuse app resolver. | NSRunningApplication can avoid Apple Events for basic activation/termination; AppleScript route needs Automation. Unsaved-document prompts remain user decisions. Do not force-kill to manufacture success. | Quit a harmless app, then an editor with an unsaved scratch document; verify correct pending/failure report. |
| **Open URLs and manage browser tabs.** Current: no navigation/tab actions. | URL validation/encoding is generic. Default-browser URL opening is generic; specific tab create/select/read/close needs a Safari or other browser adapter. | **S / M** for Safari; **L** for all browsers. | Safari Apple Events need Automation. URL readback proves location, not finished render. Redirects, consent/login pages and downloads can change outcomes. Closing tabs may lose unsaved state. | “Open Safari, then go to google.com”; test already-open Safari, a redirect and a bad/unreachable URL. |
| **Type text and send explicit keystrokes.** Current: only fixed lock-screen shortcut. | Focus/target checks and Unicode text handling are generic; accessible-field setters or known key sequences vary by target. Dictated text must be isolated from command grammar. | **S / M** for a named editable field; **L** for arbitrary fields. | Accessibility normally required. Secure fields, custom editors, focus races and keyboard layouts limit keystroke fallback. Clipboard paste can disturb clipboard state; avoid it unless required and preserve state deliberately. Enter may submit, so it is a separate action. | Type punctuation/Unicode into a scratch field; change focus before execution and confirm refusal rather than wrong-target typing. |
| **App scripting, URL schemes and CLI operations.** Current: Spotify, volume, dark mode and fixed OS commands. | Shared validation, bounded subprocess/Apple Event calls and results are generic. Each application's supported commands/schema are adapters. | **S per simple operation / M per app**; some apps are **L/unsupported**. | Apple Events need Automation; CLI/file/network access follows that tool's permissions. A URL scheme's existence does not imply completion can be observed. Never interpolate raw speech as executable code or run arbitrary named tools. | Choose one real operation in a chosen app, test success and unavailable app/document state. |
| **Run a named macOS Shortcut.** Current: not present. | Resolve a registered user-approved shortcut, validate inputs, run it and return its result. Effects/confirmation policy belong to each approved shortcut. | **S / M.** | Shortcuts and contained actions may request their own permissions or display UI. A shortcut can have broad side effects; its name/exit status does not prove every intended effect. | A harmless shortcut returning text or opening a known folder, then one requiring unavailable input. |
| **Accessibility menu paths.** Current: no general menu traversal. | Find app/window, walk exact menu labels, perform supported action. Localization and dynamic menu names may require per-app aliases/adapters. | **M / M** for named paths; **L** for broad cross-app coverage. | Accessibility; System Events scripting also adds Automation. Disabled/duplicate/contextual menu items must be reported. A menu click alone may not prove the operation finished. | Choose File > New Window in one app; try a disabled item and duplicate/localized label. |
| **Accessibility buttons and fields.** Current: not present. | Traverse role/name/identifier under an explicit app/window; require a unique target; press/set value; observe a known postcondition. Per-app targeting rules often needed. | **M / L.** Start with one window/control. | Accessibility. Custom canvas controls, missing labels, virtualized content and stale elements set a hard ceiling. “Click this” has no deterministic meaning without explicit context. | Press a uniquely named harmless button, then a duplicate-name case; read back a field value. |
| **Known website search/forms.** Current: not present. | Encoded search-URL template is small reusable plumbing with per-site templates. DOM field/submit interaction needs per-site selectors and postconditions. | **S / M** for URL search; **M / L** for forms. | Search navigation uses browser Automation. Safari DOM JavaScript additionally needs user-enabled JavaScript from Apple Events. Login, consent, cross-origin frames and site changes limit coverage. Do not claim generic form control. | One Google search recipe first. If needed later, one scratch form: fill without submitting; verify exact text. |
| **Files and folders.** Current: no file actions. | Resolve explicit paths or selected Finder items; open/reveal/create/copy/rename/move using native/stdlib APIs. Relative-name search and conflict policy are separate reusable pieces. | **S / M** for explicit paths; **L** for semantic “that file” discovery. | Files & Folders access for protected locations; Finder scripting adds Automation. Full Disk Access is not a blanket prerequisite and should not be requested preemptively. Collisions, cloud placeholders, symlinks and cross-volume operations need handling. Deletes/overwrites require concrete confirmation. | Dedicated scratch folder: create, copy and rename; collision case must preserve both originals. |
| **Windows.** Current: Hey Jev can keep its own window on top; no other-app window control. | Generic activate/minimize/move/resize via accessible window attributes, with explicit display/window selection. App exceptions need adapters. | **M / L** across apps. | Accessibility. Full-screen windows, Spaces/Stage Manager and apps that reject geometry changes restrict behavior. No universal promise for every window. | Two ordinary windows on Johnny's actual display setup; test minimized/full-screen exceptions. |
| **Display/system controls.** Current: dark mode, lock and sleep. | Known API/command per operation, typed amounts/toggles, state readback when available. Display-specific controls may require device adapters. | **S / M** for existing controls; **L** for arbitrary monitors/settings. | Dark mode Apple Events need Automation; keyboard lock route needs Accessibility. Some settings are protected or device-specific. Do not infer external-monitor brightness support from laptop support or reproduce all BetterDisplay functions. | Dark mode readback first. Lock/sleep only when Johnny chooses an interruption-safe moment. |
| **Audio/media.** Current: Mac/Spotify gain, Spotify transport; independent voice gain/mute source edits unverified. | Generic system gain and voice playback; per-player transport/app volume adapters. Device output controls need explicit device selection. | **S / M** for existing audio/player subset; **L** for all players/devices. | Spotify Apple Events need Automation. No microphone permission needed for output-only controls. Digital/fixed-volume devices may reject system gain; Spotify is not universal media transport. | Voice gain versus system gain during a reply; Spotify transport; actual output device. |
| **Recipes, sequencing and verification.** Current: two Jev slots, partial failure can still claim both succeeded. | Shared typed steps, argument validation, bounded waits, stop-on-failure and honest results. Each adapter supplies readiness/success predicates. Recipes compose existing actions. | **M / M** for short linear recipes; **L** if expanded into branching/recovery planner. | Inherits step permissions. No universal rollback. Do not retry non-idempotent actions blindly. Confirmation belongs to concrete consequential steps. | Safari open+navigate; deliberately fail step two and verify step three never runs. |
| **Persistent text bridge.** Current: fresh-process `--text`; no shared timer loop/state. | Same-user Unix socket plus tiny CLI; bounded input, structured results, single execution owner, explicit timeouts/request identity. Uses same dispatcher as speech. | **M / M.** Main cost is lifecycle/serialization, not socket code. | Restrictive filesystem/socket access and peer identity checks; no TCP listener. Bridge inherits app capabilities/permissions and must not bypass confirmation. Timeout must not trigger silent replay. | Two queued text commands plus a microphone turn; stopped app; malformed request; timeout. Measure latency before comparisons. |

## What the code review changes about the estimate

The action registry already exists as `ACTIONS` in `siri.py`, and `handle()` is shared by microphone and `--text`. Reuse those seams. The useful first change is to replace internal tuple assumptions with a small explicit result/step contract, not build a plugin platform.

Existing `open_app()` polls for five seconds but does not raise when readiness never arrives. `spotify_play()` already has a meaningful bounded readback loop. The dispatcher continues after failure and can produce a misleading compound success reply. These examples show that reliable sequencing needs adapter-specific checks, not merely a sleep between commands.

`osa()`/`sh()` currently have no subprocess timeout. The current voice loop has a local busy lock and timer workers; a persistent bridge must join that same execution boundary rather than launch independent `handle()` calls that can collide with microphone/timer work. File operations, broad Accessibility traversal and browser plans have no current implementation to extend. Do not count them as nearly complete because Python can call the underlying APIs.

## How Jev stays small

Do not send every installed app or every accessible control in every Jev request. Runtime entities are Python's job. A command can follow this path:

1. Jev chooses a bounded action type/domain, for example app/open or browser/navigate.
2. Python extracts the supported argument span and resolves it against local candidates: installed app names, a validated URL, a number, an explicit menu path or recipe name.
3. The executor checks one concrete target, performs a trusted operation and reports observed completion.

Keep the current one-call classification style for a small vocabulary. If actual measurements show a large schema harms accuracy/latency, try domain classification followed by a smaller fixed domain schema. Hierarchical routing adds a round trip; it is not automatically faster. For many named recipes, deterministic name/alias/keyword retrieval can shortlist candidates before Jev chooses. Preserve a none/unclear result; a shortlist is not evidence that one candidate must be correct.

Dynamic Accessibility candidates could also be ranked by Jev, but observation and unique target validation still belong to Python. That is an optional bounded extension, not needed for initial explicit menu paths. A fixed classifier cannot invent missing URLs, recover arbitrary misheard names, understand an unobserved screen or infer the intent of a novel website. Ask for a clearer instruction at that boundary. No LLM planner is proposed.

## Noncoder expansion

Johnny should be able to add a recipe by describing a few existing actions, naming it and trying it. Codex can initially write the small JSON recipe; a step-picker UI is optional later. Recipe fields are a name/aliases, typed inputs, ordered actions and concrete completion/confirmation requirements. A recipe can reuse `app.open` for any resolved application. It cannot acquire a new API simply by naming an unsupported operation.

For a new capability, one small trusted adapter is still necessary. Its action/argument contract becomes reusable across many recipes. Keep “add a new phrase/recipe” cheap and distinguish it from “implement support for this app's custom UI.”

## Would parallel agents help?

**Yes for independent adapters or UI versus engine, after a short contract agreement. No for several agents editing the current `siri.py` and driving Johnny's desktop simultaneously.** Most uncertainty is human trial and native state, so more workers will not scale linearly. A Buzz channel is an organizational choice, not a prerequisite for this code.

A practical split, if Johnny chooses it:

| Workstream | Owns | Integration contract |
| --- | --- | --- |
| **Native experience** | Menu/status/settings, voice gain/mute, model controls and wake phrase UI | Emits configuration/control messages; receives status/results. Does not add command executors. |
| **Deterministic capabilities** | App resolver; Safari navigate/tab adapter; one chosen recipe | Accepts validated typed steps and returns structured completed/failed/unsupported results with observed facts. Does not own UI or microphone loop. |
| **Integrator/runtime owner** | `siri.py` routing, schema/argument extraction, execution serialization, confirmation, later bridge | Owns the shared entrypoint and result contract, reviews adapters, merges one usable increment and coordinates Johnny's trial. |

Start with two active builders at most, with one person/agent clearly owning integration; the integrator can also build one stream. Consider another adapter worker only once there is a concrete independent target. Use separate branches/worktrees if parallel implementation is approved, but preserve the existing uncommitted UI work first rather than having two workers rewrite it. Do not create those worktrees or a Buzz channel during this paused discussion.

Agree on a tiny contract before splitting work: action name, argument schema, target identity, timeout, executor, success check, confirmation category, and a result carrying state/detail/observed values. Python dataclasses/dicts and the existing queue are enough. No event bus, workflow language, distributed scheduler or general plugin framework is warranted now.

Only one worker should perform live GUI/audio trials at a time. Other workers can run offline validation or inspect code. Native permissions and app identity should be coordinated through the same app bundle; testing from multiple unsigned executables can produce misleading permission results.

## Quick human-tested sequence

1. **Finish the original native controls/model work** and do one launch check; Johnny tests the feel and audio. Include configurable wake phrase as a small follow-up or part of that coherent UI slice.
2. **Any-app opening:** Johnny names three real apps outside the old list, including one ambiguous/versioned app. One compact automated resolver check protects collisions and missing apps.
3. **Safari sequence:** “open Safari, then go to google.com.” Verify URL and failed-step reporting. One offline URL/step-failure check, one live trial.
4. **One chosen practical recipe:** Google search, a named app menu operation or scratch-field text entry. The recipe determines the next adapter, not a promise to implement the entire table.
5. **Bridge if useful:** submit repeated text commands from Codex to this working dispatcher. Check lifecycle/serialization and record measured latency; do not claim cheaper/faster without comparison data.

## Unknowns and current state

Not inspected yet: completeness of Johnny's local app inventory, duplicate names, Safari Automation/JavaScript permission for the chosen app identity, accessible controls in target apps, preferred recipe, multi-display behavior and actual bridge latency. No live expanded-control trial has occurred. Resolve these through small read-only discovery and Johnny-led trials after approval to resume.

Only documentation is committed/pushed at this checkpoint. Existing UI/model/playback source edits remain uncommitted; compilation and the three existing provider tests passed, but native UI/audio behavior remains unverified. No expanded-control/bridge implementation, agents, Buzz channel or upstream PR was created for this map.
