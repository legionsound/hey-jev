"""Mac voice assistant: hold right Option or say "Hey Jev", then speak. Jev classifies, the engine acts, Fish speaks.

Voice and `jevctl` both submit text to one engine (engine.py); this file owns the microphone, transcription and speech.
"""
import os, re, sys, json, time, queue, random, argparse, subprocess, threading, hashlib, collections, contextlib
import requests
from dotenv import load_dotenv
from secrets_store import get_secret, get_setting, missing_secrets
import wake
from model_settings import (answer_payload, answer_settings, confirm_policy, tiebreak_threshold, transcription_backend,
                            wake_settings)
import diagnostics
import planner
import timers
import voice_output
from engine import Engine

load_dotenv()
TS_KEY = get_secret("TYPESAFE_API_KEY")
JEV_OR_KEY = get_secret("JEV_OPENROUTER_API_KEY")
FISH_KEY = get_secret("FISH_AUDIO_API_KEY")
OR_KEY = get_secret("OPENROUTER_API_KEY")
JEV_PROVIDER = get_setting("JEV_PROVIDER")
ANSWER_PROVIDER = get_setting("ANSWER_PROVIDER")
VOICE_ID = "9a9cf47702da476aa4629e2506d4a857"
SAMPLE_RATE = 16000
WHISPER_MODEL = "small.en"
COMMAND_PROMPT = "Open Spotify. Set a timer for five minutes. Play. Pause. Next track. Turn Spotify down. Turn the Mac volume down. Mute. Dark mode on. Lock the screen."
# Whisper often hears "Jev" as Jeff or Jeb, so accept the close ones
WAKE = wake.Wake()  # replaced from preferences at start and live from Settings; see set_wake()
WAKE_WINDOW = 6.0
TURN_WAIT = 180  # covers a 60 s confirmation plus the steps
ENGINE = None  # the one engine in this process; the UI calls ENGINE.decide()
BRIDGE = None


def reload_keys():
    global TS_KEY, JEV_OR_KEY, FISH_KEY, OR_KEY, JEV_PROVIDER, ANSWER_PROVIDER
    TS_KEY = get_secret("TYPESAFE_API_KEY")
    JEV_OR_KEY = get_secret("JEV_OPENROUTER_API_KEY")
    FISH_KEY = get_secret("FISH_AUDIO_API_KEY")
    OR_KEY = get_secret("OPENROUTER_API_KEY")
    JEV_PROVIDER = get_setting("JEV_PROVIDER")
    ANSWER_PROVIDER = get_setting("ANSWER_PROVIDER")


# --------------------------------------------------------------------------- Jev
def jev(text, questions=None):
    t = time.time()
    if JEV_PROVIDER == "openrouter":
        url, model, key = "https://openrouter.ai/api/alpha/decisions", "typesafe/jev-1.13", JEV_OR_KEY
    else:
        url, model, key = "https://api.typesafe.ai/v1/systemone", "jev-latest", TS_KEY
    r = requests.post(url, json={"model": model, "state": text, "questions": questions or planner.QUESTIONS},
                      headers={"Authorization": f"Bearer {key}"}, timeout=30, allow_redirects=False)
    r.raise_for_status()
    j = r.json()
    ans = {}
    for k, a in j["answers"].items():
        if a["type"] == "noul":  # probability, confidence is distance from 0.5
            ans[k] = (a["noul"] >= 0.5, max(a["noul"], 1 - a["noul"]))
        elif a["type"] == "score":  # index into the rubric, legend maps it back to the label
            ans[k] = (a["legend"][str(int(round(a["score"])))], a.get("confidence", 0))
        else:
            ans[k] = (a["choice"], a.get("confidence", 0))
    cost = j.get("usage", {}).get("input_tokens", 0) * 0.042 / 1e6
    return ans, int((time.time() - t) * 1000), cost


TIEBREAK_MAX_CHOICES = 6


def tiebreak(clause, choices):
    """Ask Jev which duplicate the user most likely meant. -> (index, score) or None. Engine applies the threshold."""
    import actions
    if not 2 <= len(choices) <= TIEBREAK_MAX_CHOICES:
        return None
    hints = actions.app_hints(choices, time.monotonic() + 3)
    criteria = {}
    for i, h in enumerate(hints):
        bits = [f"{h['name']} in {h['folder']}"]
        if h["running"]:
            bits.append("running right now")
        if h["last_opened"]:
            bits.append(f"last opened {h['last_opened']}")
        criteria[f"app_{i}"] = ", ".join(bits)
    q = {"pick": {"type": "choice", "instructions": "Several installed apps match. Which one did the user most likely mean?",
                  "criteria": criteria}}
    ans, ms, cost = jev(clause, q)
    choice, conf = ans["pick"]
    print(f"  jev tiebreak {clause!r}: {choice} {conf:.2f} ({ms}ms ${cost:.6f})")
    return int(choice.split("_")[1]), conf


