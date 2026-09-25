"""What is on screen: one app window read through Accessibility, with Apple Vision OCR filling the gaps.

observe(pid=None, ocr=True, deadline=None) -> Snapshot
    The frontmost app's focused window (or the given pid's). Items are numbered in reading order. Each carries its
    source: "ax" (a control the app declared), "ocr" (text read off the pixels, not proof of a control) or "ax+ocr".
    Screen text is data only: nothing here acts on what it reads.
press(ref, deadline) -> AX error code. AXPress on a control the app declared; OCR-only text is never clicked.
signature(pid, deadline) -> a light AX-only fingerprint of the window, for "did anything change?".

Identity is the element itself: AX elements compare equal across reads when they are the same element, so each
gets a local token (e1, e2...) and a replacement at the same spot is a different token. Process identity is pid plus
start time. The last snapshot a user was shown is kept in LAST, so "click 12" means the 12 they saw, and only that
exact element, still present, enabled and pressable, in the same window of the same process.

Every native read runs under one caller deadline (bounded()): a hung app or a slow Vision request ends the wait,
not the app. Labels come from AXTitle/AXDescription; AXValue is used only for non-editable roles, and is marked,
so document and field contents never become a label sent anywhere.
"""
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field

import ax_walk

AX_MESSAGE_TIMEOUT = 0.25
AX_VALUE_CHARS = 120
OCR_MIN_CONFIDENCE = 0.4
MAX_ITEMS = 60
LAST = None  # the Snapshot most recently listed to the user
_lock = threading.Lock()


class Unavailable(Exception):
    """Permission missing, no window, or the screen could not be read. Nothing was done."""


class TimedOut(Exception):
    """The deadline passed with a native call still out."""


class Wedged(Exception):
    """An earlier native call is still out, so no new one starts: the serial boundary holds until it settles."""


EFFECT_SETTLE = 10.0  # after a press times out, keep waiting this long for it to finish before calling it wedged
MAX_ABANDONED_READS = 3
_abandoned = []  # (thread, is_effect) of calls that outlived their deadline
_abandoned_lock = threading.Lock()


def _outstanding():
    with _abandoned_lock:
        _abandoned[:] = [(t, e) for t, e in _abandoned if t.is_alive()]
        return [e for _, e in _abandoned]


def effect_pending():
    """True while an abandoned effect (a press, an insert) may still land. The engine holds every action family's
    dispatch on this, not just screen calls."""
    return any(_outstanding())


def bounded(fn, deadline, *args, effect=False):
    """Run fn on a daemon thread and wait until the deadline.

    A thread can't be killed, so a call that outlives its deadline is tracked, and nothing new starts while an
    abandoned effect (a press, an insert) is still out, or while too many abandoned reads are. For an effect the
    caller waits up to EFFECT_SETTLE more; if it lands in that time it is still reported as TimedOut (late: the outcome
    is unknown), and if it doesn't the screen is wedged until it does."""
    out = _outstanding()
    if any(out):
        raise Wedged("an earlier press has not finished")
    if len(out) >= MAX_ABANDONED_READS:
        raise Wedged("earlier screen reads have not finished")
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimedOut(getattr(fn, "__name__", "call"))
    box = {}

    def run():
        try:
            box["ok"] = fn(*args)
        except BaseException as exc:  # handed back to the caller
            box["err"] = exc
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(left)
    if t.is_alive() and effect:
        t.join(EFFECT_SETTLE)  # hold the serial boundary: the next command must not overtake a pending press
    if t.is_alive():
        with _abandoned_lock:
            _abandoned.append((t, effect))
        raise TimedOut(getattr(fn, "__name__", "call"))
    if effect and time.monotonic() > deadline:
        raise TimedOut("answered after the deadline")  # it did land, late: the caller reports unknown
    if "err" in box:
        raise box["err"]
    return box["ok"]


_tokens = []  # [(element, token)], oldest first; equality is the AX element's own
_token_lock = threading.Lock()
_next_token = [0]  # never reused, whatever the cache size
TOKEN_CAP = 4000


def token(element):
    """A local id for this exact element, never reused. Once evicted, an element's old token names nothing."""
    with _token_lock:
        for el, tok in _tokens:
            if el == element:
                return tok
        _next_token[0] += 1
        tok = f"e{_next_token[0]}"
        _tokens.append((element, tok))
        del _tokens[:-TOKEN_CAP]
        return tok


