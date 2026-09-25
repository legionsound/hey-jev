# Development

For Johnny's fork (`legionsound/hey-jev`) only. Nothing here goes
upstream to `henryklunaris/hey-jev`.

## Setup

```bash
git clone https://github.com/legionsound/hey-jev.git
cd hey-jev
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt setuptools py2app
```

Developed on Python 3.14. macOS-only: the code drives Accessibility,
AppleScript, and the Keychain, and the test suite needs PyObjC
(`pyobjc-framework-Cocoa/Speech/AVFoundation` in `requirements.txt`).

## Running

```bash
.venv/bin/python setup.py py2app -A
open "dist/Hey Jev.app"
```

Alias mode: the bundle runs the code straight from this folder, so
pulling new code + restarting the app is enough. Rebuild only if the
folder moves.

`jevctl` talks to the running app over the bridge socket:

```bash
./jevctl command --text "open Safari, then go to google.com"
./jevctl command --text-file request.txt --id my-id-1 --wait 60
./jevctl status my-id-1
./jevctl cancel my-id-1
./jevctl trials --last 5
```

## Tests

```bash
.venv/bin/python -m unittest discover -q
```

Stdlib `unittest`, several hundred tests covering engine, planner,
actions, screen control, settings, and speech. Use the checkout's own
`.venv` (created by the setup instructions above) — stock system
python3 ships without AppKit/Foundation, so
every macOS-dependent test module fails to import there (unless your
system Python has PyObjC installed globally). That is
environmental, not a regression.

## Contribute

- Target this fork only: branch from `main`, open PRs against
  `legionsound/hey-jev`. (`release` was the temporary publication
  branch for 0.3.0.) Nothing is submitted upstream.
- Docs live in `docs/`: `FEATURES`, `CONFIGURATION`, `TROUBLESHOOTING`,
  `ARCHITECTURE` (current, grounded in code) and `ENGINE_CONTRACT` (the
  formal spec). `COMMAND_ARCHITECTURE`, `HEY_JEV_OVERHAUL_SPEC`,
  `CAPABILITY_EFFORT_MAP`, `BUZZ_HANDOFF` are archived historical notes —
  read them for context, do not extend them.
- Changing behavior? Update `ENGINE_CONTRACT.md` first (it wins over
  other docs), then the guide that describes the feature, then
  `CHANGELOG.md`.
- Every action needs a `verify` readback unless none exists (then say so
  in `proves`); every user-visible state must appear in the contract's
  outcome tables; never log keys, field values, or screen text.
