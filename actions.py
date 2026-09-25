"""Action registry: resolve a target, run the native effect, read the result back.

Every native call is a subprocess bounded by the step deadline (time.monotonic).
resolve(args) -> ("target", t) | ("choices", [...]) | ("none", reason)
run(target, deadline) -> None, may record prior state in target; raises Failed or Timeout
verify(target, deadline) -> ("done" | "wait" | "failed" | "unverified", facts), or None when nothing can be read
"""
import functools
import os
import re
import subprocess
import sys
import time

import app_catalog
import ax_walk
import screen
import timers
import url_adapter

LEVELS = {"silent": 0, "quiet": 25, "medium": 50, "loud": 75, "max": 100}


def level_value(a):
    """Exact percent when one was spoken, else the named level."""
    return a["percent"] if isinstance(a.get("percent"), int) else LEVELS.get(a.get("level"), 50)


class Failed(Exception):
    """The native call reported an error: the effect did not happen."""


class Timeout(Exception):
    """The deadline passed with the native call still out: outcome unknown."""


class Uncertain(Exception):
    """The effect was dispatched and then something went wrong: it may or may not have happened."""


def sh(cmd, deadline, effect=False):
    """effect=True marks the call that performs the action: an error there proves nothing, so it is Uncertain."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise Timeout(cmd[0])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=left)
    except subprocess.TimeoutExpired:  # subprocess.run kills and reaps the child
        raise Timeout(cmd[0])
    if r.returncode:
        msg = r.stderr.strip() or f"{cmd[0]} exited {r.returncode}"
        raise Uncertain(msg) if effect else Failed(msg)
    return r.stdout.strip()


def osa(deadline, *lines, argv=(), effect=False):
    """Fixed script lines; user values only ever travel as argv, never inside the script."""
    cmd = ["osascript"]
    for line in lines:
        cmd += ["-e", line]
    return sh(cmd + list(argv), deadline, effect)


def _check(value):
    return ("done", {"value": value})


# --------------------------------------------------------------------------- apps
def resolve_app(args):
    name = args.get("app") or ""
    got = app_catalog.resolve_app(name)
    if got[0] == "none":
        app_catalog.refresh()  # a freshly installed app
        got = app_catalog.resolve_app(name)
    return got


def running(bundle_id, deadline):
    """(bundle path, pid) of running processes with this bundle id, via lsappinfo (no Apple Events)."""
    asns = re.findall(r"ASN:0x[0-9a-f]+-0x[0-9a-f]+", sh(["lsappinfo", "find", f"bundleid={bundle_id}"], deadline))
    found = []
    for asn in asns:
        info = sh(["lsappinfo", "info", "-only", "bundlepath", "-only", "pid", asn], deadline)
        path, pid = re.search(r'bundle ?path"?="([^"]*)"', info, re.I), re.search(r"\bpid\s*=\s*(\d+)", info)
        if path:
            found.append((path[1], int(pid[1]) if pid else None))
    return found


def running_paths(bundle_id, deadline):
    return [p for p, _ in running(bundle_id, deadline)]


def last_opened(path, deadline):
    """Spotlight's last-used date for an app bundle, or None."""
    try:
        out = sh(["mdls", "-raw", "-name", "kMDItemLastUsedDate", path], deadline)
    except (Failed, Timeout):
        return None
    return None if not out or out == "(null)" else out[:10]


def app_hints(choices, deadline):
    """Facts Jev can use to break a tie: where each copy lives, whether it runs now, when it was last opened."""
    hints = []
    for c in choices:
        try:
            running_now = c.get("path") in running_paths(c["bundle_id"], deadline)
        except (Failed, Timeout):
            running_now = None
        hints.append({"name": c.get("name"), "folder": os.path.dirname(c.get("path") or ""),
                      "running": running_now, "last_opened": last_opened(c.get("path") or "", deadline)})
    return hints


def run_app_open(t, deadline):
    sh(["open", "-a", t["path"]] if t.get("path") else ["open", "-b", t["bundle_id"]], deadline, effect=True)


def verify_app_open(t, deadline):
    paths = running_paths(t["bundle_id"], deadline)
    facts = {"running_paths": paths}
    if t.get("path") and t["path"] in paths or not t.get("path") and paths:
        return ("done", facts)
    return ("wait", facts)


def helper_env():
    """Environment for fixed Python helpers. In the py2app bundle sys.executable is Contents/MacOS/python, a symlink
    that starts the base interpreter without the app's venv; pass the app's own sys.path so AppKit imports there too."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    return env


QUIT_HELPER = "\n".join([
    "import os, sys",
    "from AppKit import NSRunningApplication",
    "print('ready', flush=True)",
    "bid, path = sys.argv[1], os.path.realpath(sys.argv[2])",
    "for pid in map(int, sys.argv[3:]):",
    "    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)",
    "    if app is None or app.isTerminated():",
    "        print(pid, 'gone')",
    "    elif app.bundleIdentifier() != bid or not app.bundleURL() or os.path.realpath(app.bundleURL().path()) != path:",
    "        print(pid, 'mismatch')",
    "    else:",
    "        print(pid, 'sent' if app.terminate() else 'refused', flush=True)",
])


def run_app_quit(t, deadline):
    """Quit only the install that was resolved and confirmed. A fixed helper runs in a bounded subprocess and
    rechecks each pid's current bundle id and path right before terminate; a mismatch is never terminated."""
    pids = [pid for path, pid in running(t["bundle_id"], deadline) if path == t.get("path") and pid]
    if not pids:
        raise Failed(f"{t.get('name') or 'that app'} is not running from {t.get('path')}")
    left = deadline - time.monotonic()
    if left <= 0:
        raise Timeout("quit helper")
    try:
        r = subprocess.run([sys.executable, "-c", QUIT_HELPER, t["bundle_id"], t["path"], *map(str, pids)],
                           capture_output=True, text=True, timeout=left, env=helper_env())
    except subprocess.TimeoutExpired:
        raise Timeout("quit helper")
    lines = r.stdout.split()
    if not lines or lines[0] != "ready":  # helper never got to terminate: nothing was sent
        raise Failed(f"quit helper could not start: {r.stderr.strip()[-300:] or r.returncode}")
    if r.returncode:
        raise Uncertain(f"quit helper exited {r.returncode}: {r.stderr.strip()[-300:]}")
    results = dict(line.split() for line in r.stdout.splitlines()[1:] if line.strip())
    t["quit"] = results
    if "sent" not in results.values():
        raise Failed(f"nothing quit: {results}")


def verify_app_quit(t, deadline):
    paths = running_paths(t["bundle_id"], deadline)
    alive = t["path"] in paths if t.get("path") else bool(paths)
    return ("wait" if alive else "done", {"running_paths": paths})


# --------------------------------------------------------------------------- system volume
def get_volume(deadline):
    return int(osa(deadline, "output volume of (get volume settings)"))


def get_muted(deadline):
    return osa(deadline, "output muted of (get volume settings)") == "true"


def set_volume(t, deadline):
    osa(deadline, "on run argv", "set volume output volume (item 1 of argv as integer)", "end run", argv=[str(t["value"])],
        effect=True)


def volume_step(delta):
    def run(t, deadline):
        t["value"] = max(0, min(100, get_volume(deadline) + t.get("delta", delta)))
        set_volume(t, deadline)
    return run


def verify_volume(t, deadline):
    v = get_volume(deadline)
    return ("done" if abs(v - t["value"]) <= 1 else "wait", {"volume": v})


def run_mute(muted):
    def run(t, deadline):
        osa(deadline, f"set volume output muted {'true' if muted else 'false'}", effect=True)
    return run


def verify_mute(muted):
    def verify(t, deadline):
        m = get_muted(deadline)
        return ("done" if m == muted else "wait", {"muted": m})
    return verify


# --------------------------------------------------------------------------- Spotify
def spotify_running(deadline):
    return bool(running_paths("com.spotify.client", deadline))


def resolve_spotify(args):
    return ("target", {"app": "Spotify", **args})


def get_spotify_volume(deadline):
    return int(osa(deadline, 'tell application "Spotify" to get sound volume'))


def set_spotify_volume(t, deadline):
    osa(deadline, "on run argv", 'tell application "Spotify" to set sound volume to (item 1 of argv as integer)',
        "end run", argv=[str(t["value"])], effect=True)


def spotify_step(delta):
    def run(t, deadline):
        if not spotify_running(deadline):
            raise Failed("Spotify is not running")
        t["value"] = max(0, min(100, get_spotify_volume(deadline) + t.get("delta", delta)))
        set_spotify_volume(t, deadline)
    return run


def spotify_fixed(run_value):
    def run(t, deadline):
        if not spotify_running(deadline):
            raise Failed("Spotify is not running")
        t["value"] = run_value(t)
        set_spotify_volume(t, deadline)
    return run


