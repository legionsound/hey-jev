# Hey Jev

Voice control for your Mac. Say "Hey Jev" or hold the right Option key, tell it what you want, and it does the thing. Then it checks whether it actually worked before telling you so.

```text
"Hey Jev, open Safari, then go to github.com"
"Hey Jev, set the volume to 40 percent"
"Hey Jev, what can I click?"
"Hey Jev, click the third video"
"Hey Jev, take over: turn on dark mode in System Settings"
```

This is a fork of [henryklunaris/hey-jev](https://github.com/henryklunaris/hey-jev). The command engine was rebuilt from scratch. On top of that, the fork adds screen control across every visible window, a command-line bridge into the running app, Apple on-device dictation, a wake phrase you choose yourself, answers from Apple's models or a Claude Code or Codex session, and a redesigned Settings window. [CHANGELOG.md](CHANGELOG.md) has the full list.

Hey Jev runs on macOS only (Sequoia or later). It works through Accessibility, AppleScript and the Keychain, so there is no Windows or Linux version.

## How it's different

Most voice assistants guess what you meant, do something, and announce success either way. Hey Jev takes the opposite approach.

It checks before it says "done." After most actions it reads the result back: the app is really running, the volume really is 40, the Chrome tab really shows that address. Some actions have nothing to read back, like locking the screen or navigating in Safari. For those it tells you plainly that it sent the command but couldn't confirm it.

A model decides and plain Python acts. Jev is a small classifier model that answers narrow questions, such as "is this a command, and what kind?" When it chooses between options (two copies of an app, a card on screen, the next step of a task), its choice is checked against a fixed list of real candidates. No model writes or runs code.

Nothing runs twice. A multi-step request stops at the first step that didn't verifiably work and tells you what already happened. Side effects are never retried, and re-sending a request returns the stored result instead of running it again.

You decide what needs your OK. Each kind of action (opening apps, quitting apps, clicking, typing, starting a task, and so on) is set to *Ask first* or *Automatic*. When it asks, a small confirmation drops down from the menu bar, and you can answer by voice.

## What it can do

| Area | Try saying |
| --- | --- |
| Apps | "open Slack", "quit Chrome", "open Mac whisper" (finds MacWhisper). If two copies match, it asks which, or picks the one that's running or you opened last. |
| Volume | "turn it up", "set volume to 40 percent", "turn it down by 10 percent", "mute", "turn Spotify down" |
| Music and video | "play", "pause", "next track" for Spotify. "Pause the video" presses the player on screen instead. |
| System | "dark mode on", "lock the screen", "put the Mac to sleep" |
| Timers | "set a timer for 5 minutes", "remind me in 20 minutes to call Sam", "how long is left?", "cancel the timer" |
| Web | "go to github.com", "go to YouTube", "search Google for rock and roll", "open github.com in Safari" |
| Screen | "what can I click?", "click Share", "click 4", "type hello into the search field", "press return", "scroll down", "play the video by Frame Set" |
| Writing | "write a reply saying I'll be late", "draft a thank-you note in the message field". Apple's on-device model writes it, then it's typed into the field. |
| Tasks | "take over: …" or "work on: …" gives Jev a goal to work through step by step in the current app |
| Questions | "who wrote Hamlet?", "what time is it?" (optional; you choose who answers) |
| Chaining | "pause Spotify, then open Slack". Up to five steps joined with "then", "after that" or ", and". A bare "and" never splits a request, so "play rock and roll" stays one thing. |

Say "stop" at any point to cancel what's running and everything queued behind it.

### Your own wake phrase

"Hey Jev" is only the default. In Settings > Transcription you can change it to anything up to four words, like "Computer" or "Okay Mac", and it takes effect right away without a restart. Speech recognition doesn't always spell unusual words the same way, so you can add up to six extra spellings it should also accept. Or press Teach Jev, say your phrase five times, and Hey Jev shows the spellings it actually heard so you can add them with one click. Very short or common words still work, but it warns you they may wake it by accident.

### Screen control

Choose Show what Jev sees from the menu bar and Hey Jev draws a number on every button, link, tab and field it can find in every visible window, including windows behind the front one. Say "click 4" and it presses number 4 in whatever window that number belongs to. You can also go by name ("click Share"), by position ("the video in the bottom-right"), or by description ("the Full Tilt video").

Controls come from macOS Accessibility, with Apple's on-device text recognition filling in text that Accessibility can't see. Chrome and other Chromium apps ignore Accessibility clicks on page content, so on web pages Hey Jev focuses the control and presses Return instead. The mouse pointer never moves. Anything a window on top is covering is refused rather than clicked blind.

Before each action, a second pointer with a small J glides to the control it's about to use, so you can see what it's doing. Your real mouse stays where you left it. You can turn it off from the menu bar.

If a click or Return would run without asking (because you set clicks to Automatic, or it's a step inside a task), Jev takes a second look first: would this send, delete, buy, share, merge or approve something? If so, Hey Jev asks you before doing it. This check can only add a question, never skip one.

### Answers

Hey Jev can also answer questions out loud. In Settings > Answers, pick one:

- **Off.** Questions are ignored.
- **OpenRouter.** Any model on your OpenRouter key.
- **Apple.** Apple's own language model, either on this Mac or through Apple Private Cloud Compute. Needs macOS 26 or later with Apple Intelligence turned on.
- **Claude Code or Codex.** A full agent session with your own tools, settings and login. The agent can take actions of its own, and every permission it asks for shows up in the same confirmation drop-down, where you approve or decline it. Hey Jev never approves anything for you.

Hey Jev never turns an answer into one of its own actions, and if your chosen provider isn't available it tells you why instead of quietly switching to a different one.

## Getting started

You'll need:

- A Mac with Python 3. It was developed on 3.14. Check with `python3 --version`, or install it from [python.org](https://www.python.org/downloads/) or with `brew install python`.
- A [Fish Audio](https://fish.audio/) API key for the voice.
- A key for Jev: either [OpenRouter](https://openrouter.ai/) or a direct [TypeSafe](https://typesafe.ai/) key.
- Optional: the Spotify desktop app for music commands.
- Optional: an OpenRouter key, macOS 26 with Apple Intelligence, or Claude Code or Codex installed and signed in, if you want spoken answers.

```bash
git clone https://github.com/legionsound/hey-jev.git
cd hey-jev
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt setuptools py2app
.venv/bin/python setup.py py2app -A
helpers/heyjev-fm/build.sh        # optional: Apple answers (macOS 26+)
open "dist/Hey Jev.app"
```

`py2app -A` builds the app in alias mode, so the app runs the code straight from this folder. After pulling new code, restarting the app is enough. You only need to rebuild if you move the folder.

The first time you open it:

1. Settings opens and asks for your keys. They're stored in your Mac's Keychain.
2. Whisper downloads its speech model once (small.en, about 480 MB). To skip that, choose Apple dictation in Settings > Transcription.
3. Allow the microphone when macOS asks.
4. Turn on Accessibility for Hey Jev in System Settings > Privacy & Security > Accessibility. The right Option key and all screen control depend on it.
5. Allow Automation the first time Hey Jev controls Spotify, Safari, Chrome or System Events.
6. Optionally, allow Screen Recording so it can read on-screen text that Accessibility doesn't expose.

Settings > Permissions shows all of these in one place, with the current status of each and a button to request it or jump to the right page in System Settings.

Finally, pick how you want to talk to it using the switch in the main window. **Hold Option** listens while you hold the right Option key. **Hey Jev** listens all the time for your wake phrase.

## Command line

`jevctl` sends typed commands into the running app. They go through the same planner, queue and confirmation rules as your voice, and you get a JSON result for each step.

```bash
./jevctl command --text "open Safari, then go to google.com"
./jevctl command --text-file request.txt --id my-id-1 --wait 60
./jevctl status my-id-1
./jevctl cancel my-id-1
./jevctl trials --last 5        # what recent requests heard, planned, did and said
```

Each step comes back with a status. Only `completed` means the app saw the result. `unverified` means it acted but had no way to check, and `unknown` means it may or may not have happened. `jevctl` never retries, and sending the same id again returns the stored result. The socket only accepts connections from your own user account.

## How it works

```text
mic ─► Whisper / Apple dictation ─► wake phrase ─► planner ─► engine ─► actions ─► readback ─► reply (Fish voice)
                                                       ▲
                                    jevctl ─► socket ──┘
```

1. **Hear.** Speech is transcribed on your Mac, by faster-whisper or Apple's on-device recognizer. In wake mode, only phrases that start with the wake phrase count.
2. **Plan.** The request is split into steps. Jev classifies each step (open, quit, volume, website, press, timer, and so on). Python then pins down the target: an installed app, a checked URL, a percentage, a control on screen. Direct screen commands like "scroll down" skip classification.
3. **Run.** One engine runs every request, from voice or the command line, one at a time. It asks first where your settings say to, runs each action under a time limit, and keeps checking the result until it sees it or runs out of time.
4. **Reply.** What Hey Jev says is based on what actually happened, not on what it meant to do.

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) explains how the pieces fit together. [docs/ENGINE_CONTRACT.md](docs/ENGINE_CONTRACT.md) is the precise rulebook: every step state and what each action checks.

## Privacy

- Your audio stays on your Mac. Transcription runs locally with Whisper, or on-device with Apple dictation.
- Jev sees the words of each command. For screen commands it sees control names and the text around them, only where Accessibility confirms it's ordinary on-screen text. Nothing is read from inside text fields, and password fields are never read or typed into.
- Everything Hey Jev says is voiced by Fish Audio, so spoken replies go to Fish. Control names are never spoken aloud.
- Answers go to whichever provider you picked. Apple's on-device option keeps them on your Mac.
- Text Hey Jev writes into fields for you is written by Apple's model on your Mac. What's already typed in fields and passwords is never given to it.
- A diagnostic log at `~/Library/Logs/Hey Jev/requests.jsonl` records each stage of every request, with keys removed. It never leaves your Mac.

## What it costs

- **Jev:** one small classifier call per step, sometimes a few more for screen picks. Rates are on the [TypeSafe](https://typesafe.ai/) and [OpenRouter](https://openrouter.ai/) sites.
- **Fish Audio:** billed per spoken line. Fixed replies are cached so they're only paid for once. See [Fish Audio pricing](https://fish.audio/).
- **Transcription:** free. Both options run on your Mac.
- **Answers:** depends on your choice. OpenRouter bills per question, Apple's on-device model is free, and Claude Code or Codex use your existing plan.

## Documentation

| Doc | What's in it |
| --- | --- |
| [docs/FEATURES.md](docs/FEATURES.md) | Everything it can do, in detail |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Every Settings pane, permissions, where files live |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Common problems and fixes |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How the pieces fit together |
| [docs/ENGINE_CONTRACT.md](docs/ENGINE_CONTRACT.md) | The engine's exact rules: step states, confirmation, readback |
| [docs/ANSWER_PROVIDERS.md](docs/ANSWER_PROVIDERS.md) | How the answer providers work |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Setup, tests, contributing |
| [CHANGELOG.md](CHANGELOG.md) | What changed in this fork |

The rest of `docs/` (the overhaul spec, command architecture, capability map and handoff notes) are planning documents from before the rebuild, kept for history.

## Project status

Hey Jev is in daily use and under active development. All 632 automated tests pass, and most features have been tried in the running app. A few edge cases haven't had a live run yet: pressing a control by voice in a window that isn't in front, switching dictation mid-sentence, and Apple dictation with no network.

## Contributing

Development happens on [legionsound/hey-jev](https://github.com/legionsound/hey-jev). Issues and pull requests are welcome there. Changes from this fork aren't sent to the original project.

## Credits and license

Hey Jev was created by Henryk Brzozowski ([henryklunaris/hey-jev](https://github.com/henryklunaris/hey-jev)). This fork is maintained by Legion Media.

`ax_walk.py` is adapted from [awlevin/typesafe-computer-use](https://github.com/awlevin/typesafe-computer-use) under the MIT License. See [LICENSES/typesafe-computer-use.txt](LICENSES/typesafe-computer-use.txt).

The original project doesn't include a license file, so this fork doesn't add one either. Please ask the original author before reusing the upstream code.
