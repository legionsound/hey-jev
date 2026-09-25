# Engine contract v1.2

Status: implemented in release 0.3.0 (2026-09-25). v1.2 corrects the
v1.1 draft against the built code: effect categories and defaults now
match `actions.py: EFFECTS` / `DEFAULT_POLICY`, row ordering matches
`screen.py` (12 pt bands), bridge limits match `bridge.py`, and the
"not yet built" sections are retired. Implements the foundation in
`COMMAND_ARCHITECTURE.md` (historical); where this file is more
specific, this file wins.

## Shape

One engine inside the running app. Voice and `jevctl` both hand it text; it plans, asks, runs and reports. Legacy `handle()`, `decide()`, `split_actions()` and the `ACTIONS` table are replaced, not wrapped.

| File | Owns |
| --- | --- |
| `actions.py` | Action registry and native adapters. |
| `planner.py` | Text to ordered steps: clause split, Jev classify per clause, argument extraction. |
| `engine.py` | Serial worker, queue, ledger, confirmation gate, step runner, results. |
| `bridge.py` | Unix socket server. Validates and enqueues only. |
| `jevctl` | CLI client. |
| `app_catalog.py` | Installed-app discovery and name resolution (Muse Spark). |

`siri.py` keeps microphone, transcription, speech and UI wiring. It submits text to the engine and speaks from the returned result.

## Action entry

```python
"app.open": {
    "effect": "open",          # policy category, see Confirmation
    "resolve": resolve_app,    # args -> ("target", {...}) | ("choices", [...]) | ("none", reason). Deterministic.
    "run": run_app_open,       # (target, deadline) -> None. Raises on definite failure.
    "verify": verify_app_open, # (target, deadline) -> ("done" | "wait" | "failed" | "unverified", facts). Polled until deadline.
    "proves": "a running app with the resolved bundle id and path",
    "timeout": 10,
}
```

`verify` may be `None` only where no readback exists (lock, sleep). Native calls run two ways: subprocess calls (`run`/`verify` in `actions.py`, e.g. `open`, `osascript`) carry `timeout=deadline - now`, and at the deadline the worker kills and reaps its own subprocess and reports `unknown`. Screen/AX calls (`screen.py`) run on daemon threads via `bounded()`: a thread cannot be killed, so a call that outlives its deadline is abandoned and tracked, and nothing new dispatches while an abandoned effect is still out (the pending-effect barrier). Either way, killing a subprocess or abandoning a thread does not recall an Apple Event or AX action already delivered to another app, so the effect may still happen later; the engine never claims external work stopped.

## Step outcome

| State | Meaning |
| --- | --- |
| `completed` | `verify` returned `done`. The only success. |
| `unverified` | `run` returned, and either no `verify` exists or `verify` returned `unverified` (a readback that cannot prove the effect, such as a redirect). Not success. |
| `unknown` | Deadline hit, process died, or any error after the effect was dispatched (the native call itself exited non-zero, a browser returned no tab id). May or may not have happened. |
| `failed` | A definite error before dispatch (validation, browser lookup, app not running from the resolved path, a pre-read), or `verify` returned `failed`. Carries `error` code and facts. |
| `unsupported` | No action fits the clause. |
| `needs_clarification` | `resolve` returned choices. Nothing ran for this step. Carries `choices`. |
| `declined` | Ask-first action cancelled or not confirmed within 60 s. |
| `skipped` | An earlier step did not complete. |

Every step carries `index`, `clause`, `action`, `state`, `target`, `facts`, `detail`. While a request runs, steps may show `not_started`, `running` or `awaiting_confirmation`; none of these is terminal. The worker stops at the first step that is not `completed`, including `unverified`; the rest are `skipped`.

## Request outcome

`queued`, `running` (in progress, not success), then one terminal state: `completed`, `answered` (no action: a scripted reply or a spoken answer, carried in `reply` or `say`), `partial`, `failed`, `unverified`, `unknown`, `unsupported`, `needs_clarification`, `declined`, `cancelled`, `expired` (queued over 30 s), `busy` (queue or ledger full, never queued).