def choose_control(spoken, labels):
    """Jev picks which on-screen control the user named. Only the control names and the spoken words are sent."""
    from engine import current_rid
    names = labels[:250]
    ids = [f"c{i}" for i in range(len(names))]  # opaque keys: control text can never collide with a protocol choice
    q = {"control": {"type": "choice", "instructions": f"Which on-screen control did the user mean by: {spoken}",
                     "criteria": {**{k: f"the control named “{n}”" for k, n in zip(ids, names)},
                                  "none": "none of these controls"}}}
    try:
        ans, ms, _ = jev(spoken, q)
    except Exception as exc:
        diagnostics.record(current_rid(), "choose_control", "error", error=repr(exc), options=len(names))
        return None, 0.0
    pick, conf = ans["control"]
    diagnostics.record(current_rid(), "choose_control", "ok", ms, options=len(names), confidence=round(conf, 2))
    return (ids.index(pick) if pick in ids else None), conf


def classify(clause):
    from engine import current_rid
    try:
        ans, ms, cost = jev(clause)
    except Exception as exc:
        diagnostics.record(current_rid(), "classify", "error", clause=clause, provider=JEV_PROVIDER, error=repr(exc))
        raise
    diagnostics.record(current_rid(), "classify", "ok", ms, clause=clause, provider=JEV_PROVIDER,
                       answers={k: [v, round(c, 2)] for k, (v, c) in ans.items()})
    print(f"  jev {clause!r}: {ms}ms ${cost:.6f}")
    for k, (v, c) in ans.items():
        print(f"    {k:15} {str(v):22} {c:.2f}{'' if c >= planner.GATE else '  <- below gate'}")
    return ans


# --------------------------------------------------------------------------- Scripted replies with Fish tags
REPLIES = {
    "app.open": ["[cheerful] {app}'s up.", "{app}, opening now.", "[chuckling] There you go, {app}."],
    "app.quit": ["{app}'s gone.", "[sighing] Closing {app}. Good riddance.", "Done, {app} is closed."],
    "url.open": ["[cheerful] There's the page.", "Opened it."],
    "volume.up": ["Louder it is.", "[cheerful] Turning it up.", "Up we go."],
    "volume.down": ["Bringing it down.", "[sighing] A little quieter.", "Turning it down."],
    "volume.mute": ["[sighing] Muting. Finally some quiet.", "Muting.", "Shh. Muting."],
    "volume.unmute": ["Sound's back.", "[cheerful] Unmuted.", "And we're back."],
    "volume.set": ["Set to {level}.", "Volume's {level} now."],
    "spotify_volume.up": ["Turning Spotify up.", "[cheerful] Spotify's louder."],
    "spotify_volume.down": ["Turning Spotify down.", "Spotify's a little quieter."],
    "spotify_volume.mute": ["Spotify's muted.", "[sighing] Muted Spotify."],
    "spotify_volume.unmute": ["Spotify's sound is back.", "[cheerful] Spotify's unmuted."],
    "spotify_volume.set": ["Spotify's set to {level}.", "Set Spotify to {level}."],
    "display.dark_on": ["[chuckling] Lights off.", "Dark mode on.", "Going dark."],
    "display.dark_off": ["[cheerful] Let there be light.", "Dark mode off.", "Back to light."],
    "display.toggle": ["Flipped it.", "There, switched."],
    "media.play": ["[cheerful] Playing.", "Music's on.", "Here we go."],
    "media.pause": ["Paused.", "[sighing] Pausing. Take your time.", "Holding it there."],
    "media.next": ["Skipping.", "[chuckling] Not a fan? Next one.", "Next track."],
    "media.previous": ["Going back one.", "Previous track.", "[chuckling] Again? Sure."],
    "system.lock": ["Locking up. See you soon.", "Locking the screen."],
    "system.sleep": ["Good night.", "Going to sleep now.", "[sighing] Finally, a nap."],
    "timer.set": ["[cheerful] Timer's set.", "On it. I'll let you know.", "Done, counting down."],
    "reminder_set": ["Got it, I'll remind you.", "[cheerful] Sure, I'll give you a shout."],
    "timer.check": ["{left} left.", "You've got {left} to go."],
    "screen.list": ["I can see {count} things in {app}. They're numbered on screen.",
                    "{count} things in {app}, numbered on screen. Say click and a number."],
    "screen.press": ["Clicked it.", "[cheerful] Done, clicked."],
    "timer.cancel": ["Timer cancelled.", "[sighing] Fine, no timer then."],
    "timers_cancel": ["All timers cancelled.", "Cleared them all."],
    "timer_none": ["[chuckling] There's no timer running."],
    "timer_unclear": ["[clear throat] How long for?"],
    "timer_done": ["[cheerful] Time's up!", "[chuckling] Ding ding, time's up."],
    "reminder_done": ["[cheerful] Hey, just a reminder: {label}.", "Reminder: {label}."],
    "info": ["[chuckling] That's a question, not a command. I'll get a brain for that soon.",
             "[sighing] I can't answer that one yet."],
    "chit_chat": ["[chuckling] Hi. Give me something to do.", "[cheerful] Hey. I'm listening."],
    "compound_done": ["[chuckling] Done, all of it.", "[cheerful] All done.", "All sorted."],
    "wake": ["Yes?", "[cheerful] Mm-hm?", "I'm listening."],
    "clarify": ["[clear throat] Sorry, say that again?", "Hm, one more time?"],
    "give_up": ["[sighing] I'm not sure what you mean. Try saying it differently?"],
    "split_please": ["[clear throat] Say that as one thing, then the next."],
    "bad_percent": ["[clear throat] Volume goes from 0 to 100 percent, in whole numbers. Try again?"],
    "too_many": ["[sighing] That's a lot at once. Five steps at most, please."],
    "unsupported": ["[chuckling] I know what you want, I just can't do that one yet."],
    "failed": ["[sighing] That didn't work.", "Hm, that didn't go through."],
    "unknown": ["[clear throat] I'm not sure that worked. Check before I try again."],
    "unverified": ["I sent that, but I couldn't check whether it worked.", "Asked for it, but I can't confirm it happened."],
    "declined": ["Okay, I won't.", "Cancelled."],
    "cancelled": ["Stopped."],
    "busy": ["[sighing] I'm swamped, give me a second."],
    "answer_failed": ["[sighing] I couldn't get an answer to that just now.", "Hm, my answer didn't come through."],
}
SPEAK_FIRST = {"volume.mute", "system.lock", "system.sleep"}  # speech can't follow these