def verify_spotify_volume(t, deadline):
    v = get_spotify_volume(deadline)
    return ("done" if abs(v - t["value"]) <= 1 else "wait", {"spotify_volume": v})


def spotify_state(deadline):
    return osa(deadline, 'tell application "Spotify" to player state')


def spotify_track(deadline):
    return osa(deadline, 'tell application "Spotify" to id of current track')


def run_play(t, deadline):
    if not spotify_running(deadline):
        sh(["open", "-b", "com.spotify.client"], deadline, effect=True)
    osa(deadline, 'tell application "Spotify" to play', effect=True)


def verify_play(t, deadline):
    s = spotify_state(deadline)
    if s == "playing":
        return ("done", {"player_state": s})
    osa(deadline, 'tell application "Spotify" to play')  # ignored while Spotify is still loading
    return ("wait", {"player_state": s})


def run_spotify_cmd(cmd, record_track=False):
    def run(t, deadline):
        if not spotify_running(deadline):
            raise Failed("Spotify is not running")
        if record_track:
            t["track"] = spotify_track(deadline)
        osa(deadline, f'tell application "Spotify" to {cmd}', effect=True)
    return run


def verify_paused(t, deadline):
    s = spotify_state(deadline)
    return ("done" if s == "paused" else "wait", {"player_state": s})


def verify_next(t, deadline):
    now = spotify_track(deadline)
    return ("done" if now != t["track"] else "wait", {"track": now})


# --------------------------------------------------------------------------- display
def get_dark(deadline):
    return osa(deadline, 'tell application "System Events" to tell appearance preferences to get dark mode') == "true"


def run_dark(value):
    def run(t, deadline):
        t["value"] = (not get_dark(deadline)) if value is None else value
        osa(deadline, 'tell application "System Events" to tell appearance preferences to set dark mode to '
            + ("true" if t["value"] else "false"), effect=True)
    return run


def verify_dark(t, deadline):
    d = get_dark(deadline)
    return ("done" if d == t["value"] else "wait", {"dark_mode": d})


# --------------------------------------------------------------------------- system
def run_lock(t, deadline):
    osa(deadline, 'tell application "System Events" to keystroke "q" using {control down, command down}', effect=True)


def run_sleep(t, deadline):
    sh(["pmset", "sleepnow"], deadline, effect=True)


# --------------------------------------------------------------------------- timers
def resolve_timer_set(args):
    secs = timers.parse_duration(args.get("text", ""))
    if not secs:
        return ("none", "no duration heard")
    return ("target", {"secs": secs, "label": timers.parse_reminder(args["text"]), "text": args["text"]})


def run_timer_set(t, deadline):
    t["timer"] = timers.add(t["secs"], t["label"], t["text"])


def verify_timer_set(t, deadline):
    with timers.LOCK:
        alive = any(x is t["timer"] for x in timers.TIMERS)
    return ("done" if alive else "failed", {"secs": t["secs"], "label": t["label"]})


def _timer_view(x):
    return {"id": x["id"], "secs": x["secs"], "label": x["label"]}


def resolve_timer_check(args):
    with timers.LOCK:
        if not timers.TIMERS:
            return ("none", "no timer running")
    return ("target", {})


def resolve_timer_cancel(args):
    """Pin the exact timers now, so the confirmation names them and the run cancels only those."""
    everything = bool(re.search(r"\ball\b", args.get("text", "").lower()))
    with timers.LOCK:
        if not timers.TIMERS:
            return ("none", "no timer running")
        pick = list(timers.TIMERS) if everything else [max(timers.TIMERS, key=lambda x: x["end"] - x["secs"])]
        return ("target", {"all": everything, "timers": [_timer_view(x) for x in pick]})


def run_timer_check(t, deadline):
    with timers.LOCK:
        if not timers.TIMERS:
            raise Failed("no timer running")
        t["left"] = timers.TIMERS[0]["end"] - time.time()


def verify_timer_check(t, deadline):
    return ("done", {"left": timers.say_duration(t["left"])})


def run_timer_cancel(t, deadline):
    ids = {x["id"] for x in t["timers"]}
    with timers.LOCK:
        gone = [x for x in timers.TIMERS if x["id"] in ids]
        if not gone:
            raise Failed("that timer already finished")
        for x in gone:
            timers.TIMERS.remove(x)
    t["cancelled"] = [x["id"] for x in gone]


def verify_timer_cancel(t, deadline):
    with timers.LOCK:
        left = [x for x in timers.TIMERS if x["id"] in t["cancelled"]]
    return ("done" if not left else "failed", {"cancelled": len(t["cancelled"])})


# --------------------------------------------------------------------------- websites
def _wall(deadline):
    """url_adapter keeps wall-clock deadlines; the engine keeps monotonic ones."""
    return time.time() + (deadline - time.monotonic())


def run_url(t, deadline):
    """Validation and the browser lookup happen here, before dispatch: their errors mean nothing opened.
    Any error from the adapter after that (no tab id, osascript error) is Uncertain: a tab may exist."""
    try:
        url_adapter.normalize_url(t.get("url", ""))
        explicit = t.get("browser")
        if explicit in url_adapter.SUPPORTED_BROWSERS:
            browser = explicit  # planner-set intent: skip the default-handler lookup
        else:
            browser = url_adapter.default_browser_for_url(t["url"], max(0.1, deadline - time.monotonic()),
                                                          _run=functools.partial(subprocess.run, env=helper_env()))
    except TimeoutError as exc:
        raise Failed(f"browser lookup timed out: {exc}")
    except (RuntimeError, ValueError) as exc:
        raise Failed(str(exc))
    try:
        url_adapter.run_url_open(t, _wall(deadline), _default_browser_fn=lambda _: browser)
    except TimeoutError as exc:
        raise Timeout(str(exc))
    except Exception as exc:
        raise Uncertain(str(exc))


def verify_url(t, deadline):
    try:
        return url_adapter.verify_url_open(t, _wall(deadline))
    except TimeoutError as exc:
        raise Timeout(str(exc))


def resolve_url(args):
    kind, got = url_adapter.resolve_url(args.get("url", ""))
    if kind != "target":
        return kind, got
    browser = args.get("browser")
    if browser is None:
        return kind, got
    if browser in url_adapter.SUPPORTED_BROWSERS:
        return ("target", {**got, "browser": browser})
    return ("none", "unsupported browser")


# --------------------------------------------------------------------------- screen
# Labels that always ask first, whatever the Click setting says. They add confirmation; their absence proves nothing.
RISKY = re.compile(r"\b(buy|purchase|order|pay|checkout|check out|send|submit|post|publish|delete|remove|erase|trash|"
                   r"empty|discard|uninstall|format|sign out|log out|transfer|confirm|accept|agree|install|share|reply all)\b",
                   re.I)
CHOOSE = None  # set by the app: (spoken, [labels]) -> (index or None, confidence). Only control names are sent.
CHOOSE_GATE = 0.65  # provisional, uncalibrated (jev skill); confirmation, not this number, authorizes risky presses
CHOOSE_MAX = 60  # controls per Jev call
INTENT_GATE = 0.75  # no control was named: Jev must be surer before a press happens (also provisional)
RESOLVE_BUDGET = 3.0  # one deadline over every native read a resolve makes
RESOLVE_UNTIL = None  # set by the engine while a task step resolves: never past the task's shared deadline


def resolve_deadline():
    d = time.monotonic() + RESOLVE_BUDGET
    return min(d, RESOLVE_UNTIL) if RESOLVE_UNTIL is not None else d
SETTLE = 1.5  # how long a press gets to show a relevant change
AX_GONE = (-25202, -25205, -25206, -25201)  # invalid element, no value, action unsupported, illegal argument
MENU_ROLES = ("AXMenuBarItem", "AXMenuButton", "AXPopUpButton")
_listed = {}  # id(target) -> the Snapshot run read, for verify
_pressed = {}  # id(target) -> (before, dispatched_at, ref)


def _where(item, frame):
    x, y, w, h = frame
    cx, cy = (item.frame[0] + item.frame[2] / 2 - x) / max(w, 1), (item.frame[1] + item.frame[3] / 2 - y) / max(h, 1)
    return ("top" if cy < 0.33 else "bottom" if cy > 0.66 else "middle") + "-" + (
        "left" if cx < 0.33 else "right" if cx > 0.66 else "centre")


def _screen_target(snap, item):
    """Exact identity: the process (pid + start time), the window element and the control element."""
    return {"pid": snap.pid, "started": snap.started, "app": snap.app, "window": snap.window_token,
            "element": item.token, "role": item.role, "label": item.label, "frame": [round(v) for v in item.frame],
            "confirm": bool(RISKY.search(item.label))}