- `partial`: at least one step `completed`, then a stop. `stopped_state` carries the exact state of the stopping step.
- Single-step or first-step stops use that step's state as the request state.
- `cancelled`: cancelled while queued, while awaiting confirmation, or at any point before a step is dispatched. Completed steps stay listed as completed.
- Dispatch boundary: under the engine lock the worker checks the cancel flag and marks the step `running` in one critical section, immediately before `run`. A cancel that lands before that section stops the step (state `skipped`, nothing dispatched), including while `resolve` is blocked; a cancel after it lets the dispatched step settle on its deadline and stops before the next step.

Every response lists all steps in order and the per-launch `instance` id. On a stop:

- `uncertain_step`: index and state of a dispatched step that ended `unknown` or `unverified`. Never resubmittable.
- `not_started`: clause texts that were never touched. Only these may be resubmitted, and only by a caller who knows they do not depend on the uncertain or failed step. Never resubmit the whole command.

## Planning

1. Split the text into ordered clauses on sequencing words (`then`, `and then`, `after that`, `, and`) outside quotes and URLs. Order and duplicates are preserved. Max 5 clauses.
2. Classify each clause with the fixed Jev questions. Jev picks the action type only.
3. Extract arguments from the clause text in Python: app name span, URL, level, duration.
4. If Jev says the text is compound but the split finds one clause, return `needs_clarification` asking for "X, then Y". Never reorder, drop or guess.

## Confirmation

Each action has an `effect` category (`actions.py: EFFECTS`): `open`, `navigate`, `media`, `volume`, `display`, `timer`, `scroll`, `click`, `type`, `submit`, `task`, `in_task`, `risky`, `quit`, `lock`, `sleep`. (`look`, reading the screen, has no effect and is not a setting.) A user setting maps each category to **Ask first** or **Automatic**. Defaults: Ask first for `quit`, `lock`, `sleep`, `click`, `type`, `submit`, `task`, `risky`; Automatic for the rest (`open`, `navigate`, `media`, `volume`, `display`, `timer`, `scroll`, `in_task`).

