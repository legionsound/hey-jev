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
    "timer.cancel": entry("timer", resolve_timer_cancel, run_timer_cancel, verify_timer_cancel, "the timer left the running list", 2),
}

EFFECTS = ("open", "navigate", "media", "volume", "display", "timer", "quit", "lock", "sleep")
DEFAULT_POLICY = {e: ("ask" if e in ("quit", "lock", "sleep") else "auto") for e in EFFECTS}
EFFECT_LABELS = {"open": "Open apps", "navigate": "Open websites", "media": "Music playback", "volume": "Volume",
                 "display": "Dark mode", "timer": "Timers and reminders", "quit": "Quit apps",
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
            "timer.check": "Read out the time left"}.get(action, action.replace(".", ": ").replace("_", " "))
    return what.format(name=name, url=t.get("url") or "the website", level=level)