def say_line(key, **fmt):
    return random.choice(REPLIES[key]).format(**fmt)


def step_line(step):
    """Success line for one completed step, from what the step actually observed."""
    action, target, facts = step["action"], step.get("target") or {}, step.get("facts") or {}
    if action == "timer.set":
        return say_line("reminder_set" if target.get("label") else "timer.set")
    if action == "timer.cancel":
        return say_line("timers_cancel" if target.get("all") else "timer.cancel")
    if action == "timer.check":
        return say_line("timer.check", left=facts.get("left", "some time"))
    if action == "screen.list":
        line = say_line("screen.list", count=facts.get("count", 0), app=facts.get("app") or "this window")
        return line + (" Allow Screen Recording and I can read the text too." if facts.get("ocr") == "no_permission" else "")
    if action == "screen.press":
        return say_line("screen.press")  # never the label: speech goes to a remote voice service
    return say_line(action, app=target.get("name") or target.get("app") or "it", level=target.get("level") or "that")


misses = 0


def line_for(result):
    """What to say about a finished request. Never claims more than the result shows."""
    global misses
    state, steps = result["state"], result.get("steps", [])
    if state != "needs_clarification":
        misses = 0
    if state == "answered":
        return result.get("say") or say_line(result.get("reply") or "info")
    if state == "completed":
        return step_line(steps[0]) if len(steps) == 1 else say_line("compound_done")
    if state == "needs_clarification":
        bad = next((s for s in steps if s["state"] == "needs_clarification"), None)
        if bad and bad["action"] == "screen.press":  # control text is never spoken: speech goes to a remote service
            if bad["facts"].get("choices"):
                return "[clear throat] There's more than one of those. Ask what you can click, then say a number."
            return say_line("clarify")
        if bad and bad["facts"].get("choices"):
            names = [c["name"] + (f" in {os.path.basename(os.path.dirname(c['path']))}" if c.get("path") else "")
                     for c in bad["facts"]["choices"][:4]]
            return "[clear throat] Which one? " + ", ".join(names[:-1]) + " or " + names[-1] + "?"
        detail = result.get("detail")
        if detail == "compound_unsplit":
            return say_line("split_please")
        if detail == "bad_percent":
            return say_line("bad_percent")
        if detail == "unsupported_browser":
            return "[clear throat] I can only open websites in Safari or Chrome."
        if detail == "too_many_steps":
            return say_line("too_many")
        misses += 1
        if misses >= 2:
            misses = 0
            return say_line("give_up")
        return say_line("clarify")
    if result.get("error") == "answer_failed":
        return say_line("answer_failed")
    stop = next((s for s in steps if s["state"] not in ("completed", "skipped", "not_started")), None)
    done = [s for s in steps if s["state"] == "completed"]
    why = stop["state"] if stop else state
    if stop and stop["state"] == "failed" and stop["facts"].get("error") == "not_found":
        miss = {"app.open": "I can't find that app.", "app.quit": "I can't find that app.",
                "timer.check": say_line("timer_none"), "timer.cancel": say_line("timer_none"),
                "timer.set": say_line("timer_unclear"), "url.open": "That doesn't look like a web address.",
                "screen.press": "I can't find that on screen.", "screen.list": "I can't read this window."}
        line = miss.get(stop["action"], say_line("failed"))
    elif why in REPLIES:
        line = say_line(why)
    else:
        line = say_line("failed")
    if done:
        line = f"Did the first {'part' if len(done) == 1 else f'{len(done)} parts'}, then: {line}"
    return line


# --------------------------------------------------------------------------- Timers and reminders
def prepare_reminder(t, said):
    """While the timer runs, have the LLM write the alert and a short name, and render the audio, so it plays instantly."""
    if ANSWER_PROVIDER != "openrouter" or not OR_KEY:
        return
    try:
        r = requests.post("https://openrouter.ai/api/v1/chat/completions",
                          headers={"Authorization": f"Bearer {OR_KEY}"},
                          json=answer_payload([{"role": "system", "content":
                                    "The user set a reminder with a voice assistant. Reply with JSON only: "
                                    '{"label": "2 to 4 word name for the task, e.g. Call Sam", '
                                    '"alert": "one short friendly sentence the assistant says out loud when the time is up, '
                                    'speaking to the user, e.g. Hey, it\'s time to give Sam a call."}. '
                                    "The alert may start with one tag from [cheerful] [chuckling] [sighing], or none. No markdown."},
                                    {"role": "user", "content": said}], reminder=True), timeout=30, allow_redirects=False)
        r.raise_for_status()
        raw = r.json()["choices"][0]["message"]["content"]
        data = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
        t["label"] = data.get("label") or t["label"]
        fetch_tts(data["alert"])  # cache the audio now
        t["line"] = data["alert"]
        print(f"\n  reminder ready: {t['label']!r} -> {t['line']!r}")
    except Exception as e:
        print(f"\n  reminder prep failed, using the plain line: {e}")