def _choice(snap, item):
    """A spoken option ("name (where)") that keeps its exact target, so a later "the first one" presses exactly it."""
    where = _where(item, snap.window_frame)
    return {"name": f"{item.label} ({where})", "where": where, "target": _screen_target(snap, item)}


def resolve_pinned(t, deadline=None):
    """An answer to "which one?": the exact control offered then, only if it is still there, unchanged."""
    deadline = deadline or resolve_deadline()
    try:
        snap = _target_window(t, deadline)
    except (screen.Unavailable, screen.TimedOut, screen.Wedged) as exc:
        return ("none", f"can't read the screen: {exc}")
    item = next((i for i in snap.items if i.token == t["element"]), None)
    if ((snap.started, snap.window_token) != (t["started"], t["window"]) or item is None or not _live(item)
            or [round(v) for v in item.frame] != t["frame"] or (item.role, item.label) != (t["role"], t["label"])):
        return ("none", "that one isn't on screen anymore")
    hidden = _hidden(snap, item, deadline)
    if hidden:
        return ("none", hidden)
    return ("target", _screen_target(snap, item))


def _target_window(t, deadline):
    """The target's own window: the app's focused one, else (a background window) that exact window element."""
    snap = screen.observe(pid=t["pid"], ocr=False, deadline=deadline)
    if snap.window_token != t["window"]:
        win = screen.element_for(t["window"])
        if win is not None:
            snap = screen.observe(pid=t["pid"], ocr=False, deadline=deadline, window=win)
    return snap


VISIBLE = None  # set by the app: screen.still_visible. Unset (tests), visibility isn't re-checked


def _hidden(snap, item, deadline):
    """The control's window left the screen, or another window now covers it. -> reason or None."""
    if VISIBLE is None:
        return None
    try:
        return None if VISIBLE(snap.pid, snap.window_frame, item.frame, deadline) else \
            "that control is hidden behind another window now"
    except (screen.TimedOut, screen.Wedged, screen.Unavailable):
        return "can't check that control is still showing"


def _live(item):
    return item.source != "ocr" and item.pressable and item.enabled


def valid_choice(got, n):
    """The chooser's answer only when it has exactly the expected shape: (int index in range or None, finite 0-1)."""
    import math
    try:
        idx, conf = got
    except (TypeError, ValueError):
        return None
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not math.isfinite(conf) or not 0 <= conf <= 1:
        return None
    if idx is None:
        return (None, float(conf))
    if isinstance(idx, bool) or not isinstance(idx, int) or not 0 <= idx < n:
        return None
    return (idx, float(conf))


def _shown_list(args):
    """The numbered list a number refers to. Spoken: exactly the list displayed when the user started speaking
    (args["shown"] is its version), refused when the display changed mid-sentence, nothing was shown, or the version
    is too old. Typed (no "shown" at all): the current list. -> Snapshot, None, or "stale"."""
    if "shown" not in args:
        return screen.last()
    v = args["shown"]
    if not isinstance(v, int) or isinstance(v, bool):
        return "stale"
    return screen.shown(v) or "stale"


CARD_BELOW, CARD_ABOVE, CARD_SIDE, CARD_WORDS = 70, 20, 12, 3


def describe_cards(controls, context, frame):
    """One short description per control for Jev: its own name, the shareable words right around it (the channel
    under a video title, the price next to a product), and where it sits. Neighbours come only from what a task may
    share with Jev (AX labels, and OCR text the field scan and text regions vouch for), minus any label that came
    from an element's value: names, never contents."""
    import task as task_mod
    shareable, _ = task_mod.shareable_all(context)  # neighbours come from the whole page, not the task's first 60
    out = []
    for c in controls:
        x, y, w, h = c.frame
        top, bottom = y - CARD_ABOVE, y + h + CARD_BELOW
        for o in shareable:  # a card ends where the next like control starts: the row above or below is its own card
            ox, oy, ow, oh = o.frame
            if (o is c or o.role != c.role or min(w, ow) < 0.6 * max(w, ow)
                    or min(x + w, ox + ow) - max(x, ox) < 0.5 * min(w, ow)):
                continue
            if oy >= y + h / 2:  # the next item, however its title wraps: nothing from it is this card's context
                bottom = min(bottom, oy)
            elif oy + oh <= y + h / 2 and y - (oy + oh) <= CARD_BELOW:
                top = y  # an item just above: whatever sits between belongs to it, not to this card
        near = []
        for o in shareable:
            if o.label == c.label or o.from_value or o.secure:  # never a field's or document's value
                continue
            ox, oy = o.frame[0] + o.frame[2] / 2, o.frame[1] + o.frame[3] / 2
            if x - CARD_SIDE <= ox <= x + w + CARD_SIDE and top <= oy <= bottom:
                near.append((abs(oy - (y + h / 2)), o.label[:60]))
        words = [t for _, t in sorted(near)][:CARD_WORDS]
        out.append(f"“{c.label[:100]}”" + (f", near: {' · '.join(words)}" if words else "") + f", {_where(c, frame)}")
    return out


def resolve_screen_press(args):
    """A number from the last list the user saw, or a spoken control name, to one exact control the app declared."""
    if "pinned" in args:
        return resolve_pinned(args["pinned"])
    deadline = resolve_deadline()
    seen = None
    if args.get("number") is not None:
        shown = _shown_list(args)
        if shown == "stale":
            return ("none", "the numbers changed since you spoke; ask what you can click again")
        seen = next((i for i in shown.items if i.n == args["number"]), None) if shown is not None else None
        if seen is None:
            return ("none", "no such number on the last list")
        if seen.source == "ocr":
            return ("none", "not_a_control")
        if seen.home is not None:  # a desktop list: that exact control, re-read in its own window, wherever it is now
            return resolve_pinned(_screen_target(seen.home, seen), deadline)
    try:
        snap = screen.observe(ocr=False, deadline=deadline)
    except (screen.Unavailable, screen.TimedOut, screen.Wedged) as exc:
        return ("none", f"can't read the screen: {exc}")
    controls = [i for i in snap.items if _live(i)]
    if seen is not None:
        if (shown.pid, shown.started, shown.window_token) != (snap.pid, snap.started, snap.window_token):
            return ("none", "screen_changed")
        now = next((i for i in controls if i.token == seen.token and i.key() == seen.key()), None)
        return ("target", _screen_target(snap, now)) if now else ("none", "screen_changed")
    intent = args.get("intent")
    said = screen._norm(intent or args.get("label"))
    if not said:
        return ("none", "no control named")
    exact = [] if intent else [i for i in controls if screen._norm(i.label) == said]
    if not exact and not intent:
        exact = [i for i in controls if f" {said} " in f" {screen._norm(i.label)} "]
    if not exact and CHOOSE:
        named = [i for i in controls if not i.from_value]  # a field's or document's value is never sent
        if len(named) > CHOOSE_MAX:  # staged narrowing (jev skill): best of each batch, then a final pick
            finalists = []
            for k in range(0, len(named), CHOOSE_MAX):
                part = named[k:k + CHOOSE_MAX]
                cards = describe_cards(part, snap, snap.window_frame)
                try:
                    got = valid_choice(CHOOSE(intent, cards, intent=True) if intent
                                       else CHOOSE(args.get("label", ""), cards), len(cards))
                except Exception:
                    got = None
                if got is None:
                    return ("choices", [])  # a failed or malformed answer asks again; nothing is pressed
                if got[0] is not None:
                    finalists.append(part[got[0]])
            named = finalists[:CHOOSE_MAX]
        if named:
            try:
                context = screen.observe(pid=snap.pid, ocr=True, deadline=deadline)  # the words around each control
            except (screen.Unavailable, screen.TimedOut, screen.Wedged):
                context = snap
            cards = describe_cards(named, context, snap.window_frame)
            try:
                got = valid_choice(CHOOSE(intent, cards, intent=True) if intent
                                   else CHOOSE(args.get("label", ""), cards), len(cards))
            except Exception:
                got = None
            if got is None:
                return ("choices", [])  # a malformed answer asks again; nothing is pressed
            if got[0] is not None and got[1] >= (INTENT_GATE if intent else CHOOSE_GATE):
                exact = [named[got[0]]]
    if not exact:
        return _elsewhere(args, snap, deadline) or \
            ("none", "nothing on screen does that" if intent else "no control by that name")
    if len({i.token for i in exact}) > 1:
        return ("choices", [_choice(snap, i) for i in exact[:4]])
    return ("target", _screen_target(snap, exact[0]))


DESKTOP = None  # set by the app: screen.observe_desktop. Unset (tests), only the front window is searched


