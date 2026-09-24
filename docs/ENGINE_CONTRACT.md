# Engine contract v1

Status: design for review, 2026-09-24. Supersedes the v0 draft posted in Buzz. Implements the foundation in `COMMAND_ARCHITECTURE.md`; where this file is more specific, this file wins. Nothing here is built yet.

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
    "verify": verify_app_open, # (target) -> ("done" | "wait" | "failed", facts). Polled until deadline.
    "proves": "a running app with the resolved bundle id and path",
    "timeout": 10,
}
```

`verify` may be `None` only where no readback exists (lock, sleep). Every native call is a subprocess run with `timeout=deadline - now`, so the worker kills it at the deadline and never leaves a detached call that can still act. No background threads issue effects.

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

Every step carries `action`, `state`, `target`, `facts`, `detail`. The worker stops at the first step that is not `completed` or `unverified`; the rest are `skipped`.

## Request outcome

`queued`, `running` (in progress, not success), then one terminal state: `completed`, `partial` (some steps done, then a stop), `failed`, `needs_clarification`, `declined`, `unknown`, `cancelled` (removed while queued), `expired` (queued over 30 s), `busy` (queue full, never queued).

Every response lists all steps in order, the per-launch `instance` id, and for a stop after earlier steps, `remaining`: the clause texts not yet run. Callers resubmit only `remaining`, never the whole command.

## Planning

1. Split the text into ordered clauses on sequencing words (`then`, `and then`, `after that`, `, and`) outside quotes and URLs. Order and duplicates are preserved. Max 5 clauses.
2. Classify each clause with the fixed Jev questions. Jev picks the action type only.
3. Extract arguments from the clause text in Python: app name span, URL, level, duration.
4. If Jev says the text is compound but the split finds one clause, return `needs_clarification` asking for "X, then Y". Never reorder, drop or guess.

## Confirmation

Each action has an `effect` category: `open`, `navigate`, `media`, `volume`, `display`, `timer`, `quit`, `lock`, `sleep`. A user setting maps each category to **Ask first** or **Automatic**. Defaults: Ask first for `quit`, `lock`, `sleep`; Automatic for the rest.

- The engine enforces the policy for every source. Source (`voice`, `cli`) is set by the entry point, never read from a request, and is display metadata only.
- An Ask-first step blocks the worker and opens a menu-bar popover naming the concrete action and target, with Confirm and Cancel. The pending decision is bound to that request id and step.
- On Confirm the engine re-resolves the target; if it changed or vanished the step fails with `target_changed`. Cancel or 60 s timeout gives `declined`.
- No request field can confirm, skip policy or supply a target handle.

## Ledger and replay

- Request ids are reserved under one lock before enqueue. Same id and same text returns the existing status. Same id, different text: `id_conflict`.
- Queued and running entries are never evicted. Full results are kept 10 minutes; after that the id keeps a small record (text hash, terminal state) for the rest of the app run, so a reused id never runs again in this run.
- After app restart the `instance` changes and old ids are unknown: `unknown_outcome`, which never means safe to retry.
- `jevctl` never resubmits. On wait timeout it prints the last known status and exits.

## Verification per adapter

- `app.open`: `open -b <bundle id>` (path when duplicates exist), then poll running apps for that bundle id and path.
- `url.open` (Safari, Chrome): open a new tab via AppleScript and remember that tab. Poll that tab's URL. `done` when scheme-insensitive host (ignoring `www.`) matches and the path starts with the requested path. A redirect to another host ends as `unverified` with the observed URL. Page content and load state are not checked.
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