def element_for(tok):
    with _token_lock:
        return next((el for el, t in _tokens if t == tok), None)


def process_start(pid, deadline):
    """Start time of the process, so a recycled pid is a different process."""
    try:
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                           timeout=max(0.05, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        raise TimedOut("ps")
    start = r.stdout.strip()
    if not start:
        raise Unavailable(f"no process {pid}")
    return start


@dataclass
class Item:
    n: int
    source: str  # ax | ocr | ax+ocr
    role: str  # AXButton..., or "text" for OCR
    label: str
    frame: tuple  # x, y, w, h in screen points, top-left origin
    pressable: bool
    ref: object = field(default=None, repr=False, compare=False)
    enabled: bool = True
    from_value: bool = False  # the label is the element's AXValue, not its name
    secure: bool = False  # a password field: never typed into, its value never read

    @property
    def token(self):
        return token(self.ref) if self.ref is not None else None

    def key(self):
        """Identity across observations: same role, label and (rounded) place."""
        return (self.role, self.label, tuple(round(v) for v in self.frame))

    def public(self):
        return {"n": self.n, "source": self.source, "role": self.role, "label": self.label,
                "frame": [round(v) for v in self.frame], "pressable": self.pressable}


@dataclass
class Snapshot:
    pid: int
    app: str
    bundle: str
    window: str
    window_frame: tuple
    items: list
    truncated: bool = False
    ocr: str = "off"  # off | ok | no_permission | failed | timed_out
    ms: dict = field(default_factory=dict)
    window_ref: object = field(default=None, repr=False)
    started: str = ""  # process start time

    @property
    def window_token(self):
        return token(self.window_ref) if self.window_ref is not None else None

    def public(self):
        return {"app": self.app, "bundle": self.bundle, "window": self.window, "pid": self.pid,
                "items": [i.public() for i in self.items], "truncated": self.truncated, "ocr": self.ocr, "ms": self.ms}


# --------------------------------------------------------------------------- Accessibility bridge
def _AS():
    import ApplicationServices
    return ApplicationServices


AX_NO_VALUE, AX_UNSUPPORTED, AX_INVALID = -25212, -25205, -25202


def _read(element, name):
    """(status, value): ok, absent (the element has no such attribute or no value), gone (the element was
    destroyed), or unknown (any other error: nothing can be concluded)."""
    try:
        err, value = _AS().AXUIElementCopyAttributeValue(element, name, None)
    except Exception:
        return "unknown", None
    if err == 0:
        return "ok", value
    if err in (AX_NO_VALUE, AX_UNSUPPORTED):
        return "absent", None
    if err == AX_INVALID:
        return "gone", None
    return "unknown", None


def _attr(element, name):
    """The value, or None for any miss. For decisions that must not guess, use _read."""
    return _read(element, name)[1]


EDITABLE = {"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox", "AXSecureTextField"}


def _named(element):
    """(label, came from AXValue). AXTitle on AppKit, AXDescription on web and Electron; a short AXValue only for
    roles whose value is not something the user typed or a document holds."""
    for name in ("AXTitle", "AXDescription"):
        text = _attr(element, name)
        if isinstance(text, str) and text.strip():
            return " ".join(text.split()), False
    role, sub = str(_attr(element, "AXRole") or ""), str(_attr(element, "AXSubrole") or "")
    if role in EDITABLE or sub == "AXSecureTextField":
        hint = _attr(element, "AXPlaceholderValue")  # the field's prompt text is UI, not what anyone typed
        return (" ".join(hint.split()), False) if isinstance(hint, str) and hint.strip() else ("", False)
    value = _attr(element, "AXValue")
    if isinstance(value, str) and 0 < len(value.strip()) <= AX_VALUE_CHARS:
        return " ".join(value.split()), True
    return "", False


def _label(element):
    return _named(element)[0]


def _frame(element):
    AS = _AS()
    pos, size = _attr(element, "AXPosition"), _attr(element, "AXSize")
    if pos is None or size is None:
        return None
    ok_p, pt = AS.AXValueGetValue(pos, AS.kAXValueCGPointType, None)
    ok_s, sz = AS.AXValueGetValue(size, AS.kAXValueCGSizeType, None)
    if not (ok_p and ok_s):
        return None
    return float(pt.x), float(pt.y), float(sz.width), float(sz.height)


def _children(element):
    return list(_attr(element, "AXChildren") or [])


def _attrs(element):
    return ax_walk.AxAttrs(str(_attr(element, "AXRole") or ""), _label(element), _frame(element))


def enabled(element):
    """True only when the element says it is enabled, or has no enabled state at all. Unknown is not enabled."""
    status, value = _read(element, "AXEnabled")
    return status == "absent" or (status == "ok" and value is not False)


def is_secure(element):
    """A password field, or one whose kind can't be read: either way, never typed into and never read."""
    (rs, role), (ss, sub) = _read(element, "AXRole"), _read(element, "AXSubrole")
    if rs != "ok" or ss not in ("ok", "absent"):
        return True
    return "AXSecureTextField" in (str(role), str(sub or ""))


def _actions(element):
    try:
        err, names = _AS().AXUIElementCopyActionNames(element, None)
    except Exception:
        return []
    return [str(n) for n in names] if err == 0 and names else []




_asked = set()  # permissions whose macOS prompt was already shown this run


def trusted():
    """Accessibility permission. The first time it's missing in a run, macOS shows its own prompt, once."""
    AS = _AS()
    if AS.AXIsProcessTrusted():
        return True
    if "ax" not in _asked:
        _asked.add("ax")
        AS.AXIsProcessTrustedWithOptions({AS.kAXTrustedCheckOptionPrompt: True})
    return False


def frontmost():
    """(pid, name, bundle id) of the frontmost app. HEYJEV_SCREEN_PID pins another app, for tests only."""
    if os.environ.get("HEYJEV_SCREEN_PID"):
        pid = int(os.environ["HEYJEV_SCREEN_PID"])
        return (pid, *_app_info(pid))
    from AppKit import NSWorkspace
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        raise Unavailable("no frontmost app")
    pid = int(app.processIdentifier())
    if pid == os.getpid():  # our own confirmation pop-down took the foreground: use the app it took it from
        pid = handoff()
        if pid is None:
            raise Unavailable("Hey Jev is in front and no app handed off to it")
    return (pid, *_app_info(pid))


HANDOFF_GRACE = 10.0  # the handoff outlives the pop-down this long, so the post-confirm re-check still finds it
_handoff = [None, None]  # [pid of the app in front when the pop-down opened, expiry time or None while open]


def set_handoff(pid):
    """The UI calls this with the app's pid when its pop-down takes the foreground."""
    _handoff[:] = [pid, None]


def end_handoff():
    """The pop-down closed: the handoff stays good for HANDOFF_GRACE, then names nothing."""
    if _handoff[0] is not None:
        _handoff[1] = time.monotonic() + HANDOFF_GRACE


def handoff():
    pid, expires = _handoff
    return pid if pid is not None and (expires is None or time.monotonic() < expires) else None


def _app_info(pid):
    from AppKit import NSRunningApplication
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app is None:
        raise Unavailable(f"no app with pid {pid}")
    return str(app.localizedName() or ""), str(app.bundleIdentifier() or "")


def _window(app_el):
    """The focused window, else the main one, else the first."""
    for name in ("AXFocusedWindow", "AXMainWindow"):
        w = _attr(app_el, name)
        if w is not None:
            return w
    wins = _attr(app_el, "AXWindows") or []
    return wins[0] if wins else None


def _walk(win, wframe, time_cap):
    """The window's own controls: the visible area is the window, wherever it sits on which display."""
    x, y, w, h = wframe
    return ax_walk.walk_actionable(win, _children, _attrs, _actions, w, h, time_cap=time_cap, x0=x, y0=y)


def _inside(frame, box, slack=2):
    x, y, w, h = frame
    bx, by, bw, bh = box
    cx, cy = x + w / 2, y + h / 2
    return bx - slack <= cx <= bx + bw + slack and by - slack <= cy <= by + bh + slack


# --------------------------------------------------------------------------- OCR
_vision = {}


def _vision_classes():
    if not _vision:
        import objc
        objc.loadBundle("Vision", _vision, bundle_path="/System/Library/Frameworks/Vision.framework")
    return _vision["VNRecognizeTextRequest"], _vision["VNImageRequestHandler"]


def _cg_window_id(pid, frame):
    """The on-screen CG window of this pid whose bounds match the AX window frame."""
    import Quartz
    opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    best = None
    for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []:
        if w.get("kCGWindowOwnerPID") != pid or w.get("kCGWindowLayer") != 0:
            continue
        b = w.get("kCGWindowBounds") or {}
        d = sum(abs(float(b.get(k, 0)) - v) for k, v in zip(("X", "Y", "Width", "Height"), frame))
        if best is None or d < best[0]:
            best = (d, int(w["kCGWindowNumber"]))
    return best[1] if best and best[0] <= 8 else None


def configure_ocr(req):
    """Apply Settings' text reading mode to a Vision request: accurate (0, with language correction) unless the
    user chose fast (1). An unreadable preference means accurate. -> True when fast."""
    try:
        import model_settings
        fast = model_settings.ocr_level() == "fast"
    except Exception:
        fast = False
    req.setRecognitionLevel_(1 if fast else 0)
    req.setUsesLanguageCorrection_(not fast)
    return fast


def read_text(pid, frame, deadline):
    """[(text, confidence, (x, y, w, h) screen points)] for one window. Raises Unavailable."""
    import Quartz
    if not Quartz.CGPreflightScreenCaptureAccess():
        if "screen" not in _asked:
            _asked.add("screen")
            Quartz.CGRequestScreenCaptureAccess()  # macOS's own prompt, once per run; text reading waits for it
        raise Unavailable("no_permission")
    wid = _cg_window_id(pid, frame)
    if wid is None:
        raise Unavailable("window not on screen")
    fd, path = tempfile.mkstemp(suffix=".png", prefix="heyjev-screen-")
    os.close(fd)
    try:
        left = deadline - time.monotonic()
        if left <= 0:
            raise Unavailable("out of time before capture")
        try:
            r = subprocess.run(["screencapture", "-x", "-o", "-l", str(wid), path], capture_output=True, timeout=left)
        except subprocess.TimeoutExpired:
            raise Unavailable("capture timed out")
        if r.returncode or not os.path.getsize(path):
            raise Unavailable("capture failed")
        from Foundation import NSDictionary, NSURL
        request_cls, handler_cls = _vision_classes()
        req = request_cls.alloc().init()
        configure_ocr(req)
        handler = handler_cls.alloc().initWithURL_options_(NSURL.fileURLWithPath_(path), NSDictionary.dictionary())
        ok = handler.performRequests_error_([req], None)
        if not (ok[0] if isinstance(ok, tuple) else ok):
            raise Unavailable("text recognition failed")
        x0, y0, w0, h0 = frame
        out = []
        for obs in req.results() or []:
            cands = obs.topCandidates_(1)
            if not cands:
                continue
            text, conf = " ".join(str(cands[0].string()).split()), float(cands[0].confidence())
            if not text or conf < OCR_MIN_CONFIDENCE:
                continue
            b = obs.boundingBox()  # normalized, bottom-left origin
            out.append((text, conf, (x0 + b.origin.x * w0, y0 + (1 - b.origin.y - b.size.height) * h0,
                                     b.size.width * w0, b.size.height * h0)))
        return out
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# --------------------------------------------------------------------------- observation
def _norm(text):
    return " ".join(re.findall(r"[^\W_]+", (text or "").lower()))


def merge(controls, texts, window_frame):
    """AX controls first; an OCR line centred on a control is that control's text (and marks it ax+ocr).
    Remaining OCR lines inside the window become text items. Reading order: rows top to bottom, then left to right."""
    items = []
    for c in controls:
        items.append(Item(0, "ax", c.role, c.label, (c.x, c.y, c.w, c.h), c.pressable, c.ref))
    for text, _conf, box in texts:
        if not _inside(box, window_frame):
            continue
        host = next((i for i in items if i.source != "ocr" and _inside(box, i.frame)), None)
        if host is not None:
            if _norm(text) and _norm(text) in _norm(host.label):
                host.source = "ax+ocr"
            continue
        items.append(Item(0, "ocr", "text", text, box, False))
    items.sort(key=lambda i: (round(i.frame[1] / 12), i.frame[0]))
    for n, i in enumerate(items, 1):
        i.n = n
    return items


def _read_ax(pid, deadline):
    AS = _AS()
    app_el = AS.AXUIElementCreateApplication(pid)
    AS.AXUIElementSetMessagingTimeout(app_el, AX_MESSAGE_TIMEOUT)
    win = _window(app_el)
    if win is None:
        raise Unavailable("no window")
    wframe = _frame(win)
    if not wframe:
        raise Unavailable("window has no frame")
    left = max(0.05, min(ax_walk.AX_TIME_CAP, deadline - time.monotonic()))
    found, _offscreen, truncated = _walk(win, wframe, left)
    menubar = _attr(app_el, "AXMenuBar")
    bar = [ax_walk.AxNode(r, l, *f, True, k) for k in (_children(menubar) if menubar is not None else [])
           for r, l, f in [_attrs(k)] if l and f and r == "AXMenuBarItem" and l != "Apple"]
    controls = bar + found
    extra = {id(c): (enabled(c.ref), _named(c.ref)[1], is_secure(c.ref)) for c in controls}
    return win, wframe, _label(win), controls, extra, truncated


def observe(pid=None, ocr=True, deadline=None):
    """Read one window under one deadline. Raises Unavailable or TimedOut; never acts."""
    t0 = time.monotonic()
    deadline = deadline or t0 + 4.0
    if not trusted():
        raise Unavailable("accessibility_permission")
    if pid is None:
        pid, app, bundle = frontmost()
    else:
        app, bundle = _app_info(pid)
    started = process_start(pid, deadline)
    win, wframe, title, controls, extra, truncated = bounded(_read_ax, deadline, pid, deadline)
    t_ax = time.monotonic()
    texts, ocr_state = [], "off"
    if ocr:
        try:
            texts, ocr_state = bounded(read_text, deadline, pid, wframe, deadline), "ok"
        except TimedOut:
            ocr_state = "timed_out"  # the controls still stand; text is a bonus
        except Unavailable as exc:
            ocr_state = "no_permission" if str(exc) == "no_permission" else "failed"
    items = merge(controls, texts, wframe)
    for i in items:
        if i.source != "ocr":
            ref_extra = next((v for c in controls if c.ref is i.ref for v in [extra[id(c)]]), (True, False, False))
            i.enabled, i.from_value, i.secure = ref_extra
    return Snapshot(pid, app, bundle, title, wframe, items[:MAX_ITEMS], truncated or len(items) > MAX_ITEMS, ocr_state,
                    {"ax": round((t_ax - t0) * 1000), "ocr": round((time.monotonic() - t_ax) * 1000)},
                    window_ref=win, started=started)


def remember(snap):
    global LAST
    with _lock:
        LAST = snap


def last():
    with _lock:
        return LAST


# --------------------------------------------------------------------------- acting and reading back
def press(ref, deadline):
    """AXPress. Returns the AX error code (0 is delivered). Raises TimedOut when the app didn't answer in time: the
    press may or may not have happened, and no other screen call starts until it settles."""
    return bounded(lambda: int(_AS().AXUIElementPerformAction(ref, "AXPress")), deadline, effect=True)


UNKNOWN = "?unknown"


def element_state(ref, deadline):
    """What a press on this element might flip: value, selection, expansion, and whether it still exists.
    A read that failed is UNKNOWN, never a value: it can't prove a change or a disappearance."""
    def read():
        role, _ = _read(ref, "AXRole")
        state = {"exists": {"ok": "yes", "gone": "no"}.get(role, UNKNOWN)}
        for k in ("AXValue", "AXSelected", "AXExpanded"):
            st, v = _read(ref, k)
            state[k] = repr(v) if st in ("ok", "absent") else UNKNOWN
        return state
    return bounded(read, deadline)


def field_facts(ref, deadline):
    """Role, secure flag, frame, window token and whether text can be inserted, read fresh from the element."""
    def read():
        return {"role": str(_attr(ref, "AXRole") or ""), "secure": is_secure(ref), "enabled": enabled(ref),
                "frame": [round(v) for v in (_frame(ref) or (0, 0, 0, 0))],
                "window": token(_attr(ref, "AXWindow")) if _attr(ref, "AXWindow") is not None else None,
                "insertable": _settable(ref, "AXSelectedText")}
    return bounded(read, deadline)


def _settable(ref, name):
    try:
        err, ok = _AS().AXUIElementIsAttributeSettable(ref, name, None)
    except Exception:
        return False
    return err == 0 and bool(ok)


def focused_field(pid, deadline):
    """The app's focused element, or None."""
    def read():
        AS = _AS()
        app_el = AS.AXUIElementCreateApplication(pid)
        AS.AXUIElementSetMessagingTimeout(app_el, AX_MESSAGE_TIMEOUT)
        return _attr(app_el, "AXFocusedUIElement")
    return bounded(read, deadline)


def field_value(ref, deadline):
    """The field's text, for the before/after check only: never stored, logged or sent. Secure fields read as None."""
    return bounded(lambda: None if is_secure(ref) else _attr(ref, "AXValue"), deadline)


def selected_range(ref, deadline):
    """(location, length) of the field's selection in UTF-16 units, as AX reports it, or None when unreadable."""
    def read():
        st, v = _read(ref, "AXSelectedTextRange")
        if st != "ok" or v is None:
            return None
        ok, rng = _AS().AXValueGetValue(v, _AS().kAXValueCFRangeType, None)
        if not ok:
            return None
        loc, length = (rng.location, rng.length) if hasattr(rng, "location") else rng
        return int(loc), int(length)
    return bounded(read, deadline)


def expected_after(before, rng, text):
    """before with the UTF-16 range replaced by text, or None when the range doesn't fit or splits a character."""
    b, t = before.encode("utf-16-le"), text.encode("utf-16-le")
    loc, length = rng
    if loc < 0 or length < 0 or 2 * (loc + length) > len(b):
        return None
    try:
        return (b[:2 * loc] + t + b[2 * (loc + length):]).decode("utf-16-le")
    except UnicodeDecodeError:
        return None


def focus(ref, deadline):
    """Give the field keyboard focus. Done before reading its selection, since focusing can change it."""
    return bounded(lambda: int(_AS().AXUIElementSetAttributeValue(ref, "AXFocused", True)), deadline, effect=True)


def insert_text(ref, text, deadline):
    """Insert at the field's selection through AXSelectedText: no keystrokes, so nothing can land in another window.
    Returns the AX error code."""
    return bounded(lambda: int(_AS().AXUIElementSetAttributeValue(ref, "AXSelectedText", text)), deadline,
                   effect=True)


def can_confirm(ref, deadline):
    return bounded(lambda: enabled(ref) and "AXConfirm" in _actions(ref), deadline)


def confirm(ref, deadline):
    """AXConfirm: what Return does in a field, sent to that element. No keystroke."""
    return bounded(lambda: int(_AS().AXUIElementPerformAction(ref, "AXConfirm")), deadline, effect=True)


def is_pressable(ref, deadline):
    return bounded(lambda: enabled(ref) and "AXPress" in _actions(ref), deadline)


def signature(pid, deadline):
    """AX-only fingerprint: window, window count, focused element, open menus, the control set, a text hash."""
    def read():
        AS = _AS()
        app_el = AS.AXUIElementCreateApplication(pid)
        AS.AXUIElementSetMessagingTimeout(app_el, AX_MESSAGE_TIMEOUT)
        win = _window(app_el)
        focused = _attr(app_el, "AXFocusedUIElement")
        bar = _attr(app_el, "AXMenuBar")
        sig = {"window": token(win) if win is not None else None,
               "windows": len(_attr(app_el, "AXWindows") or []),
               "focused": token(focused) if focused is not None else None,
               "menu_open": any(_attr(k, "AXSelected") for k in _children(bar)) if bar is not None else False}
        if win is not None:
            found, _, _ = _walk(win, _frame(win) or (0, 0, 0, 0), 0.4)
            sig["controls"] = sorted((c.role, c.label) for c in found)
            sig["text"] = _text_digest(win)
        return sig
    return bounded(read, deadline)


def _text_digest(win, node_cap=1500, time_cap=0.3):
    """A hash of the window's visible text, so a label that changes ("Count: 1") counts as a change.
    Only the hash is kept: the text itself is never stored or logged."""
    import hashlib
    h, queue, seen, end = hashlib.sha256(), [win], 0, time.monotonic() + time_cap
    while queue and seen < node_cap and time.monotonic() < end:
        el = queue.pop(0)
        seen += 1
        if str(_attr(el, "AXRole") or "") in ("AXStaticText", "AXTextField", "AXTextArea"):
            v = _attr(el, "AXValue")
            h.update(str(v if isinstance(v, str) else "").encode() + b"\0")
        queue.extend(_children(el))
    return h.hexdigest()[:16]
