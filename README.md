# Hey Jev

A voice assistant for your Mac. Say "Hey Jev" or hold right Option, say a thing, it does it and answers back.

- **Jev** (OpenRouter or TypeSafe direct) makes every decision in one call, about $0.00004 per request
- **Fish Audio S2.1 Pro** speaks every reply, with emotion tags like `[chuckling]` and `[sighing]`
- **Whisper** (local, faster-whisper) turns your voice into text
- An optional LLM only wakes up when Jev says you asked a question, not a command

**Mac only.** Works on macOS Sequoia and Tahoe. It controls the Mac through AppleScript and the Keychain, so it won't run on Windows or Linux.

## What it can do

Open or quit apps, Mac volume up / down / mute / set, Spotify volume, play / pause / next / previous, dark mode, lock or sleep the Mac. Two things in one sentence work too: "pause Spotify and open Slack".

Timers and reminders: "set a timer for 5 minutes", "remind me in 20 minutes to call Mum", "how long is left?", "cancel the timer". Each one counts down live in the window, and she tells you when it's done.

With deeper answers enabled, questions such as "who wrote Hamlet" go to Claude Haiku via OpenRouter. Otherwise the app gives a scripted reply.

## What you need

- A Mac
- Python 3 (tested on 3.14, see below if you don't have it)
- The Spotify desktop app, for the music commands
- **Voice:** a [Fish Audio](https://fish.audio/) API key. The app currently uses Fish for all spoken replies.
- **Jev decisions:** choose either an [OpenRouter](https://openrouter.ai/) API key (no TypeSafe account needed) or a direct [TypeSafe](https://typesafe.ai/) API key.
- **Deeper answers (optional):** a separate OpenRouter key for Claude Haiku. Disable deeper answers to run without it. The same OpenRouter credential may be entered for both roles, but the app stores and uses them separately.

### Don't have Python?

Check in Terminal:

```bash
python3 --version
```

If that prints a version, you're set. If not, pick one:

- **Easiest:** download the macOS installer from [python.org/downloads](https://www.python.org/downloads/) and run it.
- **With Homebrew:** `brew install python`

## Setup

```bash
git clone https://github.com/henryklunaris/hey-jev.git
cd hey-jev
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt setuptools py2app
.venv/bin/python setup.py py2app -A
open "dist/Hey Jev - Fish Audio.app"
```

The py2app line builds the app bundle in alias mode, so it runs the code straight from this folder. Build it once, and again only if you move the folder.

First launch:

1. The Keys panel opens. Choose the Jev provider and whether to enable deeper answers. Enter the keys required for those choices. They are saved in your Mac Keychain. Change them any time with **Keys…**.
2. Whisper downloads its `small.en` model (about 250MB), one time.
3. macOS will ask for **Microphone** access. Say yes.
4. Add "Hey Jev - Fish Audio" (or your terminal, if you run from the terminal) under **System Settings > Privacy & Security > Accessibility**, or key presses are ignored.
5. The first time it quits an app or toggles dark mode you'll get an **Automation** prompt. Say yes.

The window goes green when it's ready. The switch in the bottom right picks how you talk to it:

- **Hold Option:** hold right Option, talk, let go.
- **Hey Jev:** always listening. Say "Hey Jev, open Spotify" in one go, or say "Hey Jev", wait for her reply, then give the command.

### Or let Claude Code set it up

Paste this into Claude Code with the repo link:

> Clone https://github.com/henryklunaris/hey-jev and set it up on my Mac. Check Python 3 is installed and help me install it if not. Create a venv from requirements.txt, build the app with `python setup.py py2app -A`, then tell me which API keys I need, where to get them, and which macOS permissions to grant. Then open the app from the dist folder.

Use Claude Code (the terminal, or the Code tab in the desktop app). The chat side of Claude Desktop runs commands in a Linux sandbox, not on your Mac, so the Mac only packages fail there.

## Using the window

- **Minimise** with the yellow button or Cmd+M.
- **Close** hides the window but keeps it listening. Click the Dock icon to bring it back.
- **Keep on Top** in the Window menu (Cmd+T) keeps it above other apps. Off by default.
- **Quit** with Cmd+Q.

## Running from the terminal

Useful for seeing the Jev trace (every question, answer and confidence per turn):

```bash
.venv/bin/python siri.py               # hold right Option mode, trace prints to the terminal
.venv/bin/python siri.py --wake        # Hey Jev mode, always listening
.venv/bin/python siri.py --text "open spotify, then turn it down"   # one turn in a fresh process, no mic
.venv/bin/python siri.py --ui          # same as the app, but shows as "Python" in the Dock
```

### Sending commands to the running app

`jevctl` sends text into the app that is already running, through the same classifier, queue and confirmation rules as your voice. It prints a JSON result naming each step and whether it was actually checked.

```bash
./jevctl command --text "open Safari, then go to google.com"
./jevctl command --text-file request.txt --id my-id-1 --wait 60
./jevctl status my-id-1
./jevctl cancel my-id-1
```

Only `completed` means the app saw the result. `unverified` means it did the thing but could not check it; `unknown` means it may or may not have happened. `jevctl` never retries: re-sending the same id returns the stored result. The socket lives in `~/Library/Application Support/Hey Jev/run/` and only accepts your user account. Settings > Confirmations chooses which kinds of action pop up from the menu bar for approval first.

Keys can also go in a `.env` file in this folder: `FISH_AUDIO_API_KEY` for voice, `JEV_OPENROUTER_API_KEY` or `TYPESAFE_API_KEY` for Jev, and `OPENROUTER_API_KEY` for optional deeper answers. Set `JEV_PROVIDER=openrouter|typesafe` and `ANSWER_PROVIDER=disabled|openrouter` to choose routes from the terminal. A `.env` value takes priority over Keychain. By default, Jev uses TypeSafe if a direct TypeSafe key already exists, otherwise OpenRouter; deeper answers default to enabled only if their OpenRouter key already exists.

## How it works

1. Audio is recorded while you hold right Option. In Hey Jev mode the mic stays open, and each phrase is transcribed locally and only acted on if it starts with "Hey Jev".
2. faster-whisper transcribes it locally for free, about 0.8s.
3. The text is split into steps on "then", "after that" and ", and". Each step gets one Jev call that picks the kind of action (open, quit, volume, website, timer and so on). Jev never writes code or picks targets.
4. Python pulls the target out of the words: the app name is matched against the apps actually installed on this Mac (`app_catalog.py`), a web address is checked before use. Two matching apps means it asks which one.
5. One engine (`engine.py`) runs steps one at a time for voice and `jevctl` alike, asks first where Settings say so, runs each action with a time limit, then reads the result back. It stops at the first step that did not verifiably work.
6. The reply is chosen from what actually happened, then played. Fixed lines are pre-rendered into `cache/tts/`, so replies are instant.

Below 0.65 confidence it asks you to say it again, twice in a row and it gives up. The full rules are in `docs/ENGINE_CONTRACT.md`.

## What it costs

- **Fish Audio:** $0. The `s2.1-pro-free` model string on the API is free until the end of November 2026. You don't need to top up API credits. (Their MCP and web playground bill your plan credits instead, this app doesn't use those.) After November the paid `s2.1-pro` is $15 per million characters, and the cached replies mean a normal day of use is a few cents.
- **Jev:** $0.042 per million input tokens, output free. One command is about $0.00004, a two part command about $0.00011.
- **Whisper:** free, runs on your Mac.
- **OpenRouter deeper answers (if enabled):** Claude Haiku, about $0.0002 per answer.

## Troubleshooting

- **Holding Option does nothing.** The app needs Accessibility access. Add it under System Settings > Privacy & Security > Accessibility, then quit and reopen it.
- **"401 Unauthorized" in the window.** One of your keys is wrong or expired. Re-paste it with the Keys… button. If you also have a `.env`, check the key there, because it wins over the Keychain.
- **The app won't open again.** It's probably still running with the window closed. Click its Dock icon, or quit it properly with Cmd+Q and open it again.
- **It stopped controlling apps after a macOS update.** Updates can reset permissions. Check Microphone, Accessibility and Automation under Privacy & Security again.

## Files

- `siri.py` microphone, Whisper, speech, replies and the voice loop
- `engine.py` the shared queue, confirmation gate, step runner and results
- `planner.py` the Jev questions, step splitting and argument extraction
- `actions.py` every action: resolve the target, run it, read it back
- `app_catalog.py`, `url_adapter.py`, `timers.py` installed apps, websites, timers
- `bridge.py` and `jevctl` the command socket and its client
- `assistant_ui.py` the status window, mode switch and Keys panel
- `secrets_store.py` Keychain read / write
- `app.py` and `setup.py` the app bundle entry point and the py2app config, output lands in `dist/`
- `assets/` the app icon

## Change the voice

`VOICE_ID` at the top of `siri.py`. Find voices at [https://fish.audio](https://fish.audio), open one and copy its ID from the page link. Her replies re-render in the new voice automatically on the next launch.
