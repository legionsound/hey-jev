# Hey Jev

Voice control for your Mac. Say "Hey Jev" (or hold right Option), say what you want, and it does it, checks that it worked, and tells you what happened.

```text
"Hey Jev, open Safari, then go to github.com"
"Hey Jev, set the volume to 40 percent"
"Hey Jev, click the third video"
"Hey Jev, take over: turn on dark mode in System Settings"
```

This is a fork of [henryklunaris/hey-jev](https://github.com/henryklunaris/hey-jev) with a rebuilt command engine, screen control, a command-line bridge into the running app, Apple on-device dictation, and a redesigned Settings window. See [CHANGELOG.md](CHANGELOG.md) for everything that changed.

**Mac only.** macOS Sequoia or later. It drives the Mac through Accessibility, AppleScript and the Keychain.

## Why it's different

Most voice assistants guess, act, and announce success. Hey Jev is built the other way round:

- **It checks before it says "done".** Every action has a readback: the app is actually running, the volume is actually 40, the browser tab actually shows that address. If it can't check, it says so ("I sent that, but I couldn't check whether it worked").
- **The model decides, Python acts.** Jev, a small classifier model, only answers narrow questions ("is this a command, and which kind?"). It never writes code or picks targets. The target (which app, which URL, which button) is resolved deterministically against what is actually on your Mac or your screen.
- **Nothing runs twice.** A multi-step request stops at the first step that did not verifiably work and tells you what already happened. Side effects are never retried.
- **You choose what asks first.** Every kind of action (open apps, quit apps, click buttons, type text, start a task…) is either *Ask first* or *Automatic*. Asks appear as a small pop-down from the menu bar.

## What it can do

| Area | Examples |
| --- | --- |
| Apps | "open Slack", "quit Chrome", "open Mac whisper" (finds MacWhisper). Two matching copies? It asks which, or picks by what's running and what you opened last. |
| Volume | "turn it up", "set volume to 40 percent", "turn it down by 10 percent", "mute", "turn Spotify down" |
| Music | "play", "pause", "next track", "previous track" (Spotify) |
| System | "dark mode on", "lock the screen", "put the Mac to sleep" |
| Timers | "set a timer for 5 minutes", "remind me in 20 minutes to call Sam", "how long is left?", "cancel the timer" |
| Web | "go to github.com", "go to YouTube", "search Google for rock and roll", "open github.com in Safari" |
| Screen | "what can I click?", "click Share", "click 4", "type hello into the search field", "press return", "scroll down", "click the video in the bottom-right" |
| Tasks | "take over: …" or "work on: …" hands Jev a goal it works through step by step in the current app |
| Questions | "who wrote Hamlet?", "what time is it?" (optional, via an LLM you choose) |
| Chaining | "pause Spotify, then open Slack". Up to five steps, joined with "then", "after that" or ", and". A bare "and" never splits, so "play rock and roll" stays one request. |

Say **"stop"** at any time to cancel what's running and anything queued.

The full guide, with every phrase and what each one checks, is in [docs/FEATURES.md](docs/FEATURES.md).

## Quick start

You need:

- A Mac with Python 3 (developed on 3.14; `python3 --version` to check, or install from [python.org](https://www.python.org/downloads/) or with `brew install python`)
- A [Fish Audio](https://fish.audio/) API key for the voice
- For Jev's decisions, either an [OpenRouter](https://openrouter.ai/) key or a direct [TypeSafe](https://typesafe.ai/) key
- Optional: an OpenRouter key for spoken answers to questions
- Optional: the Spotify desktop app, for music commands

```bash
git clone https://github.com/legionsound/hey-jev.git
cd hey-jev
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt setuptools py2app
.venv/bin/python setup.py py2app -A
open "dist/Hey Jev.app"
```

`py2app -A` builds the app in alias mode: the bundle runs the code straight from this folder, so pulling new code and restarting the app is enough. Rebuild only if you move the folder.

On first launch:

1. **Settings opens** and asks for your keys. They are stored in your Mac's Keychain.
2. **Whisper downloads** its speech model once (small.en, about 480 MB). Or choose Apple's on-device dictation in Settings > Transcription and skip the download.
3. **Allow the Microphone** when macOS asks.
4. **Allow Accessibility**: System Settings > Privacy & Security > Accessibility, turn on Hey Jev. Needed for the right-Option key and for all screen control.
5. **Allow Automation** the first time it controls Spotify, Safari, Chrome or System Events.
6. Optional: **allow Screen Recording** so it can read on-screen text that Accessibility doesn't expose.

Then choose how you talk to it with the switch in the window: **Hold Option** (hold right Option, talk, let go) or **Hey Jev** (always listening for the wake phrase, which you can change in Settings).

Details on every permission and setting: [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

## Command line

`jevctl` sends text into the running app. It goes through the same planner, queue and confirmation rules as your voice, and prints a JSON result for each step.

```bash
./jevctl command --text "open Safari, then go to google.com"
./jevctl command --text-file request.txt --id my-id-1 --wait 60
./jevctl status my-id-1
./jevctl cancel my-id-1
./jevctl trials --last 5        # what recent requests heard, planned, did and said
```

Only `completed` means the app saw the result. `unverified` means it acted but could not check; `unknown` means it may or may not have happened. `jevctl` never retries, and re-sending the same id returns the stored result instead of running it again. The socket only accepts your own user account.

## How it works

```text
mic ─► Whisper / Apple dictation ─► wake phrase ─► planner ─► engine ─► actions ─► readback ─► reply (Fish voice)
                                                       ▲
                                    jevctl ─► socket ──┘
```

1. **Hear.** Audio is transcribed on your Mac by faster-whisper or Apple's on-device recognizer. In wake mode, only phrases that start with the wake phrase are acted on.
2. **Plan.** The text is split into steps. Each step gets one Jev call that classifies it (open, quit, volume, website, press, timer…). Python then pulls out the target: an installed app, a checked URL, a percentage, a control on screen.
3. **Run.** One engine runs every request, voice or CLI, on a single serial queue. It asks first where your settings say to, runs each action under a time limit, and polls a readback until it sees the result or runs out of time.
4. **Reply.** The spoken line is chosen from what actually happened, never from what was intended.

Architecture, module map and safety rules: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). The formal behaviour spec is [docs/ENGINE_CONTRACT.md](docs/ENGINE_CONTRACT.md).

## Privacy

- Audio never leaves your Mac. Transcription is local (Whisper) or on-device only (Apple).
- Jev sees the words of each command, and for screen commands only the names of controls. Text read off the screen is shared only where Accessibility confirms it is ordinary text, never from inside a text field, and password fields are never read or typed into.
- Everything Jev says is synthesized by Fish Audio, so spoken lines go to Fish. Control names are never spoken.
- A local diagnostic log (`~/Library/Logs/Hey Jev/requests.jsonl`) records each request stage with keys redacted. It stays on your Mac.

## What it costs

- **Jev:** about $0.00004 per command.
- **Fish Audio:** the `s2.1-pro-free` model is free until the end of November 2026; after that `s2.1-pro` is $15 per million characters. Fixed replies are cached, so normal use is a few cents a day.
- **Whisper / Apple dictation:** free, on your Mac.
- **Answers (optional):** Claude Haiku via OpenRouter, about $0.0002 per answer.

## Documentation

| Doc | For |
| --- | --- |
| [docs/FEATURES.md](docs/FEATURES.md) | Every command, example phrases, what it checks, its limits |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Settings, keys, permissions, stored files, logs, privacy |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Common problems and what the app's messages mean |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How the pieces fit, request lifecycle, safety invariants |
| [docs/ENGINE_CONTRACT.md](docs/ENGINE_CONTRACT.md) | The engine's exact rules: step states, confirmation, readback |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Running from source, tests, building, contributing |
| [CHANGELOG.md](CHANGELOG.md) | What changed in this fork |
| [docs/history/](docs/history/) | Design notes written before the build, kept for context |

## Credits and license

Hey Jev was created by Henryk Brzozowski ([henryklunaris/hey-jev](https://github.com/henryklunaris/hey-jev)). This fork was extended by Legion Media, built with Claude, ChatGPT and Muse Spark working through Buzz.

`ax_walk.py` is adapted from [awlevin/typesafe-computer-use](https://github.com/awlevin/typesafe-computer-use) under the MIT License; see [LICENSES/typesafe-computer-use.txt](LICENSES/typesafe-computer-use.txt).

The upstream project does not include a license file, so this fork does not add one. Ask the original author before reusing the upstream code.
