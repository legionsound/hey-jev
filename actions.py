"""Action registry: resolve a target, run the native effect, read the result back.

Every native call is a subprocess bounded by the step deadline (time.monotonic).
resolve(args) -> ("target", t) | ("choices", [...]) | ("none", reason)
run(target, deadline) -> None, may record prior state in target; raises Failed or Timeout
verify(target, deadline) -> ("done" | "wait" | "failed" | "unverified", facts), or None when nothing can be read
"""
import re
import subprocess
import time

import app_catalog
import timers
import url_adapter

LEVELS = {"silent": 0, "quiet": 25, "medium": 50, "loud": 75, "max": 100}


class Failed(Exception):
    """The native call reported an error: the effect did not happen."""


class Timeout(Exception):
    """The deadline passed with the native call still out: outcome unknown."""


def sh(cmd, deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise Timeout(cmd[0])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=left)
    except subprocess.TimeoutExpired:  # subprocess.run kills and reaps the child
        raise Timeout(cmd[0])
    if r.returncode:
        raise Failed(r.stderr.strip() or f"{cmd[0]} exited {r.returncode}")
    return r.stdout.strip()


def osa(deadline, *lines, argv=()):
    """Fixed script lines; user values only ever travel as argv, never inside the script."""
    cmd = ["osascript"]
    for line in lines:
        cmd += ["-e", line]
    return sh(cmd + list(argv), deadline)


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


def running_paths(bundle_id, deadline):
    """Bundle paths of running processes with this bundle id, via lsappinfo (no Apple Events)."""
    asns = re.findall(r"ASN:0x[0-9a-f]+-0x[0-9a-f]+", sh(["lsappinfo", "find", f"bundleid={bundle_id}"], deadline))
    paths = []
    for asn in asns:
        m = re.search(r'bundle path="([^"]*)"', sh(["lsappinfo", "info", "-only", "bundlepath", asn], deadline))
        if m:
            paths.append(m[1])
    return paths


def run_app_open(t, deadline):
    sh(["open", "-a", t["path"]] if t.get("path") else ["open", "-b", t["bundle_id"]], deadline)


def verify_app_open(t, deadline):
    paths = running_paths(t["bundle_id"], deadline)
    facts = {"running_paths": paths}
    if t.get("path") and t["path"] in paths or not t.get("path") and paths:
        return ("done", facts)
    return ("wait", facts)


def run_app_quit(t, deadline):
    osa(deadline, "on run argv", "tell application id (item 1 of argv) to quit", "end run", argv=[t["bundle_id"]])


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
    osa(deadline, "on run argv", "set volume output volume (item 1 of argv as integer)", "end run", argv=[str(t["value"])])


def volume_step(delta):
    def run(t, deadline):
        t["value"] = max(0, min(100, get_volume(deadline) + delta))
        set_volume(t, deadline)
    return run


def verify_volume(t, deadline):
    v = get_volume(deadline)
    return ("done" if abs(v - t["value"]) <= 1 else "wait", {"volume": v})


def run_mute(muted):
    def run(t, deadline):
        osa(deadline, f"set volume output muted {'true' if muted else 'false'}")
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
        "end run", argv=[str(t["value"])])


def spotify_step(delta):
    def run(t, deadline):
        if not spotify_running(deadline):
            raise Failed("Spotify is not running")
        t["value"] = max(0, min(100, get_spotify_volume(deadline) + delta))
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
        sh(["open", "-b", "com.spotify.client"], deadline)
    osa(deadline, 'tell application "Spotify" to play')


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
        osa(deadline, f'tell application "Spotify" to {cmd}')
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
            + ("true" if t["value"] else "false"))
    return run


def verify_dark(t, deadline):
    d = get_dark(deadline)
    return ("done" if d == t["value"] else "wait", {"dark_mode": d})


# --------------------------------------------------------------------------- system
def run_lock(t, deadline):
    osa(deadline, 'tell application "System Events" to keystroke "q" using {control down, command down}')


def run_sleep(t, deadline):
    sh(["pmset", "sleepnow"], deadline)


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


def resolve_timer_query(args):
    with timers.LOCK:
        if not timers.TIMERS:
            return ("none", "no timer running")
    return ("target", {"all": bool(re.search(r"\ball\b", args.get("text", "").lower()))})


def run_timer_check(t, deadline):
    with timers.LOCK:
        if not timers.TIMERS:
            raise Failed("no timer running")
        t["left"] = timers.TIMERS[0]["end"] - time.time()