- The engine enforces the policy for every source. Source (`voice`, `cli`) is set by the entry point, never read from a request, and is display metadata only.
- An Ask-first step blocks the worker and opens a menu-bar popover naming the concrete action and target, with Confirm and Cancel. The pending decision is one record bound to that request id and step.
- Confirm, popover Cancel, request `cancel` and the 60 s timeout all consume that record with one compare-and-set under the engine lock. The first wins; any later Confirm is a no-op and closes the popover. Request `cancel` gives request state `cancelled`; popover Cancel or timeout gives `declined`.
- Duplicate targets: when `resolve` returns choices, the engine may ask Jev to pick (`tiebreak`, 2 to 6 choices, hints: folder, running now, last opened). The pick is used only when Jev's score reaches the user threshold (Settings, 50 to 100, default 85, 100 = always ask). The threshold is a model score, not a correctness claim. Below it, or on any error, the step stays `needs_clarification` with the choices and the score in `facts.tiebreak`. A picked quit target always asks, whatever the quit policy; after Confirm the picked target must still be among the re-resolved choices.
- After a Confirm wins, the engine re-resolves the target before dispatch; if any field differs or it vanished the step fails with `target_changed`.
- The popover text names the exact effect: volume level and percent, timer duration or reminder text, the pinned timer(s) to cancel, and for apps the install's folder. Timer cancel pins timer ids at resolve; run cancels only those. Quit terminates only processes whose bundle path equals the resolved path, never by bundle id.
- No request field can confirm, skip policy or supply a target handle.
- Outstanding effects: if an earlier step's native effect (a screen press) outlived its deadline and may still land, every later dispatch, of any action family and including queued requests, waits up to 10 s (`engine.py: PENDING_WAIT`) for it to settle, then fails with `an earlier action hasn't finished` without dispatching. The earlier step stays `unknown`.
- `click` (on-screen controls) defaults to Ask first. A control whose label matches the risky list (buy, send, delete, pay, submit, post, share, install, sign out and similar) asks by default, whatever the click setting — but setting `risky` to Automatic overrides this (`engine.py`). That list only adds confirmation; it never proves any other click harmless. A `screen.press`, `screen.pick` or `screen.submit` that would otherwise dispatch without asking, while `risky` is Ask first, first gets Jev's yes/no on whether it has a material effect (send, delete, buy, share, merge, approve and similar); at p ≥ 0.5, or on any failed or malformed check, it asks (`engine.py: _consequential`, provisional uncalibrated gate). `look` (reading the screen) has no effect and never asks.

## Ledger and replay

- Request ids are reserved under one lock before enqueue. Same id and same text returns the existing status. Same id, different text: `id_conflict`.
- Queued and running entries are never evicted. Full results are kept 10 minutes; after that the id keeps a small record (text hash, terminal state) for the rest of the app run, so a reused id never runs again in this run.
- Id records are capped at 10,000 per app run. When full, new ids get `busy` with detail `ledger_full`; old records are never dropped to make room. Restart clears it.
- After app restart the `instance` changes and old ids are unknown: `unknown_outcome`, which never means safe to retry.
- `jevctl` never resubmits. On wait timeout it prints the last known status and exits.

## Verification per adapter

- `app.open`: `open -a <path>` (bundle id when no path), then poll `lsappinfo` for a running process with that bundle id at that path. No Apple Events needed.
- `url.open`: the browser is the system handler for the URL (NSWorkspace, bounded helper); lookup failure stops before any effect. Chrome: open a new tab and keep its unique tab id; `done` only when that tab shows the exact URL, `unverified` if the tab closed or shows anything else. Safari exposes no tab id, so it opens a new tab and reports `unverified`. Other browsers: `open -b <browser> <url>`, `unverified`. Comparison rule below.
- URL comparison: `done` only when the observed URL equals the requested URL after two normalizations: host lowercased, and an empty path treated as `/`. Exactly two redirects are tolerated. (1) `www.` equivalence, only for hosts in `url_adapter.WWW_CANONICAL` (youtube.com, google.com), in either direction; every other host, including `example.test` vs `www.example.test`, must match exactly. (2) A requested `http` URL observed as `https` is an accepted upgrade; `https` observed as `http` never is. Port, path, raw query (order and repeats kept) and fragment must otherwise match. Any other difference ends `unverified`, with both URLs in facts. While the new tab still shows a known initial state (`chrome://newtab/`, `about:blank`, or empty), verify returns `wait`; any other unsupported scheme or malformed observed URL is `unverified`. A user who says a bare domain gets `https://` added before the request, and a dotless name from the curated `site_for_name` map ("go to YouTube") resolves to its fixed URL; unknown names are never guessed. Page content and load state are not checked.
- Volume, Spotify volume, dark mode, media: read the value back after setting it.
- `app.quit`: poll until the bundle id is no longer running.
- Lock, sleep: `unverified`.
- `screen.list`: one bounded observation of the frontmost app's focused window. The Accessibility walk (node and time caps, ported from typesafe-computer-use under MIT) lists the controls the app declares, and the menu bar items. Apple Vision OCR on a capture of that window only adds the text lines no control covers. Each item carries its source: `ax`, `ocr` (text, not proof of a control) or `ax+ocr`. Items are numbered in reading order, capped at 60, and remembered as the last list shown. The UI badges them for 12 s. Facts carry the items. The diagnostics log gets only their count, never screen text.
- `screen.press`: resolve takes a fresh AX observation within one 3 s budget. Identity is exact: the process (pid plus start time), the window element and the control element (AX elements compare equal only when they are the same element; each gets a local token). A number must name, in the last list shown, the same element, still present, enabled and pressable, with the same role, label and frame, in the same window of the same process; anything else is `screen_changed`, never a replacement at the same spot. OCR-only items are `not_a_control`. A name matches exactly, then as whole words. Failing that, Jev chooses among the window's controls, each described as a short card: its name, up to three words right around it (from AX labels and vouched OCR only, never value-derived labels or fields) and its place. Opaque ids are used (`c0`, `c1`…, plus `none`); only those names and the spoken words are sent, never an AXValue-derived label, a field value or document text. Its answer must be exactly (in-range non-bool int or none, finite 0–1), otherwise the step asks again with zero presses, and it must clear 0.65. Identical labels ask which. After a confirmation the target is re-resolved and must be equal. Run re-finds the same element and re-checks all of the above, then sends `AXPress` (no mouse movement). Exception: a control inside an `AXWebArea` in a Chromium app (a renderer helper in the bundle) gets `AXFocused` and then Return (Space for a checkbox, radio button, switch or toggle) posted to that pid only, because Chromium ignores `AXPress` on page content (measured on Chrome 153, 2026-09-25). The key goes only after the app's `AXFocusedUIElement` equals the target and only if the step deadline has not passed immediately before the key pair; otherwise the step `failed` with no key sent. A key pressed is always released. Every native read, OCR and the press run under the step deadline; a call that outlives it is abandoned. A press the app never answers is `unknown`; an AX error meaning gone or refused is `failed`; any other error after sending is `unknown`. Verify completes only on a change of the pressed control itself (value, selection, expansion, its title or description, as Pause becoming Play, or it going away) or a menu opening from a menu control. Any other change in the window is recorded as `observed` and the press ends `unverified` (delivered), which stops a multi-step request. Logs carry label lengths and item counts, never screen text, and speech never says a control's text.
- `screen.type`: "type <text> [into the <name> field|box]", with quoted text taken exactly and unquoted text losing only a trailing full stop. The field is the named editable control (AXTextField, AXTextArea, AXSearchField or AXComboBox, named by its title, description or placeholder, never its value), or the app's focused element. It must be enabled, accept `AXSelectedText`, sit in the observed window, and not be a password field. A field whose kind can't be read counts as a password field. Run re-checks process, element, role, frame and window, focuses the field, then reads its value and its selection (`AXSelectedTextRange`, UTF-16 units), and inserts through `AXSelectedText`: no keystrokes, so nothing can land in another window. Nothing is submitted; pressing Return is a separate action. Verify: done only when the new value equals exactly the old value with that selected range replaced by the typed text. A range that can't be read, or that would split a character, gives no exact expectation, so the result is unverified. Anything else after 1.5 s is unverified. After a quoted text, anything but a complete field phrase clarifies, and an unfinished "into"/"into the" is never dropped. The field value is compared in memory only, never logged, stored or sent, and typed text is logged as a length. Category `type`, Ask first by default.
- `screen.submit`: only the exact phrases "press enter/return [key]", "hit enter/return" and "submit [it/that/this]". Sends `AXConfirm` (what Return does) to the app's focused element, which must accept it and sit in the observed window; there are no keystrokes. Run re-checks the exact element first. Done only when that field goes away or its text changes (a field that clears after sending), and done means only that: the field changed after Return, never that a message, purchase or form was accepted. Focus leaving the field is recorded as observed but proves nothing (a click elsewhere does it too), so on its own the step is unverified. Speech says "Pressed return", never "sent" or "submitted". A focus call that times out after being sent is unknown, and nothing is typed after it. Category `submit`, Ask first by default.
- Direct commands skip classification, because their words leave nothing for Jev to choose: "scroll up/down [a little | a lot | to the top/bottom] [in <app>]" and "[double/right] click [here | the mouse]".
- `screen.scroll`: the frontmost window's main scrollable view (a named app must be the one in front). After all preparation, immediately before the write or action (even on Automatic), the same process must be in front with the same window, and the view must still belong to that window (its AXWindow). Native views: the largest scroll area's vertical scroll bar. Its value must read as a finite number in 0–1 and be settable, otherwise nothing is written. At the edge, "already at the top/bottom" is returned before any write. It moves by a fraction (little 0.08, normal 0.25, a lot 0.6, or to the end). Web areas, which expose no bars: `AXScrollToVisible` (checked available) on the text or link nearest to about three quarters of a screen beyond the edge. No wheel events and no pointer movement. Done only when a native bar's value (valid 0–1 readback) changed. Web areas expose no scroll position, so a web scroll is always reported as sent and unverified; layout moving isn't taken as proof. The last element of a capped tree isn't claimed to be the page's end. Category `scroll`, Automatic by default.
- `pointer.click`: a real click (left, right or double) where the pointer already is; nothing moves it. The target is the topmost window under the pointer. If that's Hey Jev's own, an overlay, a menu or anything but an ordinary window, the click is refused, with the process identity (pid + start) carried. At dispatch, a moved pointer, a different window or a restarted app fails with nothing clicked. Immediately before each press the pointer and window are re-checked. A press that went down is always released. A change partway through a double click stops it and reports unknown. A click at an arbitrary spot has no checkable postcondition, so it always ends unverified (delivered), with changes in that window recorded as observed. "click it/that/there" aren't pointer clicks. Category `click`.
- `screen.pick`: "click/play/open the <ordinal> <noun> [in the <place>] [in <app>]" and "the <noun> in the top/middle/bottom[-left/right]". Nouns that are roles (button, link, tab, row, item) filter by AX role. Others (video, result, song, post…) are Jev's call: one batched yes/no question per pressable control name (names only, never field values or OCR), each answer exactly (bool, finite 0–1), counted only at ≥ 0.65. A malformed answer asks again with nothing pressed. The code counts in reading order — relative rows built from the items themselves (`actions.py: reading_order`: an item joins the current row when its centre lies within the row's vertical span), top to bottom, then left to right — or finds the control nearest the named place. (This is distinct from snapshot numbering: `screen.py` numbers the shown list in 12 pt horizontal bands.) Content picks (Jev-judged nouns, link, row) on a page with an `AXWebArea` count only controls whose centre lies inside it, so browser tabs named after videos never count; button, tab and item picks count the whole window. A qualified pick ("the NetworkChuck video") may also match the heard words on a card: they must run whole-word through the card's text, spacing and case aside ("network Chuck" matches NetworkChuck, "apple" never matches Pineapple), and a card Jev is sure is not one never matches. When any card matches, cards without the words that Jev was unsure about drop out; the words prove the name only, so a matching card Jev was not already sure of is judged again, with the bare noun, in one batched question over the whole pool, and that answer counts under the same gate. An unsure candidate, leaning yes or no, still asks rather than being counted past: one sure item is unique only when no rival is unsure. The pick then becomes an ordinary exact-element press target (identity, confirmation, risky gate, readback). Category `click`.