def _elsewhere(args, front, deadline):
    """Nothing in the front window fits: look in every other visible window (front to back, hidden controls
    removed). One exact name -> that control; several -> ask, naming each app; none -> Jev's chooser over the
    cards, each saying which app it's in. -> a resolve result, or None when nothing else fits either."""
    if DESKTOP is None:
        return None
    try:
        snaps, skipped = DESKTOP(deadline)
    except (screen.Unavailable, screen.TimedOut, screen.Wedged):
        return None
    others = [s for s in snaps if (s.pid, s.window_token) != (front.pid, front.window_token)]
    partial = skipped > 0 or any(s.truncated for s in others)  # an unread window could hold a second match
    pairs = [(s, i) for s in others for i in s.items if _live(i) and not i.from_value]
    if not pairs:
        return ("none", "I couldn't read every window; bring that app to the front") if partial else None
    intent = args.get("intent")
    said = screen._norm(intent or args.get("label"))
    exact = [] if intent else [(s, i) for s, i in pairs if screen._norm(i.label) == said] or \
        [(s, i) for s, i in pairs if f" {said} " in f" {screen._norm(i.label)} "]
    if not exact and CHOOSE:
        cards = []
        for s in others:
            mine = [i for t, i in pairs if t is s]
            cards += [f"{c}, in {s.app}" for c in describe_cards(mine, s, s.window_frame)]
        best = None
        for k in range(0, len(pairs), CHOOSE_MAX):  # every card judged, a batch at a time, the surest pick wins
            part = cards[k:k + CHOOSE_MAX]
            try:
                got = valid_choice(CHOOSE(intent, part, intent=True) if intent
                                   else CHOOSE(args.get("label", ""), part), len(part))
            except Exception:
                got = None
            if got is None:
                return ("choices", [])
            if got[0] is not None and got[1] >= (INTENT_GATE if intent else CHOOSE_GATE):
                if best is not None:  # two windows each have a sure fit: ask rather than guess
                    return ("choices", [_choice_in(*pairs[best[0]]), _choice_in(*pairs[k + got[0]])])
                best = (k + got[0], got[1])
        exact = [pairs[best[0]]] if best else []
    if not exact:
        return ("none", "I couldn't read every window; bring that app to the front") if partial else None
    if len(exact) > 1 or partial:  # never a claimed unique match when some windows went unread: ask
        return ("choices", [_choice_in(s, i) for s, i in exact[:4]])
    return ("target", _screen_target(*exact[0]))


def _choice_in(snap, item):
    c = _choice(snap, item)
    c["name"] = f"{item.label} in {snap.app} ({_where(item, snap.window_frame)})"
    return c


def _find(t, deadline):
    """The target's own element, still in the same window of the same process, still enabled and pressable."""
    ref = screen.element_for(t.get("element"))
    if ref is None:
        raise Failed("unknown control")
    try:
        snap = _target_window(t, deadline)
    except (screen.Unavailable, screen.TimedOut, screen.Wedged) as exc:
        raise Failed(f"can't read the screen: {exc}")
    item = next((i for i in snap.items if i.token == t["element"]), None)
    if ((snap.started, snap.window_token) != (t["started"], t["window"]) or item is None or not _live(item)
            or [round(v) for v in item.frame] != t["frame"] or (item.role, item.label) != (t["role"], t["label"])):
        raise Failed("that control is no longer there")
    hidden = _hidden(snap, item, deadline)
    if hidden:
        raise Failed(hidden)
    return ref


_raised = set()  # id(target): its window was brought to the front for the press (reported, not hidden)


def _took_raise(t):
    if id(t) in _raised:
        _raised.discard(id(t))
        return True
    return False


def run_screen_press(t, deadline):
    try:
        _run_press(t, deadline)
    except Exception:
        _raised.discard(id(t))  # no verify will follow to collect it; the failure message carries the raise
        raise


def _run_press(t, deadline):
    ref = _find(t, deadline)
    try:
        before = (screen.signature(t["pid"], deadline), screen.element_state(ref, deadline))
    except (screen.TimedOut, screen.Wedged) as exc:
        raise Failed(f"screen read failed before the press: {exc}")
    try:
        web = screen.chromium_page(t["pid"], ref, deadline)
    except (screen.TimedOut, screen.Wedged) as exc:
        raise Failed(f"screen read failed before the press: {exc}")
    raised = ""  # once a raise was attempted, every report says so: the press failed, but the window moved
    _raised.discard(id(t))
    if web:  # keys go to the focused window only: a page in a window behind is brought to the front first
        win = screen.element_for(t["window"])
        try:
            if win is None:
                raise Failed("that window is gone, so nothing was pressed")
            if not screen.in_front(t["pid"], win, deadline):
                raised = f" (I brought {t.get('app') or 'that app'} to the front)"
                _raised.add(id(t))
                if not screen.bring_forward(t["pid"], win, deadline):
                    raise Failed("that window wouldn't come to the front, so nothing was pressed" + raised)
        except (screen.TimedOut, screen.Wedged) as exc:
            raise Failed(f"couldn't bring that window forward, so nothing was pressed{raised} ({exc})")
    _pressed[id(t)] = (before, time.monotonic(), ref)
    try:
        err = screen.page_key(t["pid"], ref, t["role"], deadline) if web else screen.press(ref, deadline)
    except screen.Wedged as exc:
        raise Failed(str(exc) + raised)  # refused before sending: nothing was pressed
    except screen.TimedOut as exc:
        raise Uncertain(f"the app did not answer the press in time ({exc}){raised}")
    if err == screen.NOT_FOCUSED:
        raise Failed("the page wouldn't focus that control, so nothing was pressed" + raised)
    if err == screen.TOO_LATE:
        raise Failed("the page took too long to focus that control, so nothing was pressed" + raised)
    if err in AX_GONE:
        raise Failed(f"the app refused the press (AX error {err})")
    if err != 0:
        raise Uncertain(f"AX error {err} after the press was sent")


def verify_screen_press(t, deadline):
    """done only on a change of the pressed control itself: its value, selection or expansion, it going away, or a
    menu opening from a menu control. Anything else that changed is recorded, and the press stays unverified."""
    before, at, ref = _pressed.get(id(t), (None, 0, None))
    if before is None:
        return ("unverified", {"why": "no before-state"})
    after = (screen.signature(t["pid"], deadline), screen.element_state(ref, deadline))
    known = [k for k in before[1] if screen.UNKNOWN not in (before[1][k], after[1][k])]
    own = sorted(k for k in known if before[1][k] != after[1][k])  # a failed read proves nothing
    if t["role"] in MENU_ROLES and after[0].get("menu_open") and not before[0].get("menu_open"):
        own.append("menu_open")
    if own:
        _pressed.pop(id(t), None)
        return ("done", {"changed": own, "window_changed": before[0].get("window") != after[0].get("window"),
                         "brought_forward": _took_raise(t)})
    if time.monotonic() - at < SETTLE:
        return ("wait", {})
    _pressed.pop(id(t), None)
    other = sorted(k for k in before[0] if before[0].get(k) != after[0].get(k))
    unread = sorted(k for k in before[1] if k not in known)
    return ("unverified", {"delivered": True, "observed": other, "unread": unread, "brought_forward": _took_raise(t),
                           "why": "pressed; the control itself did not change" if not unread
                           else "pressed; the control could not be read back"})


EDITABLE_ROLES = ("AXTextField", "AXTextArea", "AXSearchField", "AXComboBox")
_typed = {}  # id(target) -> (value before, dispatched_at, ref)


def _field_target(snap, ref, facts, label):
    return {"pid": snap.pid, "started": snap.started, "app": snap.app, "window": facts["window"],
            "element": screen.token(ref), "role": facts["role"], "label": label, "frame": facts["frame"]}