def verify_timer_check(t, deadline):
    return ("done", {"left": timers.say_duration(t["left"])})


def run_timer_cancel(t, deadline):
    with timers.LOCK:
        if not timers.TIMERS:
            raise Failed("no timer running")
        gone = list(timers.TIMERS) if t["all"] else [max(timers.TIMERS, key=lambda x: x["end"] - x["secs"])]
        for x in gone:
            timers.TIMERS.remove(x)
        t["cancelled"] = gone


def verify_timer_cancel(t, deadline):
    with timers.LOCK:
        left = [x for x in t["cancelled"] if any(x is y for y in timers.TIMERS)]
    return ("done" if not left else "failed", {"cancelled": len(t["cancelled"])})


# --------------------------------------------------------------------------- websites
def _wall(deadline):
    """url_adapter keeps wall-clock deadlines; the engine keeps monotonic ones."""
    return time.time() + (deadline - time.monotonic())


def _adapted(fn):
    def call(t, deadline):
        try:
            return fn(t, _wall(deadline))
        except TimeoutError as exc:
            raise Timeout(str(exc))
        except (RuntimeError, ValueError) as exc:
            raise Failed(str(exc))
    return call


def resolve_url(args):
    return url_adapter.resolve_url(args.get("url", ""))


# --------------------------------------------------------------------------- registry
def plain(args):
    return ("target", dict(args))


def entry(effect, resolve, run, verify, proves, timeout=10):
    return {"effect": effect, "resolve": resolve, "run": run, "verify": verify, "proves": proves, "timeout": timeout}


ACTIONS = {
    "app.open": entry("open", resolve_app, run_app_open, verify_app_open, "a running app at the resolved path"),
    "url.open": entry("navigate", resolve_url, _adapted(url_adapter.run_url_open), _adapted(url_adapter.verify_url_open),
                      "Chrome: the opened tab shows exactly this URL. Safari and others: nothing", 10),
    "app.quit": entry("quit", resolve_app, run_app_quit, verify_app_quit, "no running app at the resolved path"),
    "volume.up": entry("volume", plain, volume_step(20), verify_volume, "output volume reads back the new level", 5),
    "volume.down": entry("volume", plain, volume_step(-20), verify_volume, "output volume reads back the new level", 5),
    "volume.set": entry("volume", lambda a: ("target", {**a, "value": LEVELS.get(a.get("level"), 50)}),
                        set_volume, verify_volume, "output volume reads back the level", 5),
    "volume.mute": entry("volume", plain, run_mute(True), verify_mute(True), "output reads muted", 5),
    "volume.unmute": entry("volume", plain, run_mute(False), verify_mute(False), "output reads unmuted", 5),
    "spotify_volume.up": entry("volume", resolve_spotify, spotify_step(20), verify_spotify_volume, "Spotify volume reads back", 5),
    "spotify_volume.down": entry("volume", resolve_spotify, spotify_step(-20), verify_spotify_volume, "Spotify volume reads back", 5),
    "spotify_volume.set": entry("volume", resolve_spotify, spotify_fixed(lambda t: LEVELS.get(t.get("level"), 50)),
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
    "timer.check": entry("timer", resolve_timer_query, run_timer_check, verify_timer_check, "read from the running list", 2),
    "timer.cancel": entry("timer", resolve_timer_query, run_timer_cancel, verify_timer_cancel, "the timer left the running list", 2),
}

EFFECTS = ("open", "navigate", "media", "volume", "display", "timer", "quit", "lock", "sleep")
DEFAULT_POLICY = {e: ("ask" if e in ("quit", "lock", "sleep") else "auto") for e in EFFECTS}
EFFECT_LABELS = {"open": "Open apps", "navigate": "Open websites", "media": "Music playback", "volume": "Volume",
                 "display": "Dark mode", "timer": "Timers and reminders", "quit": "Quit apps",
                 "lock": "Lock screen", "sleep": "Sleep the Mac"}


def describe(action, target):
    """Plain words for the confirmation pop-down."""
    what = {"app.quit": "Quit {app}", "app.open": "Open {app}", "url.open": "Open {url}", "system.lock": "Lock the screen",
            "system.sleep": "Put the Mac to sleep"}.get(action, action.replace(".", ": ").replace("_", " "))
    t = target or {}
    return what.format(app=t.get("name") or t.get("app") or "the app", url=t.get("url") or "the website")
