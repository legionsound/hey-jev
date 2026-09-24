# Engine contract v1.1

Status: design for review, 2026-09-24. v1.1 amends v1 after review. Supersedes the v0 draft posted in Buzz. Implements the foundation in `COMMAND_ARCHITECTURE.md`; where this file is more specific, this file wins. Nothing here is built yet.

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
    "verify": verify_app_open, # (target, deadline) -> ("done" | "wait" | "failed", facts). Polled until deadline.
    "proves": "a running app with the resolved bundle id and path",
    "timeout": 10,
}
```

`verify` may be `None` only where no readback exists (lock, sleep). Every native call in `run` and `verify` is a subprocess with `timeout=deadline - now`. At the deadline the worker kills and reaps its own subprocess and reports `unknown`. Killing `osascript` or `open` does not recall an Apple Event already delivered to another app, so the effect may still happen later; the engine never claims external work stopped.

## Step outcome

| State | Meaning |
| --- | --- |
| `completed` | `verify` returned `done`. The only success. |
| `unverified` | `run` returned; no `verify` exists for this action. Not success. |
| `unknown` | Deadline hit or process died after dispatch. May or may not have happened. |
| `failed` | `run` raised a definite error, or `verify` returned `failed`. Carries `error` code and facts. |
| `unsupported` | No action fits the clause. |
| `needs_clarification` | `resolve` returned choices. Nothing ran for this step. Carries `choices`. |
| `declined` | Ask-first action cancelled or not confirmed within 60 s. |
| `skipped` | An earlier step did not complete. |

Every step carries `action`, `state`, `target`, `facts`, `detail`. The worker stops at the first step that is not `completed`, including `unverified`; the rest are `skipped`.

## Request outcome

`queued`, `running` (in progress, not success), then one terminal state: `completed`, `partial`, `failed`, `unverified`, `unknown`, `unsupported`, `needs_clarification`, `declined`, `cancelled`, `expired` (queued over 30 s), `busy` (queue or ledger full, never queued).

- `partial`: at least one step `completed`, then a stop. `stopped_state` carries the exact state of the stopping step.
- Single-step or first-step stops use that step's state as the request state.
- `cancelled`: cancelled while queued, while awaiting confirmation, or between steps. Completed steps stay listed as completed.

Every response lists all steps in order and the per-launch `instance` id. On a stop:

- `uncertain_step`: index and state of a dispatched step that ended `unknown` or `unverified`. Never resubmittable.
- `not_started`: clause texts that were never touched. Only these may be resubmitted, and only by a caller who knows they do not depend on the uncertain or failed step. Never resubmit the whole command.

## Planning

1. Split the text into ordered clauses on sequencing words (`then`, `and then`, `after that`, `, and`) outside quotes and URLs. Order and duplicates are preserved. Max 5 clauses.
2. Classify each clause with the fixed Jev questions. Jev picks the action type only.
3. Extract arguments from the clause text in Python: app name span, URL, level, duration.
4. If Jev says the text is compound but the split finds one clause, return `needs_clarification` asking for "X, then Y". Never reorder, drop or guess.

## Confirmation

Each action has an `effect` category: `open`, `navigate`, `media`, `volume`, `display`, `timer`, `quit`, `lock`, `sleep`. A user setting maps each category to **Ask first** or **Automatic**. Defaults: Ask first for `quit`, `lock`, `sleep`; Automatic for the rest.

- The engine enforces the policy for every source. Source (`voice`, `cli`) is set by the entry point, never read from a request, and is display metadata only.
- An Ask-first step blocks the worker and opens a menu-bar popover naming the concrete action and target, with Confirm and Cancel. The pending decision is one record bound to that request id and step.
- Confirm, popover Cancel, request `cancel` and the 60 s timeout all consume that record with one compare-and-set under the engine lock. The first wins; any later Confirm is a no-op and closes the popover. Request `cancel` gives request state `cancelled`; popover Cancel or timeout gives `declined`.
- After a Confirm wins, the engine re-resolves the target before dispatch; if it changed or vanished the step fails with `target_changed`.
- No request field can confirm, skip policy or supply a target handle.

## Ledger and replay

- Request ids are reserved under one lock before enqueue. Same id and same text returns the existing status. Same id, different text: `id_conflict`.
- Queued and running entries are never evicted. Full results are kept 10 minutes; after that the id keeps a small record (text hash, terminal state) for the rest of the app run, so a reused id never runs again in this run.
- Id records are capped at 10,000 per app run. When full, new ids get `busy` with detail `ledger_full`; old records are never dropped to make room. Restart clears it.
- After app restart the `instance` changes and old ids are unknown: `unknown_outcome`, which never means safe to retry.
- `jevctl` never resubmits. On wait timeout it prints the last known status and exits.

## Verification per adapter

- `app.open`: `open -b <bundle id>` (path when duplicates exist), then poll running apps for that bundle id and path.
- `url.open` (Safari, Chrome), after milestone 1: open a new tab via AppleScript and remember that tab. Poll that tab's URL. `done` only when the observed URL equals the requested URL after exactly two normalizations: host lowercased, and an empty path treated as `/`. Scheme, host (including `www.`), path, query (order and repeats kept) and fragment must otherwise match. A user who says a bare domain gets `https://` added before the request is made, so the requested URL is always explicit. Any difference, including redirects, ends `unverified` with the observed URL. Page content and load state are not checked.
- `url.open` (other browsers): `open -b <browser> <url>`, `unverified` with `{"opened_with": bundle_id}`.
- Volume, Spotify volume, dark mode, media: read the value back after setting it.
- `app.quit`: poll until the bundle id is no longer running.
- Lock, sleep: `unverified`.

## Bridge

Socket `~/Library/Application Support/Hey Jev/run/jev.sock`; directory mode 0700, socket 0600. Peer uid via `getpeereid` must equal ours, else close. `flock` on a lock file before bind; a stale socket is removed only while holding the lock after a failed connect. One JSON line in, one out; 16 KiB request cap, 5 s to send. Ops: `command {id, text}`, `status {id}`, `cancel {id}`. Cancel removes queued work or stops before the next step; no rollback.

## Speech

`siri.py` speaks from the final result, naming the failed step when there is one. Exception: mute, lock and sleep speak a short "about to" line before running, because speech cannot follow them. That line never claims completion.

## Milestone 1

Engine, planner, bridge, `jevctl`, and `app.open` resolved through the current eight-app table mapped to bundle ids. All other current actions move to the registry with their readbacks. The confirmation popover and the per-category setting ship in milestone 1 too, so voice lock, sleep and quit keep working under the default policy. Tests: offline engine and ledger checks, including a false-success case, plus existing provider tests. Johnny runs the live trial.