def resolve_screen_type(args):
    """The named field, or the focused one, as an exact element: editable, enabled, not a password field, and
    accepting inserted text. The text itself is the user's own words, taken literally."""
    text = args.get("text")
    if not isinstance(text, str) or not text or len(text) > 2000:
        return ("none", "no text to type")
    deadline = resolve_deadline()
    try:
        snap = screen.observe(ocr=False, deadline=deadline)
        if args.get("number") is not None:  # a field from the list the user (or a task) saw
            shown = _shown_list(args)
            if shown == "stale":
                return ("none", "the numbers changed since you spoke; ask what you can click again")
            seen = next((i for i in shown.items if i.n == args["number"]), None) if shown is not None else None
            if seen is None:
                return ("none", "no such number on the last list")
            if seen.home is not None and screen.scope_of(seen, shown) != (snap.pid, snap.started, snap.window_token):
                return ("none", "that field is in another window; bring it to the front first")
            if seen.home is None and (shown.pid, shown.started, shown.window_token) != (snap.pid, snap.started,
                                                                                         snap.window_token):
                return ("none", "screen_changed")
            now = next((i for i in snap.items if i.token == seen.token and i.key() == seen.key()), None)
            if now is None or now.role not in EDITABLE_ROLES:
                return ("none", "not_a_text_field")
            ref, label = now.ref, now.label
        elif args.get("field"):
            said = screen._norm(args["field"])
            fields = [i for i in snap.items if i.role in EDITABLE_ROLES and i.source != "ocr" and not i.from_value]
            hits = [i for i in fields if screen._norm(i.label) == said] or \
                   [i for i in fields if f" {said} " in f" {screen._norm(i.label)} "]
            if len({i.token for i in hits}) > 1:
                return ("choices", [{"name": f"{i.label} ({_where(i, snap.window_frame)})"} for i in hits[:4]])
            if not hits:
                return ("none", "no field by that name")
            ref, label = hits[0].ref, hits[0].label
        else:
            ref, label = screen.focused_field(snap.pid, deadline), ""
            if ref is None:
                return ("none", "no field is selected")
        facts = screen.field_facts(ref, deadline)
    except (screen.Unavailable, screen.TimedOut, screen.Wedged) as exc:
        return ("none", f"can't read the screen: {exc}")
    if facts["secure"]:
        return ("none", "password_field")  # never typed into, whatever was asked
    if facts["role"] not in EDITABLE_ROLES or not facts["enabled"] or not facts["insertable"]:
        return ("none", "not_a_text_field")
    if facts["window"] != snap.window_token:
        return ("none", "field is in another window")
    return ("target", {**_field_target(snap, ref, facts, label), "text": text})


def run_screen_type(t, deadline):
    ref = screen.element_for(t.get("element"))
    if ref is None:
        raise Failed("unknown field")
    try:
        if screen.process_start(t["pid"], deadline) != t["started"]:
            raise Failed("the app restarted")
        facts = screen.field_facts(ref, deadline)
        if (facts["secure"] or not facts["enabled"] or not facts["insertable"] or facts["role"] != t["role"]
                or facts["frame"] != t["frame"] or facts["window"] != t["window"]):
            raise Failed("that field is no longer there")
    except (screen.Unavailable, screen.TimedOut, screen.Wedged) as exc:
        raise Failed(f"screen read failed before typing: {exc}")
    try:  # focusing is itself an effect: once sent, a timeout means it may still land
        focused = screen.focus(ref, deadline)
    except screen.Wedged as exc:
        raise Failed(str(exc))  # refused before sending
    except screen.TimedOut as exc:
        raise Uncertain(f"the app did not answer the focus in time ({exc}); nothing was typed")
    if focused in AX_GONE:
        raise Failed(f"the field refused focus (AX error {focused})")  # gone or unsupported: definitely not focused
    if focused != 0:
        raise Uncertain(f"AX error {focused} after focusing; nothing was typed")  # e.g. -25204: may have landed
    try:
        before = screen.field_value(ref, deadline)
        rng = screen.selected_range(ref, deadline) if isinstance(before, str) else None
    except (screen.Unavailable, screen.TimedOut, screen.Wedged) as exc:
        raise Failed(f"screen read failed before typing: {exc}")
    if not isinstance(before, str):
        raise Failed("can't read the field, so typing couldn't be checked")
    expected = screen.expected_after(before, rng, t["text"]) if rng is not None else None
    _typed[id(t)] = ((before, expected), time.monotonic(), ref)
    try:
        err = screen.insert_text(ref, t["text"], deadline)
    except screen.Wedged as exc:
        raise Failed(str(exc))
    except screen.TimedOut as exc:
        raise Uncertain(f"the app did not answer in time ({exc})")
    if err in AX_GONE:
        raise Failed(f"the field refused the text (AX error {err})")
    if err != 0:
        raise Uncertain(f"AX error {err} after the text was sent")


def verify_screen_type(t, deadline):
    """done only when the field now equals exactly what the insert should produce: the old value with the selection
    read before dispatch (UTF-16 range, as AX counts) replaced by the typed text. Without a readable selection there
    is no exact expectation, so the result can only be unverified. Values are compared here and dropped."""
    got, at, ref = _typed.get(id(t), (None, 0, None))
    if got is None:
        return ("unverified", {"why": "no before-state"})
    before, expected = got
    after = screen.field_value(ref, deadline)
    if expected is not None and after == expected:
        _typed.pop(id(t), None)
        return ("done", {"typed": len(t["text"])})
    if time.monotonic() - at < SETTLE:
        return ("wait", {})
    _typed.pop(id(t), None)
    why = ("the selection couldn't be read, so the result can't be checked exactly" if expected is None
           else "the field doesn't show exactly the typed text" if isinstance(after, str)
           else "the field could not be read back")
    return ("unverified", {"delivered": True, "why": why})


def resolve_screen_submit(args):
    """The app's focused element, when it accepts AXConfirm: Return, sent to that element."""
    deadline = resolve_deadline()
    try:
        snap = screen.observe(ocr=False, deadline=deadline)
        ref = screen.focused_field(snap.pid, deadline)
        if ref is None:
            return ("none", "no field is selected")
        if args.get("element") and screen.token(ref) != args["element"]:
            return ("none", "a different field is selected now")  # a task decided on one field: only that one
        facts = screen.field_facts(ref, deadline)
        if not screen.can_confirm(ref, deadline):
            return ("none", "nothing here to submit")
    except (screen.Unavailable, screen.TimedOut, screen.Wedged) as exc:
        return ("none", f"can't read the screen: {exc}")
    if facts["window"] != snap.window_token:
        return ("none", "field is in another window")
    return ("target", {**_field_target(snap, ref, facts, ""), "confirm_word": "submit"})


def run_screen_submit(t, deadline):
    ref = screen.element_for(t.get("element"))
    if ref is None:
        raise Failed("unknown field")
    try:
        if screen.process_start(t["pid"], deadline) != t["started"]:
            raise Failed("the app restarted")
        facts = screen.field_facts(ref, deadline)
        if (facts["role"] != t["role"] or facts["frame"] != t["frame"] or facts["window"] != t["window"]
                or not screen.can_confirm(ref, deadline)):
            raise Failed("that field is no longer there")
        before = (screen.field_value(ref, deadline), screen.focused_field(t["pid"], deadline),
                  screen.element_state(ref, deadline)["exists"])
    except (screen.Unavailable, screen.TimedOut, screen.Wedged) as exc:
        raise Failed(f"screen read failed before submitting: {exc}")
    _typed[id(t)] = (before, time.monotonic(), ref)
    try:
        err = screen.confirm(ref, deadline)
    except screen.Wedged as exc:
        raise Failed(str(exc))
    except screen.TimedOut as exc:
        raise Uncertain(f"the app did not answer in time ({exc})")
    if err in AX_GONE:
        raise Failed(f"the field refused (AX error {err})")
    if err != 0:
        raise Uncertain(f"AX error {err} after submitting")


def verify_screen_submit(t, deadline):
    """done only on a change of that field: it went away, focus left it, or its text changed (a field that clears
    after sending). A change elsewhere proves nothing."""
    before, at, ref = _typed.get(id(t), (None, 0, None))
    if before is None:
        return ("unverified", {"why": "no before-state"})
    value, focus, exists = before
    now_exists = screen.element_state(ref, deadline)["exists"]
    changed, observed = [], []
    if exists == "yes" and now_exists == "no":
        changed.append("field_gone")
    elif now_exists == "yes":
        now_value = screen.field_value(ref, deadline)
        if isinstance(value, str) and isinstance(now_value, str) and now_value != value:
            changed.append("field_text_changed")
        now_focus = screen.focused_field(t["pid"], deadline)
        if focus is not None and now_focus is not None and now_focus != focus:
            observed.append("focus_moved")  # a click elsewhere does this too: recorded, never proof
    if changed:
        _typed.pop(id(t), None)
        return ("done", {"changed": changed, "means": "the field changed after Return; not that anything was accepted"})
    if time.monotonic() - at < SETTLE:
        return ("wait", {})
    _typed.pop(id(t), None)
    return ("unverified", {"delivered": True, "observed": observed, "why": "Return was sent; the field itself did not change"})


_scrolled = {}  # id(target) -> position before


def resolve_screen_scroll(args):
    """The frontmost window's main scrollable view. A named app ("in Chrome") must be the one in front."""
    deadline = resolve_deadline()
    try:
        snap = screen.observe(ocr=False, deadline=deadline)
        wanted = (args.get("app") or "").strip().lower()
        if wanted and wanted not in snap.app.lower() and wanted not in {"the browser", "browser", "this page", "the page",
                                                                         "page", "here", "this", "it"}:
            return ("none", "that app isn't in front")
        kind, el = screen.bounded(lambda: screen.scroll_target(snap.window_ref), deadline)
    except (screen.Unavailable, screen.TimedOut, screen.Wedged) as exc:
        return ("none", f"can't read the screen: {exc}")
    if kind is None:
        return ("none", "nothing here scrolls")
    return ("target", {"pid": snap.pid, "started": snap.started, "app": snap.app, "window": snap.window_token,
                       "element": screen.token(el), "how": kind, "direction": args.get("direction", "down"),
                       "amount": args.get("amount", "normal")})