timers.on_reminder_set = prepare_reminder
timer_snapshot = timers.snapshot  # the UI reads this


def timer_done_line(t):
    if t["line"]:
        return t["line"]
    return say_line("reminder_done", label=t["label"]) if t["label"] else say_line("timer_done")


# --------------------------------------------------------------------------- LLM answers (questions only)
def ask_llm(text):
    t = time.time()
    r = requests.post("https://openrouter.ai/api/v1/chat/completions",
                      headers={"Authorization": f"Bearer {OR_KEY}"},
                      json=answer_payload([{"role": "system", "content": "You are a voice assistant. Answer in one short spoken sentence, no markdown. "
                                          "You may start with exactly one tag from: [chuckling] [laughing] [sighing] [cheerful], or none."},
                                         {"role": "user", "content": text}]), timeout=30, allow_redirects=False)
    r.raise_for_status()
    j = r.json()
    content = j["choices"][0]["message"].get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Model returned no spoken answer. Try a larger output token limit in Settings.")
    print(f"  llm {answer_settings()['model']} {int((time.time() - t) * 1000)}ms  ${j.get('usage', {}).get('cost')}")
    return content.strip()


# --------------------------------------------------------------------------- Fish TTS
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "tts")


def fetch_tts(text):
    """Return a wav path for this line, generating it once and caching on disk. Returns (path, ms, cached)."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, hashlib.sha1(f"{VOICE_ID}|{text}".encode()).hexdigest() + ".wav")
    if os.path.exists(path):
        return path, 0, True
    t = time.time()
    r = requests.post("https://api.fish.audio/v1/tts", headers={"Authorization": f"Bearer {FISH_KEY}", "model": "s2.1-pro-free"},
                      json={"text": text, "reference_id": VOICE_ID, "format": "wav"}, timeout=60, allow_redirects=False)
    r.raise_for_status()
    open(path, "wb").write(r.content)
    return path, int((time.time() - t) * 1000), False


def speak(text):
    if voice_output.muted() or voice_output.volume() == 0:
        return 0
    path, ms, cached = fetch_tts(text)
    voice_output.play(path)
    return ms


def warm_cache():
    """Pre-render the fixed lines in the background so replies play instantly. Skipped while the voice is muted."""
    made = 0
    for lines in REPLIES.values():
        for line in lines:
            if "{" in line:
                continue  # lines with a live value are generated when needed
            if voice_output.muted():
                return
            try:
                made += 0 if fetch_tts(line)[2] else 1
            except Exception as e:
                print(f"  cache miss for {line!r}: {e}")
    if made:
        print(f"  cached {made} new reply lines")


def emit(notify, state, detail=""):
    if notify:
        notify(state, detail)


def say(line, notify):
    print(f"  say: {line}")
    emit(notify, "Speaking", line)
    tts_ms = speak(line)
    print(f"  fish {'cached' if tts_ms == 0 else str(tts_ms) + 'ms'}")


# --------------------------------------------------------------------------- Engine wiring
def make_engine(notify=None, ask=None, show=None):
    """The single engine. Voice-sourced mute/lock/sleep get a short spoken line before they run."""
    spoke_first = set()

    def on_event(kind, view, step):
        if kind == "start":
            emit(notify, "Thinking", ("Typed: " if view["source"] == "cli" else "") + (view.get("text") or ""))
        elif kind == "step":
            emit(notify, "Doing it", step["clause"])
            if view["source"] == "voice" and (step["action"] in SPEAK_FIRST or
                                              step["action"] == "volume.set" and (step.get("target") or {}).get("value") == 0):
                spoke_first.add(view["id"])
                key = "volume.mute" if step["action"] == "volume.set" else step["action"]  # "about to", not "done"
                line = say_line(key) if key in REPLIES else "Okay."
                diagnostics.record(view["id"], "speak", "before_dispatch", line=line)
                say(line, notify)
        if kind == "done" and show:
            listed = [s for s in view.get("steps", []) if s["action"] == "screen.list" and s["state"] == "completed"]
            if listed:
                show(listed[-1]["facts"])  # numbered badges over the window
        if kind == "done" and view["source"] == "cli":
            emit(notify, "Ready", f"Typed command: {view['state']}")

    def answer(text):
        from engine import current_rid
        t, model = time.time(), answer_settings().get("model")
        try:
            said = ask_llm(text)
        except Exception as exc:
            diagnostics.record(current_rid(), "answer", "error", (time.time() - t) * 1000, provider=ANSWER_PROVIDER,
                               model=model, error=repr(exc))
            raise
        diagnostics.record(current_rid(), "answer", "ok", (time.time() - t) * 1000, provider=ANSWER_PROVIDER,
                           model=model, said=said)
        return said

    import actions
    actions.CHOOSE = choose_control
    eng = Engine(classify, policy=confirm_policy, ask=ask, tiebreak=tiebreak,
                 threshold=lambda: float("inf") if tiebreak_threshold() >= 100 else tiebreak_threshold() / 100,
                 answer=answer if ANSWER_PROVIDER == "openrouter" else None, on_event=on_event)
    eng.spoke_first = spoke_first
    return eng


def turn(eng, text, notify, hold=contextlib.nullcontext, stt_ms=None):
    """One voice turn: submit, wait, speak from the result. A result that outlives the wait is spoken when it lands,
    inside hold() so it does not talk over the microphone."""
    print(f"\n> heard: {text!r}")
    if not text.strip():
        emit(notify, "Ready", "Didn't catch anything")
        return
    first = eng.submit(text, "voice")
    diagnostics.record(first.get("id"), "recognize", first["state"], text=text, stt_ms=stt_ms)
    if first["state"] in ("busy", "id_conflict"):  # never queued: nothing to wait for
        line = say_line("busy")
        diagnostics.record(first.get("id"), "speak", first["state"], line=line)
        say(line, notify)
        emit(notify, "Ready", line)
        return
    rid = first["id"]
    result = eng.wait(rid, TURN_WAIT)
    if result["state"] not in planner_final():
        emit(notify, "Ready", "Still working on that")
        threading.Thread(target=_late, args=(eng, rid, notify, hold), daemon=True).start()
        return
    _deliver(eng, result, notify)


def _late(eng, rid, notify, hold):
    while True:
        result = eng.wait(rid, 60)
        if result["state"] in planner_final():
            break
    with hold():
        _deliver(eng, result, notify)


def _deliver(eng, result, notify):
    rid = result["id"]
    print("  result: " + json.dumps({k: result.get(k) for k in ("state", "stopped_state", "detail")}) +
          "".join(f"\n    step {s['index']}: {s['action']} {s['state']} {s.get('detail') or ''}" for s in result.get("steps", [])))
    if rid in eng.spoke_first and result["state"] in ("completed", "unverified"):
        eng.spoke_first.discard(rid)
        emit(notify, "Ready", result["state"])
        return
    eng.spoke_first.discard(rid)
    line = line_for(result)
    diagnostics.record(rid, "speak", result["state"], line=line)
    say(line, notify)
    emit(notify, "Ready", line)


def planner_final():
    from engine import TERMINAL
    return TERMINAL | {"busy", "id_conflict", "unknown_outcome"}


# --------------------------------------------------------------------------- Mic + push to talk
class Recorder:
    BLOCK = 1600  # 100ms at 16kHz

    def __init__(self):
        import numpy as np, sounddevice as sd  # imported here so tests never initialise the audio device
        self.np = np
        self.frames, self.on = [], False
        self.wake, self.paused = False, False
        self.enabled, self.epoch = True, 0
        self.segments = queue.Queue()
        self.noise = 0.005
        self._reset_segment()
        self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                     blocksize=self.BLOCK, callback=self._cb)
        self.stream.start()

    def _reset_segment(self):
        self.speech, self.silent = [], 0
        self.preroll = collections.deque(maxlen=3)

    def _cb(self, indata, *_):
        np = self.np
        if not self.enabled:
            return
        if self.on and not self.paused:
            self.frames.append(indata.copy())
        if not self.wake or self.paused or getattr(self, "isolated", False):  # isolated: transcript-only capture
            if self.speech:
                self._reset_segment()
            return
        block = indata[:, 0].copy()
        rms = float(np.sqrt(np.mean(block ** 2)))
        loud = rms > max(self.noise * 3, 0.01)
        if not self.speech:
            if loud:
                self.speech, self.silent = list(self.preroll) + [block], 0
            else:
                self.noise = 0.95 * self.noise + 0.05 * rms  # track the room's background level
                self.preroll.append(block)
            return
        self.speech.append(block)
        self.silent = 0 if loud else self.silent + 1
        if self.silent >= 8 or len(self.speech) >= 150:  # 0.8s pause ends a phrase, 15s max
            if len(self.speech) - self.silent >= 4:
                self.segments.put(np.concatenate(self.speech))
            self._reset_segment()

    def invalidate(self):
        self.epoch += 1
        self.on = False
        self.frames = []
        self._reset_segment()
        while not self.segments.empty():
            try:
                self.segments.get_nowait()
            except queue.Empty:
                break

    def start(self):
        self.frames, self.on = [], True

    def stop(self):
        self.on = False
        return self.np.concatenate(self.frames)[:, 0] if self.frames else self.np.zeros(0, dtype="float32")


class Floor:
    """One owner of the room at a time: the user holding the talk key, or Jev speaking.
    A push-to-talk recording holds the floor from key down to key up, so speech that lands meanwhile waits."""

    def __init__(self, rec):
        self.rec = rec
        self.lock = threading.Lock()   # the floor
        self.state = threading.Lock()  # guards the recording start/stop transition
        self.owner = None              # token of the recording in progress; only its holder may stop it

    def locked(self):
        return self.lock.locked()

    def start_recording(self, isolated=False):
        """-> an ownership token, or None when the floor is taken. isolated: transcript-only capture, the owner's
        audio never feeds wake segmentation. Set with the capture, cleared only by this owner's stop or a drop."""
        with self.state:
            if self.rec.on or not self.lock.acquire(blocking=False):
                return None
            self.owner = object()
            if isolated:
                self._clear_wake()
                self.rec.isolated = True
            self.rec.start()
            return self.owner

    def _clear_wake(self):
        self.rec._reset_segment()
        drain(self.rec.segments)

    def stop_recording(self, token):
        """-> the audio, or None when `token` does not own the running recording (stale, dropped or someone else's)."""
        with self.state:
            if token is None or token is not self.owner or not self.rec.on:
                return None
            self.owner = None
            audio = self.rec.stop()
            if getattr(self.rec, "isolated", False) is True:  # leftovers go before the floor is free for the wake worker
                self._clear_wake()
                self.rec.isolated = False
            self.lock.release()
            return audio

    def drop_recording(self):
        """Mode or mic change: discard any recording and give the floor back if it held it. Old tokens go stale."""
        with self.state:
            was = self.rec.on
            self.owner = None
            self.rec.isolated = False
            self.rec.invalidate()
            if was:
                self.lock.release()

    @contextlib.contextmanager
    def hold(self):
        with self.lock:
            self.rec.paused = True  # don't hear her own reply
            try:
                yield
            finally:
                time.sleep(0.3)
                self.rec.paused = False