## Bridge

Socket `~/Library/Application Support/Hey Jev/run/jev.sock`; directory mode 0700, socket 0600. Peer uid via `getpeereid` must equal ours, else close. `flock` on a lock file before bind; a stale socket is removed only while holding the lock after a failed connect. One JSON line in, one out; 16 KiB request cap, 5 s to send. Ops: `command {id, text}`, `status {id}`, `cancel {id}` — protocol version `v: 1`, ids 1–128 chars, unknown fields rejected. `op` must be a string; `wait` a finite non-negative number (not boolean); anything else is `rejected` with a `bad_*` detail, never queued. `wait` is clamped to 120 s (`bridge.py: MAX_WAIT`); 0 means reply with the current status. At most 16 concurrent clients; overflow gets a `busy`/`too_many_clients` reply on a 1 s send bound, and a failed send never stops the accept loop. Cancel follows the dispatch boundary above; no rollback. The overflow `busy` reply has a 1 s send bound and a failed send never stops the accept loop. Startup: if the app cannot own the lock and socket, it stops before creating a microphone or voice dispatcher.

## Speech

Voice stop: ordinary turns wait on one FIFO turn worker that holds the microphone floor only while speaking, so the listener hears speech while work runs. "Stop", "stop that", "cancel", "never mind" and similar exact phrases, with work in flight, are handled at once on the listener. Under one lock with submission, they drop every heard-but-unsubmitted turn (a stop generation counter) and cancel every running and queued engine request. With nothing in flight, "stop" is an ordinary command. Each queued turn is re-checked for stop generation, listening and epoch just before it submits. Limits: stop can prevent only what hasn't been dispatched, and an effect already sent is reported as sent. Capture is off while Hey Jev is speaking, so speech can't be interrupted by voice yet. `siri.py` speaks from the final result, naming the failed step when there is one. Exception: mute, lock and sleep speak a short "about to" line before running, because speech cannot follow them. That line never claims completion. `unverified` is spoken as "sent, couldn't check", never "done". A voice request refused as `busy` is spoken at once; a result that outlives the voice wait is spoken when it lands.