def _same_front(t, deadline):
    """The target's app is still in front, the same process, with the same window: checked right before an effect,
    after any wait or confirmation, Automatic included."""
    if screen.frontmost()[0] != t["pid"] or screen.process_start(t["pid"], deadline) != t["started"]:
        return "another app is in front now"
    if screen.current_window(t["pid"], deadline) != t["window"]:
        return "the window changed"
    return None


def run_screen_scroll(t, deadline):
    ref = screen.element_for(t.get("element"))
    if ref is None:
        raise Failed("unknown view")

    def guard():
        """Right before the write: same app in front, same process and window, and this view still in it."""
        why = _same_front(t, deadline)
        if why:
            return why
        if not screen.in_window(ref, t["window"]):
            return "that view isn't in the window any more"
        return None
    try:
        before = screen.bounded(lambda: screen.scroll_position(t["how"], ref), deadline) if t["how"] == "bar" else None
    except (screen.Unavailable, screen.TimedOut, screen.Wedged) as exc:
        raise Failed(f"screen read failed before scrolling: {exc}")
    if t["how"] == "bar" and before is None:
        raise Failed("can't read the scroll position")
    try:
        err = screen.scroll(t["how"], ref, t["direction"], t["amount"], deadline, guard=guard)
    except screen.Unavailable as exc:
        raise Failed(str(exc))  # every check happens before anything is written
    except screen.Wedged as exc:
        raise Failed(str(exc))
    except screen.TimedOut as exc:
        raise Uncertain(f"the app did not answer in time ({exc})")
    if err == screen.AX_NO_VALUE:
        raise Failed("already at the " + ("bottom" if t["direction"] == "down" else "top") if t["how"] == "bar"
                     else "nothing further to scroll to that I can see")
    if err in AX_GONE:
        raise Failed(f"the view refused to scroll (AX error {err})")
    if err != 0:
        raise Uncertain(f"AX error {err} after scrolling")
    _scrolled[id(t)] = (before, time.monotonic(), ref)


def verify_screen_scroll(t, deadline):
    """Native: done only when the bar's valid 0-1 value changed. Web areas expose no scroll position, so a web
    scroll is reported as sent and never as checked: layout moving is not proof of scrolling."""
    before, at, ref = _scrolled.get(id(t), (None, 0, None))
    if t["how"] != "bar":
        _scrolled.pop(id(t), None)
        return ("unverified", {"delivered": True, "why": "this page doesn't report its scroll position"})
    after = screen.bounded(lambda: screen.scroll_position("bar", ref), deadline)
    if before is not None and after is not None and after != before:
        _scrolled.pop(id(t), None)
        return ("done", {"moved": True})
    if time.monotonic() - at < SETTLE:
        return ("wait", {})
    _scrolled.pop(id(t), None)
    return ("unverified", {"delivered": True, "why": "couldn't confirm the view moved"})


_clicked = {}  # id(target) -> (signature before, dispatched_at)


def resolve_pointer_click(args):
    """Wherever the pointer is: the ordinary window on top under it, its process, and the exact point."""
    try:
        point = screen.pointer()
        pid, wid = screen.app_at(point)
        if pid is None:
            return ("none", "something is covering that spot")
        app, _ = screen._app_info(pid)
        started = screen.process_start(pid, time.monotonic() + 1)
    except Exception as exc:
        return ("none", f"can't read the pointer: {exc}")
    return ("target", {"pid": pid, "started": started, "app": app, "window_number": wid,
                       "point": [round(point[0]), round(point[1])], "button": args.get("button", "left"),
                       "double": bool(args.get("double")), "confirm": False})


def run_pointer_click(t, deadline):
    point = screen.pointer()
    if [round(point[0]), round(point[1])] != t["point"] or screen.app_at(point) != (t["pid"], t["window_number"]):
        raise Failed("the pointer moved")  # after a confirmation, never click somewhere the user didn't approve
    try:
        if screen.process_start(t["pid"], deadline) != t["started"]:
            raise Failed("the app restarted")
        before = screen.signature(t["pid"], deadline)
    except (screen.TimedOut, screen.Wedged, screen.Unavailable) as exc:
        raise Failed(f"screen read failed before clicking: {exc}")
    _clicked[id(t)] = (before, time.monotonic())
    try:
        screen.click_at(tuple(t["point"]), t["button"], t["double"], deadline,
                        expect=(t["pid"], t["window_number"]))
    except screen.Moved as exc:
        if exc.args and exc.args[0]:
            raise Uncertain("the pointer moved partway through the click")
        raise Failed("the pointer moved")
    except screen.Wedged as exc:
        raise Failed(str(exc))
    except screen.TimedOut as exc:
        raise Uncertain(f"the click didn't finish in time ({exc})")


def verify_pointer_click(t, deadline):
    """A click at an arbitrary spot has no postcondition that can be checked: it is always reported as delivered,
    with whatever changed in that window recorded, and never as done."""
    before, at = _clicked.get(id(t), (None, 0))
    if time.monotonic() - at < SETTLE:
        return ("wait", {})
    _clicked.pop(id(t), None)
    try:
        after = screen.signature(t["pid"], deadline)
        observed = sorted(k for k in (before or {}) if before.get(k) != after.get(k))
    except Exception:
        observed = []
    return ("unverified", {"delivered": True, "observed": observed,
                           "why": "clicked; a click at the pointer has nothing specific to check"})


CLASSIFY_ITEMS = None  # set by the app: (noun, labels) -> [(is_one, confidence)] per label, from one Jev call
ROLE_NOUNS = {"button": ("AXButton", "AXMenuButton", "AXPopUpButton"), "link": ("AXLink",),
              "tab": ("AXTab", "AXRadioButton"), "row": ("AXRow", "AXCell"), "item": None}
PICK_GATE = 0.65  # provisional, uncalibrated (jev skill): measure on labeled pages before trusting
PICK_MAX = 60  # items per Jev call
def reading_order(items):
    """Rows top to bottom, then left to right: how a person counts "the third video" on a grid or a list.
    Rows come from the items themselves, not fixed screen bands: an item joins the current row when its centre
    lies within the vertical span of the row's first item, so aligned controls stay together wherever the window is."""
    rows = []
    for i in sorted(items, key=lambda i: (i.frame[1], i.frame[0])):
        cy = i.frame[1] + i.frame[3] / 2
        if rows and rows[-1][0] <= cy <= rows[-1][1]:
            rows[-1][2].append(i)
        else:
            rows.append((i.frame[1], i.frame[1] + i.frame[3], [i]))
    return [i for _, _, row in rows for i in sorted(row, key=lambda i: i.frame[0] + i.frame[2] / 2)]


def nearest_to(items, where, frame):
    """The item closest to a named place in the window: "bottom-right", "top", "middle-left"…"""
    v, _, h = where.partition("-")
    ty = {"top": 0.0, "middle": 0.5, "bottom": 1.0}[v]
    tx = {"left": 0.0, "right": 1.0}.get(h)
    x, y, w, hh = frame

    def dist(i):
        cx = (i.frame[0] + i.frame[2] / 2 - x) / max(w, 1)
        cy = (i.frame[1] + i.frame[3] / 2 - y) / max(hh, 1)
        return (cy - ty) ** 2 + ((cx - tx) ** 2 if tx is not None else 0)
    return min(items, key=dist)


def _valid_flag(g):
    """(bool, finite 0-1 score), exactly."""
    import math
    return (isinstance(g, (tuple, list)) and len(g) == 2 and isinstance(g[0], bool)
            and isinstance(g[1], (int, float)) and not isinstance(g[1], bool) and math.isfinite(g[1]) and 0 <= g[1] <= 1)


WHOLE_WALK = 2.5  # seconds a second, longer walk may take when the first was cut short


def _squash(text):
    return re.sub(r"[\W_]+", "", (text or "").lower())


def _says(text, heard):
    """True when the heard words run whole-word through the text, spacing and case aside: "network Chuck" is in
    "NetworkChuck · 2 days ago", "apple" is not in "Pineapple farming"."""
    want = _squash(heard)
    if len(want) < 4:
        return False
    words = re.findall(r"[^\W_]+", (text or "").lower())
    for start in range(len(words)):
        run = ""
        for w in words[start:]:
            run += w
            if run == want:
                return True
            if not want.startswith(run):
                break
    return False