def ready_text(wake_mode):
    return WAKE.hint_text() if wake_mode else "Ready when you are"


def runtime_facts():
    """Which code and runtime this is, so a log line can be tied to an exact build. No keys."""
    here = os.path.dirname(os.path.abspath(__file__))
    rev = dirty = None
    try:
        rev = subprocess.run(["git", "-C", here, "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                             timeout=2).stdout.strip() or None
        dirty = bool(subprocess.run(["git", "-C", here, "status", "--porcelain", "--untracked-files=no"],
                                    capture_output=True, text=True, timeout=2).stdout.strip())
    except Exception:
        pass
    return {"revision": rev, "dirty": dirty, "source": here, "python": sys.version.split()[0],
            "executable": sys.executable, "bundle": os.environ.get("RESOURCEPATH"), "jev_provider": JEV_PROVIDER,
            "answer_provider": ANSWER_PROVIDER, "answer_model": answer_settings().get("model"),
            "transcription": transcription_backend()}


# Live transcription state, read by Settings: backend in use, why listening is blocked (None = usable), switching flag.
STT = {"backend": None, "blocked": None, "switching": False}
LOCALE = "en-US"
MIC_TEST_SECONDS = 4


def stt_error(exc):
    """Short, audio-free error text for the log and the status line."""
    return f"{type(exc).__name__}: {str(exc)[:160]}"


def load_transcriber(backend, notify):
    """-> (transcribe(audio, prompt) -> (text, ms), blocked reason or None). Never falls back to another backend:
    an unusable Apple selection disables listening with its reason; typed commands and jevctl still work."""
    if backend == "apple":
        try:
            import speech_apple
        except ImportError:
            return None, "Apple dictation isn't installed in this build. Choose Local Whisper in Settings."
        state, reason = speech_apple.status(LOCALE)
        if state != "ready":
            return None, f"Apple dictation isn't ready: {reason} Open Settings, Transcription."
        emit(notify, "Starting", "Starting Apple dictation…")
        # a fresh request per utterance, so a changed wake phrase is hinted from the next one on
        return (lambda audio, prompt: speech_apple.AppleTranscriber(LOCALE, contextual_strings=WAKE.hints)
                .transcribe(audio, prompt)), None
    from faster_whisper import WhisperModel
    print("loading whisper...")
    emit(notify, "Starting", "Loading Whisper…")
    model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")

    def transcribe(audio, prompt):
        t = time.time()
        segs, _ = model.transcribe(audio, language="en", beam_size=1, vad_filter=True, initial_prompt=prompt)
        return " ".join(s.text.strip() for s in segs).strip(), int((time.time() - t) * 1000)
    return transcribe, None


def drain(q):
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return


def start_bridge(eng, notify):
    """Owning the socket is what makes this the one engine. Any failure stops startup: no second mic or engine."""
    from bridge import Bridge
    b = Bridge(eng)
    try:
        b.start()
    except Exception as exc:
        b.stop()
        eng.shutdown()
        raise RuntimeError(f"Hey Jev is already running, or its command socket is unavailable: {exc}") from exc
    print(f"command socket ready: {b.sock_path}")
    return b


def run_voice_assistant(notify=None, controls=None, mode="ptt", listening=True, ask=None, show=None):
    global ENGINE, BRIDGE
    ENGINE = make_engine(notify, ask, show)
    diagnostics.init(ENGINE.instance)
    diagnostics.record(None, "startup", "starting", **runtime_facts())
    try:
        bridge = BRIDGE = start_bridge(ENGINE, notify)  # raises before any microphone or voice dispatcher exists
    except Exception as exc:
        diagnostics.record(None, "startup", "failed", error=repr(exc))
        raise
    try:
        rec = Recorder()
        rec.enabled = False
        rec.stream.stop()
    except BaseException as exc:  # startup failed: leave no socket or engine behind
        diagnostics.record(None, "startup", "failed", error=repr(exc))
        bridge.stop()
        ENGINE.shutdown()
        ENGINE = BRIDGE = None
        raise
    floor = Floor(rec)
    hold = floor.hold
    armed_until = [0.0]
    want_listening = [listening]
    ptt_token = [None]  # the push-to-talk recording this key press owns
    stt_fn = [None]
    switch_lock = threading.Lock()

    def set_listening(on):
        """The mic runs only when the user wants it and the backend is usable."""
        on = on and not STT["blocked"]
        if on != rec.enabled:
            floor.drop_recording()
            armed_until[0] = 0
            rec.enabled = on
            (rec.stream.start if on else rec.stream.stop)()

    def switch(backend):
        """Load a backend and swap it in at a clean boundary. Old recordings, queued segments and in-flight
        transcriptions are invalidated by the epoch bump; nothing falls back to another backend."""
        with switch_lock:
            STT.update(switching=True, blocked="Switching transcription…")
            set_listening(False)
            floor.drop_recording()
            try:
                fn, blocked = load_transcriber(backend, notify)
            except Exception as exc:
                fn, blocked = None, f"Couldn't start {backend}: {stt_error(exc)}"
            stt_fn[0] = fn
            STT.update(backend=backend, blocked=blocked, switching=False)
            diagnostics.record(None, "transcription", "blocked" if blocked else "ready", backend=backend,
                               locale=LOCALE if backend == "apple" else None, reason=blocked)
            set_listening(want_listening[0])
            if blocked:
                emit(notify, "Dictation unavailable", blocked)
            elif want_listening[0]:
                emit(notify, "Ready", ready_text(rec.wake))

    def set_wake(phrase, aliases):
        """Apply a new wake phrase at a clean boundary: armed follow-ups, queued and in-flight audio are dropped."""
        global WAKE
        try:
            new = wake.Wake(phrase, aliases)
        except ValueError as exc:
            emit(notify, "Wake phrase not changed", str(exc))
            return
        WAKE = new
        floor.drop_recording()  # epoch bump: transcripts already in flight are discarded
        armed_until[0] = 0
        diagnostics.record(None, "wake_phrase", "set", phrase=new.phrase, aliases=len(new.aliases))
        if rec.enabled:
            emit(notify, "Ready", ready_text(rec.wake))

    def transcribe(audio, prompt):
        fn, backend, t = stt_fn[0], STT["backend"], time.time()
        if fn is None:
            raise RuntimeError(STT["blocked"] or "No transcription backend")
        try:
            return fn(audio, prompt)
        except Exception as exc:
            diagnostics.record(None, "transcribe", "failed", (time.time() - t) * 1000, backend=backend,
                               locale=LOCALE if backend == "apple" else None, error=stt_error(exc),
                               audio_s=round(len(audio) / SAMPLE_RATE, 2))
            raise

    def mic_test(reply, seconds=MIC_TEST_SECONDS):
        """Transcript only: record from the one capture owner, transcribe, report. Never reaches the engine."""
        if STT["blocked"]:
            return reply({"error": STT["blocked"]})
        if not rec.enabled:
            return reply({"error": "Listening is paused. Resume it, then test again."})
        token = floor.start_recording(isolated=True)  # isolation belongs to this token, not to the test function
        if token is None:
            return reply({"error": "Busy right now. Try again in a moment."})
        emit(notify, "Mic test", f"Say something… ({seconds} seconds)")
        time.sleep(seconds)
        audio = floor.stop_recording(token)  # None if a switch or mode change dropped it meanwhile
        if audio is None or not len(audio):
            emit(notify, "Ready", ready_text(rec.wake))
            return reply({"error": "The test was interrupted or nothing was recorded."})
        phrase = WAKE
        try:
            text, ms = transcribe(audio, phrase.prompt)
        except Exception as exc:
            return reply({"error": stt_error(exc)})
        finally:
            emit(notify, "Ready", ready_text(rec.wake))
        rest = phrase.match(text)
        diagnostics.record(None, "mic_test", "ok", ms, backend=STT["backend"], chars=len(text), wake_matched=rest is not None)
        reply({"text": text, "ms": ms, "backend": STT["backend"], "wake": phrase.phrase, "wake_matched": rest is not None,
               "command": rest})

    def run_turn(text, stt_ms, epoch):
        with hold():
            if not rec.enabled or epoch != rec.epoch:
                return
            try:
                print(f"  (stt {stt_ms}ms)")
                turn(ENGINE, text, notify, hold=hold, stt_ms=stt_ms)
            except Exception as exc:
                print(f"\n  turn failed: {exc}")
                emit(notify, "Something went wrong", str(exc))
                time.sleep(2)
                emit(notify, "Ready", ready_text(rec.wake))

    def ptt_turn(audio, epoch):
        emit(notify, "Transcribing", "Working out what you said…")
        try:
            text, ms = transcribe(audio, COMMAND_PROMPT)
        except Exception as exc:
            emit(notify, "Couldn't hear that", stt_error(exc))
            return
        run_turn(text, ms, epoch)

    def wake_loop():
        while True:
            try:
                audio = rec.segments.get(timeout=1)
            except queue.Empty:
                if armed_until[0] and time.time() > armed_until[0]:
                    armed_until[0] = 0
                    emit(notify, "Ready", ready_text(rec.wake))
                continue
            if not rec.enabled or not rec.wake or floor.locked():
                continue
            epoch = rec.epoch
            try:
                text, ms = transcribe(audio, WAKE.prompt)
            except Exception as exc:  # logged by transcribe(); show it, wake mode has no other feedback
                emit(notify, "Couldn't hear that", stt_error(exc))
                continue
            if not rec.enabled or not rec.wake or epoch != rec.epoch:
                continue
            rest = WAKE.match(text)
            if rest is not None:
                if rest:
                    armed_until[0] = 0
                    run_turn(rest, ms, epoch)
                else:
                    with hold():
                        say(say_line("wake"), notify)
                    armed_until[0] = time.time() + WAKE_WINDOW
                    emit(notify, "Listening", "Go ahead…")
            elif armed_until[0] and time.time() < armed_until[0]:
                armed_until[0] = 0
                run_turn(text, ms, epoch)
            elif text:
                print(f"\n  (not for me: {text!r})")

    def set_mode(new):
        floor.drop_recording()
        rec.wake = new == "wake"
        armed_until[0] = 0
        print(f"\n[mode: {'always listening' if rec.wake else 'hold right Option'}]")
        if not floor.locked():
            emit(notify, "Ready", ready_text(rec.wake))

    def start_recording():
        if STT["blocked"]:
            emit(notify, "Dictation unavailable", STT["blocked"])
            return
        if ptt_token[0] is not None and ptt_token[0] is not floor.owner:
            ptt_token[0] = None  # its recording was dropped by a mode or backend change
        if rec.enabled and not rec.wake and ptt_token[0] is None:
            ptt_token[0] = floor.start_recording()
            if ptt_token[0] is not None:
                print("\n[listening]", end="", flush=True)
                emit(notify, "Listening", "Release right Option when you’re done")

    def stop_recording():
        token, ptt_token[0] = ptt_token[0], None
        audio = floor.stop_recording(token)  # only this key press's own recording
        if audio is not None:
            if len(audio) > SAMPLE_RATE * 0.3:
                threading.Thread(target=ptt_turn, args=(audio, rec.epoch), daemon=True).start()

    def timer_done(t):
        with hold():
            emit(notify, "Time's up", t["label"] or "Timer finished")
            subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"])
            say(timer_done_line(t), notify)
        emit(notify, "Ready", ready_text(rec.wake))

    timers.start_loop(timer_done)
    threading.Thread(target=warm_cache, daemon=True).start()
    threading.Thread(target=wake_loop, daemon=True).start()
    set_mode(mode)
    global WAKE
    WAKE = wake.Wake(*wake_settings())
    switch(transcription_backend())
    diagnostics.record(None, "startup", "ready", mode=mode, listening=rec.enabled, log=diagnostics.path,
                       transcription=STT["backend"], blocked=STT["blocked"])
    print("ready. ctrl+c to quit.")
    if controls is not None:
        while True:
            command = controls.get()
            if isinstance(command, tuple) and command[0] == "mode":
                set_mode(command[1])
            elif isinstance(command, tuple) and command[0] == "transcription":
                threading.Thread(target=switch, args=(command[1],), daemon=True).start()
            elif isinstance(command, tuple) and command[0] == "wake_phrase":
                set_wake(command[1], command[2])
            elif isinstance(command, tuple) and command[0] == "mic_test":
                threading.Thread(target=mic_test, args=(command[1],), daemon=True).start()
            elif isinstance(command, tuple) and command[0] == "listening":
                want_listening[0] = bool(command[1])
                if STT["blocked"]:  # the selected backend is not usable: never claim to listen
                    emit(notify, "Dictation unavailable", STT["blocked"])
                    continue
                set_listening(want_listening[0])
                emit(notify, "Ready" if rec.enabled else "Paused",
                     ready_text(rec.wake) if rec.enabled else "Microphone paused. Current action may finish.")
            elif command == "press":
                start_recording()
            elif command == "release":
                stop_recording()
            elif command == "quit":
                if bridge:
                    bridge.stop()
                return

    if rec.wake:
        threading.Event().wait()

    from pynput import keyboard

    def on_press(key):
        if key == keyboard.Key.alt_r:
            start_recording()

    def on_release(key):
        if key == keyboard.Key.alt_r:
            stop_recording()

    with keyboard.Listener(on_press=on_press, on_release=on_release) as l:
        l.join()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", help="one turn on this transcript in a fresh process (not the running app; use jevctl for that)")
    ap.add_argument("--ui", action="store_true", help="show the native floating status window")
    ap.add_argument("--wake", action="store_true", help="always listening, say \"Hey Jev\" instead of holding Option")
    args = ap.parse_args()
    if args.ui:
        from assistant_ui import run_app
        run_app()
        return
    missing = missing_secrets()
    if missing:
        sys.exit("need " + ", ".join(missing) + " in Keychain or .env")
    if args.text:
        turn(make_engine(), args.text, None)
        return
    run_voice_assistant(mode="wake" if args.wake else "ptt")


if __name__ == "__main__":
    main()