## Screen control

Built in release 0.3.0: `screen.list`, `screen.press`, `screen.type`, `screen.submit`, `screen.scroll`, `pointer.click`, `screen.pick`, and implied presses, all specified above. A clause that starts with "click" or "tap", or that names a UI part (button, checkbox, link, menu item, icon, toggle), always plans as `screen.press`, because Jev's target question hears "the Loud mode checkbox" as volume.

Implied presses: a confident single-clause command of two or more words (`mac_command` at the gate) that no built-in action matches becomes `screen.press` with `{"intent": clause}`. No label matching: Jev is asked which one control, pressed once, does exactly what was said, or none, and must reach 0.75 (named controls need 0.65). The press then runs like any other: click policy, risky confirmation, identity re-check and readback. Otherwise it fails with "nothing on screen does that".

Cards: the chooser and `screen.pick` send Jev one card per control, its name, up to three shareable nearby words and its place. Nearby means in the control's column, within 20 pt above or 70 pt below, stopping at the next control of the same role and similar width (the next item, however its title wraps); when another such item sits just above, nothing above is taken. Missing context is allowed (Jev may then be unsure, and unsure asks); borrowed context from a neighbouring item is not.

Answers to "which one?": `screen.press` / `screen.pick` choices carry each option's exact target. The engine keeps the last such question for 45 s. The next request is an answer only if it was heard (voice: speech start; otherwise: queued) after the question was asked, isn't a refusal ("stop", "cancel", "never mind", "none of them"), and `planner.answer_pick` maps it to exactly one option (ordinal, "last", a number, or words found in exactly one name). It then runs the same action with `{"pinned": target}`, which resolves only if that element is still in the same window of the same process with the same role, label and frame, then goes through click policy, confirmation and readback as usual. An answer is used once; any other request, and any cancel, withdraws the question.