def _judge_kind(noun, items, labels):
    """Whether each item is a `noun` at all ("video", "result"): role nouns by role, the rest by Jev, batched.
    None when Jev's answer failed or was malformed."""
    roles = ROLE_NOUNS.get(noun, "jev")
    if roles is None:
        return [(True, 1.0)] * len(items)
    if roles != "jev":
        return [(True, 1.0) if i.role in roles else (False, 1.0) for i in items]
    if not CLASSIFY_ITEMS:
        return None
    got = []
    for k in range(0, len(labels), PICK_MAX):
        part = labels[k:k + PICK_MAX]
        try:
            ans = CLASSIFY_ITEMS(noun, part)
        except Exception:
            ans = None
        if not isinstance(ans, list) or len(ans) != len(part) or not all(_valid_flag(g) for g in ans):
            return None
        got += ans
    return got


def observe_whole(deadline):
    """The front window, read again with a longer walk when the first read was cut short. Chrome builds its page
    tree on the first read after a while unused, so a cold YouTube page runs out the usual walk time; the second read
    is warm and usually complete (2026-09-25: "the first video" refused on a cold read)."""
    snap = screen.observe(ocr=False, deadline=deadline)
    if snap.truncated:
        left = deadline - time.monotonic() - 0.3
        if left > ax_walk.AX_TIME_CAP:
            snap = screen.observe(pid=snap.pid, ocr=False, deadline=deadline, walk_cap=min(WHOLE_WALK, left))
    return snap


def resolve_screen_pick(args):
    """"the third video", "the last result", "the video in the bottom-right". Which on-screen controls are videos
    (results, songs…) is Jev's call, one batched question over the pressable controls' names; counting and place
    are the code's. The pick becomes an ordinary exact-element press target."""
    if "pinned" in args:
        return resolve_pinned(args["pinned"])
    deadline = resolve_deadline()
    try:
        snap = observe_whole(deadline)
    except (screen.Unavailable, screen.TimedOut, screen.Wedged) as exc:
        return ("none", f"can't read the screen: {exc}")
    wanted = (args.get("app") or "").strip().lower()
    if wanted and wanted not in snap.app.lower() and wanted not in {"the browser", "browser", "this page", "the page",
                                                                     "page", "here", "this"}:
        return ("none", "that app isn't in front")
    noun = args.get("noun", "item")
    if snap.truncated:  # first/last/nearest/only all need every candidate; a cut list can't say which is which
        return ("none", "there's more on screen than I can read at once; scroll or name it")
    pool = [i for i in snap.items if _live(i) and not i.from_value]
    bare = noun
    roles = ROLE_NOUNS.get(noun, "jev") if not args.get("kind") else "jev"  # a qualified noun is Jev's to judge
    if ROLE_NOUNS.get(noun, "jev") == "jev" or noun in ("link", "row"):  # the bare noun decides; content: "the first video" counts the page, never the
        try:  # browser's tabs named after videos; "the first tab" or "button" still means the whole window
            page = screen.page_frame(snap.window_ref, deadline) if getattr(snap, "window_ref", None) is not None else None
        except (screen.TimedOut, screen.Wedged):
            page = None
        if page:
            pool = [i for i in pool if page[0] <= i.frame[0] + i.frame[2] / 2 <= page[0] + page[2]
                    and page[1] <= i.frame[1] + i.frame[3] / 2 <= page[1] + page[3]]
    unsure, flags = [], {}
    noun = f"{args['kind']} {noun}" if args.get("kind") else noun
    if roles is None:
        group = pool
    elif roles != "jev":
        group = [i for i in pool if i.role in roles]
    else:
        if not CLASSIFY_ITEMS or not pool:
            return ("none", f"no {noun}s on screen")
        # Jev judges every item, PICK_MAX per call (jev skill: batch within limits, never silently truncate)
        pool = reading_order(pool)
        labels = describe_cards(pool, snap, snap.window_frame)  # each name with its channel and nearby words
        got = []
        for k in range(0, len(labels), PICK_MAX):
            part = labels[k:k + PICK_MAX]
            try:
                ans = CLASSIFY_ITEMS(noun, part)
            except Exception:
                ans = None
            if not isinstance(ans, list) or len(ans) != len(part) or not all(_valid_flag(g) for g in ans):
                return ("choices", [])  # a failed or malformed answer asks again; nothing is pressed
            got += ans
        literal = [_says(l.rpartition(", ")[0], args.get("kind")) and not (g[0] is False and g[1] >= PICK_GATE)
                   for l, g in zip(labels, got)]  # the heard words on a card, spacing and case aside: NetworkChuck
        if any(literal):  # words the user read off the screen: the cards without them are no longer in question.
            # The words prove the name, not the kind (a channel link says NetworkChuck too). A card Jev was sure of
            # keeps that answer; for the rest Jev judges whether each is a video at all, asked over the whole page
            # (alone, one card reads as a coin toss: live 2026-09-25, .6 alone vs .85 among its neighbours), and an
            # unsure answer stays unsure.
            kinds = None
            if any(hit and not (yes is True and conf >= PICK_GATE) for hit, (yes, conf) in zip(literal, got)):
                kinds = _judge_kind(bare, pool, labels)
                if kinds is None:
                    return ("choices", [])
            got = [((yes, conf) if yes is True and conf >= PICK_GATE else kinds[n]) if hit
                   else (False, 1.0) if conf < PICK_GATE else (yes, conf)
                   for n, (hit, (yes, conf)) in enumerate(zip(literal, got))]
        flags = {id(i): g for i, g in zip(pool, got)}
        group = [i for i, (yes, conf) in zip(pool, got) if yes is True and conf >= PICK_GATE]
        unsure = [i for i, (yes, conf) in zip(pool, got) if conf < PICK_GATE]  # neither a match nor a non-match
    def likeliest(items):  # sure matches first, then the unsure ones leaning yes, then those leaning no
        def rank(i):
            yes, conf = flags.get(id(i), (True, 1.0))
            return (0, -conf) if yes and conf >= PICK_GATE else (1, -conf) if yes else (2, conf)
        return sorted(items, key=rank)

    if not group:
        if unsure:
            return ("choices", [_choice(snap, i) for i in likeliest(unsure)[:4]])
        return ("none", f"no {noun}s on screen")

    def ask(items):  # an unsure item could change the answer: ask rather than count past it
        return ("choices", [_choice(snap, i) for i in items[:4]])
    # One order for the whole pool, filtered, never regrouped: rows form around their tallest item, so ordering a
    # subset can reorder what's left (Astra, 2026-09-25).
    sure_ids, unsure_ids = {id(i) for i in group}, {id(i) for i in unsure}
    order = [i for i in reading_order(pool) if id(i) in sure_ids | unsure_ids]
    ordered = [i for i in order if id(i) in sure_ids]
    if "where" in args:
        if unsure:
            return ask(order)
        chosen = nearest_to(group, args["where"], snap.window_frame)
    else:
        k = args.get("ordinal", 1)
        if k == 0:  # "the Full Tilt video": exactly one, else ask which, likeliest first
            if len(ordered) > 1 or unsure:  # any unsure rival, even one leaning no, leaves "which one" open
                return ask(likeliest(order))
            chosen = ordered[0]
        elif k == -1:
            chosen = ordered[-1]
            after = order[[id(i) for i in order].index(id(chosen)) + 1:]  # anything past it is unsure
            if after:
                return ask([chosen] + after)
        elif isinstance(k, int) and 1 <= k <= len(ordered):
            chosen = ordered[k - 1]
            upto = order[:[id(i) for i in order].index(id(chosen)) + 1]
            if any(id(i) in unsure_ids for i in upto):
                return ask(upto)
        else:
            if unsure:
                return ask(order)
            return ("none", f"only {len(ordered)} {noun}{'s' if len(ordered) != 1 else ''} on screen")
    return ("target", _screen_target(snap, chosen))


def run_screen_list(t, deadline):
    try:
        snap = screen.desktop_view(deadline=deadline, ocr=True) if DESKTOP else screen.observe(ocr=True,
                                                                                             deadline=deadline)
    except (screen.Unavailable, screen.TimedOut, screen.Wedged) as exc:
        raise Failed(f"can't read the screen: {exc}")
    screen.remember(snap)
    _listed[id(t)] = snap


def verify_screen_list(t, deadline):
    snap = _listed.pop(id(t), None) or screen.last()
    return ("done", {"app": snap.app, "count": len(snap.items), "ocr": snap.ocr, "version": snap.version,
                     "items": [{**i.public(), "win": screen.window_index(i, snap)} for i in snap.items]})


def effect_pending():
    """Any native effect from an earlier step that may still land. Checked by the engine before every dispatch."""
    return screen.effect_pending()


def loggable(action, value):
    """Screen actions log shapes, never screen text: labels become their length, item lists their count."""
    if not (action or "").startswith("screen.") or not isinstance(value, dict):
        return value
    out = {}
    for k, v in value.items():
        if k in ("label", "window", "text", "field") and isinstance(v, str):
            out[k] = f"<{len(v)} chars>"
        elif k == "items" and isinstance(v, list):
            out[k] = len(v)
        elif k == "choices" and isinstance(v, list):
            out[k] = len(v)
        else:
            out[k] = v
    return out


