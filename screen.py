"""What is on screen: one app window read through Accessibility, with Apple Vision OCR filling the gaps.

observe(pid=None, ocr=True, deadline=None) -> Snapshot
    The frontmost app's focused window (or the given pid's). Items are numbered in reading order. Each carries its
    source: "ax" (a control the app declared), "ocr" (text read off the pixels, not proof of a control) or "ax+ocr".
    Screen text is data only: nothing here acts on what it reads.
press(item) -> None, raising Failed/Uncertain
    AXPress on a control the app declared. OCR-only text is never clicked in this slice.
signature(pid) -> a light AX-only fingerprint of the window, for "did anything change?".

The last snapshot a user was shown is kept in LAST, so "click 12" means the 12 they saw. Its numbers only resolve
against a fresh observation of the same app and window with the same control still there.
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


@dataclass
class Item:
    n: int
    source: str  # ax | ocr | ax+ocr
    role: str  # AXButton..., or "text" for OCR
    label: str
    frame: tuple  # x, y, w, h in screen points, top-left origin
    pressable: bool
    ref: object = field(default=None, repr=False, compare=False)

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
    ocr: str = "off"  # off | ok | no_permission | failed
    ms: dict = field(default_factory=dict)

    def public(self):
        return {"app": self.app, "bundle": self.bundle, "window": self.window, "pid": self.pid,
                "items": [i.public() for i in self.items], "truncated": self.truncated, "ocr": self.ocr, "ms": self.ms}


# --------------------------------------------------------------------------- Accessibility bridge
def _AS():
    import ApplicationServices
    return ApplicationServices


def _attr(element, name):
    try:
        err, value = _AS().AXUIElementCopyAttributeValue(element, name, None)
    except Exception:  # a dead element raises from the bridge: a miss, not a crash
        return None
    return value if err == 0 else None


def _label(element):
    """AXTitle on AppKit, AXDescription on web and Electron, a short AXValue as a last resort."""
    for name in ("AXTitle", "AXDescription"):
        text = _attr(element, name)
        if isinstance(text, str) and text.strip():
            return " ".join(text.split())
    value = _attr(element, "AXValue")
    if isinstance(value, str) and 0 < len(value.strip()) <= AX_VALUE_CHARS:
        return " ".join(value.split())
    return ""


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


def _actions(element):
    try:
        err, names = _AS().AXUIElementCopyActionNames(element, None)
    except Exception:
        return []
    return [str(n) for n in names] if err == 0 and names else []




def trusted():
    return bool(_AS().AXIsProcessTrusted())


def frontmost():
    """(pid, name, bundle id) of the frontmost app. HEYJEV_SCREEN_PID pins another app, for tests only."""
    if os.environ.get("HEYJEV_SCREEN_PID"):
        pid = int(os.environ["HEYJEV_SCREEN_PID"])
        return (pid, *_app_info(pid))
    from AppKit import NSWorkspace
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        raise Unavailable("no frontmost app")
    return int(app.processIdentifier()), str(app.localizedName() or ""), str(app.bundleIdentifier() or "")


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


def read_text(pid, frame, deadline):
    """[(text, confidence, (x, y, w, h) screen points)] for one window. Raises Unavailable."""
    import Quartz
    if not Quartz.CGPreflightScreenCaptureAccess():
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
        req.setRecognitionLevel_(0)  # accurate
        req.setUsesLanguageCorrection_(True)
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


def observe(pid=None, ocr=True, deadline=None):
    """Read one window. Raises Unavailable when it can't; never acts."""
    t0 = time.monotonic()
    deadline = deadline or t0 + 4.0
    if not trusted():
        raise Unavailable("accessibility_permission")
    if pid is None:
        pid, app, bundle = frontmost()
    else:
        app, bundle = _app_info(pid)
    AS = _AS()
    app_el = AS.AXUIElementCreateApplication(pid)
    AS.AXUIElementSetMessagingTimeout(app_el, AX_MESSAGE_TIMEOUT)
    win = _window(app_el)
    if win is None:
        raise Unavailable("no window")
    wframe = _frame(win)
    if not wframe:
        raise Unavailable("window has no frame")
    title = _label(win)
    left = max(0.05, min(ax_walk.AX_TIME_CAP, deadline - time.monotonic()))
    found, _offscreen, truncated = _walk(win, wframe, left)
    menubar = _attr(app_el, "AXMenuBar")
    bar = [ax_walk.AxNode(r, l, *f, True, k) for k in (_children(menubar) if menubar is not None else [])
           for r, l, f in [_attrs(k)] if l and f and r == "AXMenuBarItem" and l != "Apple"]
    t_ax = time.monotonic()
    texts, ocr_state = [], "off"
    if ocr:
        try:
            texts, ocr_state = read_text(pid, wframe, deadline), "ok"
        except Unavailable as exc:
            ocr_state = "no_permission" if str(exc) == "no_permission" else "failed"
    items = merge(bar + found, texts, wframe)
    snap = Snapshot(pid, app, bundle, title, wframe, items[:MAX_ITEMS], truncated or len(items) > MAX_ITEMS, ocr_state,
                    {"ax": round((t_ax - t0) * 1000), "ocr": round((time.monotonic() - t_ax) * 1000)})
    return snap


def remember(snap):
    global LAST
    with _lock:
        LAST = snap


def last():
    with _lock:
        return LAST


# --------------------------------------------------------------------------- acting and reading back
def press(ref):
    """AXPress. Returns the AX error code (0 is delivered)."""
    try:
        return int(_AS().AXUIElementPerformAction(ref, "AXPress"))
    except Exception:
        return -1


def element_state(ref):
    """What a press on this element might flip: value, selection, expansion."""
    return {k: repr(_attr(ref, k)) for k in ("AXValue", "AXSelected", "AXExpanded", "AXEnabled")}


def signature(pid):
    """AX-only fingerprint: window title, window count, focused element, open menus, and the control set."""
    AS = _AS()
    app_el = AS.AXUIElementCreateApplication(pid)
    AS.AXUIElementSetMessagingTimeout(app_el, AX_MESSAGE_TIMEOUT)
    win = _window(app_el)
    focused = _attr(app_el, "AXFocusedUIElement")
    sig = {"window": _label(win) if win is not None else None,
           "windows": len(_attr(app_el, "AXWindows") or []),
           "focused": (str(_attr(focused, "AXRole") or ""), _label(focused)) if focused is not None else None,
           "menu_open": any(_attr(k, "AXSelected") for k in _children(_attr(app_el, "AXMenuBar")))
           if _attr(app_el, "AXMenuBar") is not None else False}
    if win is not None:
        found, _, _ = _walk(win, _frame(win) or (0, 0, 0, 0), 0.4)
        sig["controls"] = sorted((c.role, c.label) for c in found)
        sig["text"] = _text_digest(win)
    return sig


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