## Live inspection

Menu → "Show what Jev sees". While it's on, a background observer reads the frontmost window about once a second (bounded to 1.5 s, and never while a command is running). It draws click-through boxes with numbers and names: blue can be pressed, orange is a text field, teal is other Accessibility, grey is screen text that would be shared with Jev, faint is screen text that stays local. A readout shows the app, item count, read time and age. An item keeps its number while that exact element stays in view; new items take the next number; numbering restarts when the app or window changes. Each refresh becomes the list "click N" refers to, and a number still only resolves to the same live element (a scroll or replacement never retargets it). The overlay window is excluded from screen capture, so it can't be read back as screen text. A refresh that lands after the toggle goes off is dropped. Nothing is sent or logged.

## Multi-step tasks

Built in release 0.3.0 (see "Multi-step tasks (stage 3)" — the stage name is historical; the behavior below is what shipped).

"take over: …" / "work on: …" run as one request. The task asks once (category `task`). With `in_task` Automatic, that OK covers its clicks, typing and Return; with Ask first, each step asks. `risky` labels ask by default unless `risky` is Automatic. One deadline (120 s) caps the task confirmation, every Jev call (at most 10 s, abandoned when late), observations, confirmations, pending-effect waits and each step's execution. Stop and the deadline are re-checked after every wait, and they win over a late answer. Jev is offered only the kinds that can run on the exact snapshot it's shown, and its answer must match that snapshot's opaque ids. Just before each dispatch, the same app must be in front with the same window as when Jev decided. A submit must hit the same focused field. The task never follows a changed window: if the pinned app or window changes, the task ends `unverified` ("the app/window changed"). The separate path is an explicit `open_app` step for an installed app the goal names (ordinary `app.open`: same resolve, policy, verification). After a verified open, the task waits up to 3 s for exactly the opened bundle to come forward and pins it; any other app ends the task. If the first read fails (Hey Jev's own window in front, say) and the goal names an app, the task starts with no snapshot and only `open_app`, `done` and `stuck` are offered; with no app named, it fails with the reason. Any step that isn't completed ends the task, and nothing is ever retried. `done` is verified only against an explicit "until you see "X"" that appeared during the task. Anything else ends unverified. Jev gets AX item labels. An OCR line is included only when a status-preserving scan of the whole window completed (unreadable roles, subroles or child lists, fields without frames, or caps make it incomplete), the line overlaps no editable or secure field, and the whole line lies inside one region Accessibility declares as text or a labelled control (static text, heading, link, button, menu item, checkbox, radio, pop-up, tab; never a row, cell, image or group). Text anywhere else, including apps that expose nothing, stays on the Mac. Stop reasons and speech never carry screen text.

## Release 0.3.0

Shipped 2026-09-25. Engine, planner, bridge, `jevctl`, installed-app
catalog with duplicate-app tiebreaks, `url.open` with per-browser
verification, full screen-control family, multi-step tasks, confirmation
popover with per-category policy, diagnostics log, and the offline engine
/ ledger checks including a false-success case. Status of this release:
the integrated features passed their automated checks, but the final
live trial in the running app is still pending.