# --------------------------------------------------------------------------- registry
def plain(args):
    return ("target", dict(args))


def entry(effect, resolve, run, verify, proves, timeout=10):
    return {"effect": effect, "resolve": resolve, "run": run, "verify": verify, "proves": proves, "timeout": timeout}


ACTIONS = {
    "app.open": entry("open", resolve_app, run_app_open, verify_app_open, "a running app at the resolved path"),
    "url.open": entry("navigate", resolve_url, run_url, verify_url,
                      "Chrome: the opened tab shows exactly this URL. Safari and others: nothing", 10),
    "app.quit": entry("quit", resolve_app, run_app_quit, verify_app_quit, "no running app at the resolved path"),
    "volume.up": entry("volume", plain, volume_step(20), verify_volume, "output volume reads back the new level", 5),
    "volume.down": entry("volume", plain, volume_step(-20), verify_volume, "output volume reads back the new level", 5),
    "volume.set": entry("volume", lambda a: ("target", {**a, "value": level_value(a)}),
                        set_volume, verify_volume, "output volume reads back the level", 5),
    "volume.mute": entry("volume", plain, run_mute(True), verify_mute(True), "output reads muted", 5),
    "volume.unmute": entry("volume", plain, run_mute(False), verify_mute(False), "output reads unmuted", 5),
    "spotify_volume.up": entry("volume", resolve_spotify, spotify_step(20), verify_spotify_volume, "Spotify volume reads back", 5),
    "spotify_volume.down": entry("volume", resolve_spotify, spotify_step(-20), verify_spotify_volume, "Spotify volume reads back", 5),
    "spotify_volume.set": entry("volume", resolve_spotify, spotify_fixed(level_value),
                                verify_spotify_volume, "Spotify volume reads back", 5),
    "spotify_volume.mute": entry("volume", resolve_spotify, spotify_fixed(lambda t: 0), verify_spotify_volume, "Spotify volume reads 0", 5),
    "spotify_volume.unmute": entry("volume", resolve_spotify, spotify_fixed(lambda t: 50), verify_spotify_volume, "Spotify volume reads 50", 5),
    "display.dark_on": entry("display", plain, run_dark(True), verify_dark, "dark mode reads on", 5),
    "display.dark_off": entry("display", plain, run_dark(False), verify_dark, "dark mode reads off", 5),
    "display.toggle": entry("display", plain, run_dark(None), verify_dark, "dark mode reads flipped", 5),
    "media.play": entry("media", resolve_spotify, run_play, verify_play, "Spotify reports playing", 12),
    "media.pause": entry("media", resolve_spotify, run_spotify_cmd("pause"), verify_paused, "Spotify reports paused", 5),
    "media.next": entry("media", resolve_spotify, run_spotify_cmd("next track", True), verify_next, "Spotify track id changed", 5),
    "media.previous": entry("media", resolve_spotify, run_spotify_cmd("previous track"), None, "nothing: restart and previous look alike", 5),
    "system.lock": entry("lock", plain, run_lock, None, "nothing: no lock-state readback", 5),
    "system.sleep": entry("sleep", plain, run_sleep, None, "nothing: the Mac is asleep", 5),
    "timer.set": entry("timer", resolve_timer_set, run_timer_set, verify_timer_set, "the timer is in the running list", 2),
    "timer.check": entry("timer", resolve_timer_check, run_timer_check, verify_timer_check, "read from the running list", 2),
    "screen.list": entry("look", plain, run_screen_list, verify_screen_list, "the list is what was read", 8),
    "screen.press": entry("click", resolve_screen_press, run_screen_press, verify_screen_press,
                          "the window changed after the press; not that the intended result happened", 6),
    "screen.pick": entry("click", resolve_screen_pick, run_screen_press, verify_screen_press,
                         "the picked control changed after the press; which one was picked is Jev's classification", 6),
    "screen.scroll": entry("scroll", resolve_screen_scroll, run_screen_scroll, verify_screen_scroll,
                           "the view's scroll position changed", 6),
    "pointer.click": entry("click", resolve_pointer_click, run_pointer_click, verify_pointer_click,
                           "the window under the pointer changed; not what the click meant", 6),
    "screen.submit": entry("submit", resolve_screen_submit, run_screen_submit, verify_screen_submit,
                           "the field went away or its text changed; not that anything was sent or accepted", 6),
    "screen.type": entry("type", resolve_screen_type, run_screen_type, verify_screen_type,
                         "the field holds its old text plus exactly the typed text; nothing is submitted", 6),
    "timer.cancel": entry("timer", resolve_timer_cancel, run_timer_cancel, verify_timer_cancel, "the timer left the running list", 2),
}

EFFECTS = ("open", "navigate", "media", "volume", "display", "timer", "scroll", "click", "type", "submit", "task",
           "in_task", "risky", "quit", "lock", "sleep")
DEFAULT_POLICY = {e: ("ask" if e in ("quit", "lock", "sleep", "click", "type", "submit", "task", "risky") else "auto")
                  for e in EFFECTS}  # in_task "auto": the task's one OK covers its clicks, typing and Return
DEFAULT_POLICY["look"] = "auto"  # reading the screen has no effect, so it is not a setting
EFFECT_LABELS = {"open": "Open apps", "navigate": "Open websites", "media": "Music playback", "volume": "Volume",
                 "display": "Dark mode", "timer": "Timers and reminders",
                 "scroll": "Scroll", "click": "Click buttons on screen", "type": "Type into fields",
                 "submit": "Press Return in fields", "task": "Start multi-step tasks",
                 "in_task": "Each step inside a task", "risky": "Risky buttons (Buy, Send, Delete…)", "quit": "Quit apps",
                 "lock": "Lock screen", "sleep": "Sleep the Mac"}


def _timer_name(x):
    d = timers.say_duration(x["secs"])
    return f"the reminder to {x['label']}" if x.get("label") else f"the {d} timer"


def describe(action, target):
    """Plain words for the confirmation pop-down: the exact level, duration and target, never just the category."""
    t = target or {}
    name = t.get("name") or t.get("app") or "the app"
    if t.get("path"):
        name += f" (in {os.path.basename(os.path.dirname(t['path'])) or '/'})"
    level = t.get("level")
    level = f"{level} ({LEVELS[level]}%)" if level in LEVELS else f"{t['percent']}%" if "percent" in t else "that level"
    if action == "timer.set":
        d = timers.say_duration(t.get("secs", 0))
        return f"Remind you in {d} to {t['label']}" if t.get("label") else f"Start a {d} timer"
    if action == "timer.cancel":
        xs = t.get("timers") or []
        if t.get("all"):
            return f"Cancel all {len(xs)} timer{'s' if len(xs) != 1 else ''}"
        return f"Cancel {_timer_name(xs[0])}" if xs else "Cancel a timer"
    what = {"app.quit": "Quit {name}", "app.open": "Open {name}", "url.open": "Open {url}",
            "system.lock": "Lock the screen", "system.sleep": "Put the Mac to sleep",
            "volume.up": "Turn the volume up", "volume.down": "Turn the volume down",
            "volume.mute": "Mute the sound", "volume.unmute": "Unmute the sound", "volume.set": "Set the volume to {level}",
            "spotify_volume.up": "Turn Spotify up", "spotify_volume.down": "Turn Spotify down",
            "spotify_volume.mute": "Mute Spotify", "spotify_volume.unmute": "Unmute Spotify",
            "spotify_volume.set": "Set Spotify's volume to {level}",
            "display.dark_on": "Turn dark mode on", "display.dark_off": "Turn dark mode off",
            "display.toggle": "Switch dark mode", "media.play": "Play music in Spotify", "media.pause": "Pause Spotify",
            "media.next": "Skip to the next track", "media.previous": "Go back a track",
            "timer.check": "Read out the time left",
            "screen.list": "Read what's on screen", "screen.press": "Click “{label}” in {app}",
            "screen.type": "Type “{text}” into {field} in {app}",
            "screen.submit": "Press Return in the selected field in {app}",
            "task.run": "Work on: {goal} (up to {steps} steps)",
            "screen.pick": "Click “{label}” in {app}", "screen.scroll": "Scroll {direction} in {app}", "pointer.click": "Click where the pointer is, in {app}"}.get(action, action.replace(".", ": ").replace("_", " "))
    return what.format(name=name, url=t.get("url") or "the website", level=level, label=t.get("label") or "that",
                       text=t.get("text") or "", field=f"“{t['label']}”" if t.get("label") else "the selected field",
                       app=t.get("app") or "the app", goal=t.get("goal") or "this", steps=t.get("steps") or 15,
                       direction=t.get("direction") or "down")
