"""Live inspection: see what Hey Jev reads, numbered, as the screen changes.

While on, a bounded background observer reads the frontmost window about once a second (never while a command is
running), keeps each item's number for as long as that exact element stays in view, and hands the UI a view of
every item: its number, where it came from (Accessibility, text read off the pixels, or both), whether it can be
pressed, and whether it would be shared with Jev in a task. Each refresh becomes the list "click N" refers to, so the
number on screen is the number a command resolves, then re-checked against the live element before anything happens.

Nothing here acts, sends or logs screen text. Results from a refresh that finishes after the observer was stopped
(or restarted) are dropped by generation.
"""
import itertools
import threading
import time

import screen
import task

PERIOD = 1.0
READ_BUDGET = 1.5
_ops = itertools.count(1)  # generation numbers, never reused


class Numbering:
    """Stable numbers: an element keeps its number while it stays in view; new elements get the next free number;
    a number isn't reused until numbering restarts (the app or window changes)."""

    def __init__(self):
        self.scope, self.numbers, self.next = None, {}, 1

    def apply(self, snap):
        scope = (snap.pid, snap.started, snap.window_token)
        if scope != self.scope:
            self.scope, self.numbers, self.next = scope, {}, 1
        seen = set()
        for i in snap.items:
            key = i.token if i.ref is not None else ("ocr", i.label, tuple(round(v) for v in i.frame))
            if key not in self.numbers:
                self.numbers[key] = self.next
                self.next += 1
            i.n = self.numbers[key]
            seen.add(key)
        for key in [k for k in self.numbers if k not in seen]:  # gone from view: its number retires
            del self.numbers[key]
        return snap


class Inspector:
    def __init__(self, show, busy=lambda: False, observe=None):
        """show(view or None): called with each fresh view, and None when stopped. busy(): a command is running."""
        self.show, self.busy = show, busy
        self.observe = observe or (lambda deadline: screen.observe(ocr=True, deadline=deadline))
        self.gen, self.thread, self.numbering = 0, None, Numbering()
        self.lock = threading.Lock()

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive() and self.gen != 0

    def start(self):
        with self.lock:
            self.gen = next(_ops)
            gen = self.gen
            self.numbering = Numbering()
        self.thread = threading.Thread(target=self._loop, args=(gen,), daemon=True)
        self.thread.start()

    def stop(self):
        with self.lock:
            self.gen = 0  # any refresh still in flight is now stale
        self.show(None)

    def _current(self, gen):
        with self.lock:
            return gen == self.gen

    def _loop(self, gen):
        while self._current(gen):
            t0 = time.monotonic()
            if not self.busy():
                view = self.refresh(gen)
                if view is not None and self._current(gen):
                    self.show(view)
            time.sleep(max(0.05, PERIOD - (time.monotonic() - t0)))

    def refresh(self, gen):
        """One bounded read. -> the view, or None when stale, unreadable, out of time, or a command started meanwhile.
        Publishing (numbers, the list "click N" resolves) happens under the same lock as stop(), and only while this
        generation is current and no command is running, so nothing stale is ever installed."""
        t0 = time.monotonic()
        try:
            snap = self.observe(time.monotonic() + READ_BUDGET)
        except Exception as exc:
            return {"gen": gen, "error": type(exc).__name__, "ms": round((time.monotonic() - t0) * 1000)}
        observed_at = time.time()  # when the read finished: the age shown counts from here
        shared, _ = task.shareable(snap)
        shared_ids = {id(i) for i in shared}
        with self.lock:
            if gen != self.gen or self.busy():
                return None  # stopped, restarted, or a command began while this read was out: drop it
            self.numbering.apply(snap)
            version = screen.remember(snap)
        return {"gen": gen, "version": version, "app": snap.app, "at": observed_at,
                "ms": round((time.monotonic() - t0) * 1000), "complete": snap.walk_complete, "truncated": snap.truncated,
                "items": [{**i.public(), "shared": id(i) in shared_ids,
                           "field": i.role in task.FIELD_ROLES or i.secure} for i in snap.items]}

    def current(self, gen):
        """For the UI: is a view from this generation still the one to paint?"""
        with self.lock:
            return gen != 0 and gen == self.gen
