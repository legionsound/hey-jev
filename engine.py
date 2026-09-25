"""One serial engine for voice and CLI: queue, replay ledger, confirmation gate, step runner. See docs/ENGINE_CONTRACT.md."""
import collections
import hashlib
import threading
import time
import uuid

import diagnostics
import planner
from actions import ACTIONS, DEFAULT_POLICY, Failed, Timeout, describe, effect_pending, loggable

QUEUE_MAX = 8
QUEUE_TTL = 30.0
RESULT_TTL = 600.0
LEDGER_MAX = 10_000
CONFIRM_TTL = 60.0
POLL = 0.2
TIEBREAK_EFFECTS = ("open", "quit")  # app.open / app.quit: the duplicate-app chooser
ANSWER_WINDOW = 45.0  # seconds a "which one?" stays answerable
PENDING_WAIT = 10.0  # how long a new dispatch waits for an earlier, still-outstanding effect before refusing
_local = threading.local()


def current_rid():
    """The request id the calling worker thread is running, for the classifier's diagnostic records."""
    return getattr(_local, "rid", None)


TERMINAL = {"completed", "partial", "failed", "unverified", "unknown", "unsupported", "needs_clarification",
            "declined", "cancelled", "expired", "answered"}


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _same_target(a, b):
    """Resolve returns a fresh dict each time, so any difference means the target moved under the confirmation."""
    return a == b


class Engine:
    def __init__(self, classify, policy=lambda: DEFAULT_POLICY, ask=None, answer=None, on_event=None, actions=ACTIONS,
                 tiebreak=None, threshold=lambda: 0.85, pending=effect_pending):
        """classify(clause) -> Jev answers. ask(pending) shows the pop-down (None: no UI, Ask-first steps decline).
        answer(text) -> spoken answer, or None when deeper answers are off. on_event(kind, record, step).
        tiebreak(clause, choices) -> (index, score 0..1) or None: Jev's pick among duplicate targets, used only when
        the score reaches threshold(). It is a model score, not a correctness guarantee; quitting a pick always asks."""
        self.classify, self.policy, self.ask, self.answer = classify, policy, ask, answer
        self.tiebreak, self.threshold = tiebreak, threshold
        self.on_event = on_event or (lambda *a: None)
        self.interrupt_answer = None  # set by the app: stops an answer backend that is blocked mid-request
        self.offered = None  # the last "which one?" with exact targets: {"action", "choices", "until"}
        self.actions = actions
        self.effect_pending = pending  # an earlier effect that may still land holds every later dispatch
        self.task_jev = None  # (state_text, questions) -> answers: Jev for multi-step tasks, set by the app
        self.instance = uuid.uuid4().hex[:12]
        self.lock = threading.Condition()
        self.ledger = {}  # id -> record
        self.queue = collections.deque()
        self.pending = None  # the one confirmation awaiting a decision
        self.running = None
        threading.Thread(target=self._worker, daemon=True).start()

    # ------------------------------------------------------------------ public API
    def submit(self, text, source, rid=None, shown=None):
        """Reserve the id and enqueue. Returns a status dict immediately. shown: the numbered list's version on screen
        when the user spoke, so "click 3" means the 3 they saw."""
        rid = rid or uuid.uuid4().hex
        with self.lock:
            rec = self.ledger.get(rid)
            if rec:
                if rec["sha"] != _sha(text):
                    return self._error(rid, "id_conflict")
                return self._view(rec)
            if len(self.ledger) >= LEDGER_MAX:
                self._forget_results()
                return self._error(rid, "busy", "ledger_full")
            if len(self.queue) >= QUEUE_MAX:
                return self._error(rid, "busy", "queue_full")
            diagnostics.record(rid, "submit", "queued", source=source, text=text, queue_depth=len(self.queue))
            rec = {"id": rid, "sha": _sha(text), "text": text, "source": source, "state": "queued", "shown": shown,
                   "queued_at": time.monotonic(), "done_at": None, "steps": [], "cancel": False}
            self.ledger[rid] = rec
            self.queue.append(rec)
            self.lock.notify_all()
            return self._view(rec)

    def active(self):
        """Ids of the running request and every queued one, oldest first: what a spoken "stop" cancels."""
        with self.lock:
            return ([self.running["id"]] if self.running else []) + [r["id"] for r in self.queue]

    def status(self, rid):
        with self.lock:
            rec = self.ledger.get(rid)
            return self._view(rec) if rec else self._error(rid, "unknown_outcome", "id not seen since this app started")

    def wait(self, rid, timeout):
        end = time.monotonic() + timeout
        with self.lock:
            while True:
                rec = self.ledger.get(rid)
                if not rec or rec["state"] in TERMINAL:
                    break
                left = end - time.monotonic()
                if left <= 0:
                    break
                self.lock.wait(left)
        return self.status(rid)

    def cancel(self, rid):
        with self.lock:
            rec = self.ledger.get(rid)
            if not rec:
                return self._error(rid, "unknown_outcome", "id not seen since this app started")
            if rec["state"] == "queued":
                self.queue.remove(rec)
                self._finish(rec, "cancelled")
            elif rec["state"] == "running":
                rec["cancel"] = True  # checked before each step and consumes any open confirmation
                if rec.get("answering") and self.interrupt_answer:
                    self.interrupt_answer()  # a blocked answer backend is stopped, not waited out
                if self.pending and self.pending["id"] == rid and self.pending["decision"] is None:
                    self.pending["decision"] = "cancelled"
                self.lock.notify_all()
            return self._view(rec)

    def confirm_outside(self, text, source="agent", ttl=None):
        """The same pop-down for a request that isn't an engine step (an agent asking permission).
        -> "confirmed" | "declined" | "timed_out" | "busy" (another confirmation is open) | "no_confirmation_ui"."""
        if not self.ask:
            return "no_confirmation_ui"
        with self.lock:
            if self.pending is not None:
                return "busy"
            p = {"token": uuid.uuid4().hex, "id": None, "step": None, "decision": None, "text": text, "source": source}
            self.pending = p
        self.ask(dict(p))
        end = time.monotonic() + (ttl or CONFIRM_TTL)
        with self.lock:
            while p["decision"] is None:
                left = end - time.monotonic()
                if left <= 0:
                    p["decision"] = "timed_out"
                    break
                self.lock.wait(left)
            if self.pending is p:
                self.pending = None
            decision = p["decision"]
        self.ask(None)
        return decision

    def cancel_outside(self):
        """Stop: an open agent permission pop-down is declined."""
        with self.lock:
            if self.pending is not None and self.pending["id"] is None and self.pending["decision"] is None:
                self.pending["decision"] = "cancelled"
                self.lock.notify_all()

    def decide(self, token, confirmed):
        """Pop-down button. First decision wins; a late one is a no-op. Returns True when this call decided."""
        with self.lock:
            p = self.pending
            if not p or p["token"] != token or p["decision"] is not None:
                return False
            p["decision"] = "confirmed" if confirmed else "declined"
            self.lock.notify_all()
            return True

    def shutdown(self):
        """App quit: queued work is cancelled; a running step settles on its own deadline."""
        with self.lock:
            while self.queue:
                self._finish(self.queue.popleft(), "cancelled", detail="app_quit")
            if self.running:
                self.running["cancel"] = True
            if self.pending and self.pending["decision"] is None:
                self.pending["decision"] = "cancelled"
            self.lock.notify_all()

    # ------------------------------------------------------------------ ledger helpers
    def _view(self, rec):
        v = {k: rec[k] for k in ("id", "state", "source", "text") if rec.get(k) is not None}
        v.update(v=1, instance=self.instance, steps=[dict(s) for s in rec["steps"]])
        for k in ("stopped_state", "uncertain_step", "not_started", "reply", "say", "error", "detail"):
            if rec.get(k) is not None:
                v[k] = rec[k]
        return v

    def _error(self, rid, state, detail=None):
        return {"v": 1, "id": rid, "instance": self.instance, "state": state, "steps": [], "detail": detail}

    def _forget_results(self):
        """Drop full result bodies past RESULT_TTL; the small id record stays for the whole run."""
        now = time.monotonic()
        for rec in self.ledger.values():
            if rec["done_at"] and now - rec["done_at"] > RESULT_TTL and rec["steps"]:
                rec["steps"], rec["text"] = [], None

    def _finish(self, rec, state, **extra):
        rec.update(state=state, done_at=time.monotonic(), **extra)
        self.lock.notify_all()

    # ------------------------------------------------------------------ worker
    def _worker(self):
        while True:
            with self.lock:
                while not self.queue:
                    self.lock.wait()
                rec = self.queue.popleft()
                if time.monotonic() - rec["queued_at"] > QUEUE_TTL:
                    self._finish(rec, "expired")
                    continue
                rec["state"] = "running"
                self.running = rec
                self._forget_results()
            _local.rid, started = rec["id"], time.monotonic()
            try:
                self._run(rec)
            except Exception as exc:  # planner or classifier failure before any step ran
                with self.lock:
                    if rec["state"] == "running":
                        self._finish(rec, "failed" if not rec["steps"] else "unknown", error="internal", detail=str(exc))
            finally:
                with self.lock:
                    self.running = None
                v = self._view(rec)
                bad = next((st for st in v["steps"] if st["state"] not in ("completed", "skipped", "not_started")), None)
                diagnostics.record(rec["id"], "done", v["state"], (time.monotonic() - started) * 1000,
                                   step_detail=bad.get("detail") if bad else None,
                                   **{k: v[k] for k in ("stopped_state", "uncertain_step", "not_started", "error", "detail")
                                      if k in v})
                _local.rid = None
                self._emit("done", v, None)

    def _run(self, rec):
        self._emit("start", self._view(rec), None)
        t = time.monotonic()
        with self.lock:
            offered, self.offered = self.offered, None  # an answer is good once; any other command drops the question
        pick = None
        if offered and time.monotonic() < offered["until"]:
            pick = planner.answer_pick(rec["text"], [c["name"] for c in offered["choices"]])
        if pick is not None:
            kind, payload = "steps", [{"clause": rec["text"], "action": offered["action"],
                                       "args": {"pinned": offered["choices"][pick]["target"]}}]
        else:
            kind, payload = planner.plan(rec["text"], self.classify, can_answer=self.answer is not None)
        diagnostics.record(rec["id"], "plan", kind, (time.monotonic() - t) * 1000,
                           steps=[s["action"] for s in payload] if kind == "steps" else None,
                           detail=payload if kind != "steps" else None)
        with self.lock:
            if kind == "reply":
                return self._finish(rec, "answered", reply=payload)
            if kind == "clarify":
                return self._finish(rec, "needs_clarification", detail=payload)
        if kind == "answer":
            with self.lock:
                rec["answering"] = True
            try:
                said = self.answer(rec["text"])
            except Exception as exc:  # no effect was involved: a definite failure with the provider's reason
                with self.lock:
                    if rec["cancel"]:
                        return self._finish(rec, "cancelled")
                    return self._finish(rec, "failed", error="answer_failed", detail=str(exc)[:300])
            with self.lock:
                if rec["cancel"]:  # Stop arrived while it was thinking: the late answer is dropped, never spoken
                    return self._finish(rec, "cancelled")
                return self._finish(rec, "answered", say=said)
        if len(payload) == 1 and payload[0]["action"] == "task.run":
            return self._task(rec, payload[0])
        steps = [{"index": i, "clause": s["clause"], "action": s["action"], "state": "not_started",
                  "target": None, "facts": {}, "detail": None} for i, s in enumerate(payload)]
        with self.lock:
            rec["steps"] = steps
        for i, (step, planned) in enumerate(zip(steps, payload)):
            with self.lock:
                if rec["cancel"]:
                    return self._stop(rec, i, "cancelled")
            args = planned["args"]
            if "number" in args and rec["source"] == "voice":  # a spoken number is bound to what was displayed
                args = {**args, "shown": rec.get("shown") if rec.get("shown") is not None else "unbound"}
            state = self._step(rec, step, args)
            if state != "completed":
                return self._stop(rec, i, state)
        with self.lock:
            self._finish(rec, "completed")

    def _new_step(self, rec, clause, action, **extra):
        with self.lock:
            step = {"index": len(rec["steps"]), "clause": clause, "action": action, "state": "not_started",
                    "target": None, "facts": {}, "detail": None, **extra}
            rec["steps"].append(step)
            return step

    def _task(self, rec, planned):
        """A multi-step task: one confirmation for the task, then Jev decides each step on the pinned window.
        One deadline covers everything. Stops at the first step that isn't completed, on a stall, a repeat, the step
        or time budget, a changed foreground/window/field, "stop", or when Jev proposes done (verified only against
        an explicit "until you see …" that wasn't already on screen). Details never carry screen text."""
        import task
        import screen
        goal = planned["args"].get("goal", "")
        start = time.monotonic()
        end = start + task.BUDGET
        head = self._new_step(rec, planned["clause"], "task.run", target={"goal": goal})
        policy = self.policy()
        if policy.get("task", "ask") == "ask":
            verdict = self._confirm(rec, head, {"goal": goal, "steps": task.MAX_STEPS}, end)
            diagnostics.record(rec["id"], "confirm", verdict, step=0, prompt="<task>")
            if verdict != "confirmed":
                self._set(head, state="declined" if verdict != "cancelled" else "skipped", detail=verdict)
                with self.lock:
                    return self._finish(rec, "declined" if verdict != "cancelled" else "cancelled")
        covered = policy.get("in_task", "auto") == "auto"
        self._set(head, state="running")
        history, seen, idle, repeats, pinned, last_ok, typed, first = [], [], 0, 0, None, None, set(), None
        apps, expect = task.apps_in_goal(goal), None  # expect: the bundle our own verified open_app step brought up

        def finish(state, why):
            self._set(head, state=state, detail=why, facts={"steps": len(rec["steps"]) - 1})
            diagnostics.record(rec["id"], "task", state, (time.monotonic() - start) * 1000, detail=why,
                               steps=len(rec["steps"]) - 1)
            with self.lock:
                self._finish(rec, state, detail=why)

        def interrupted():
            """A reason to stop now, checked after every wait: stop, or the time budget."""
            with self.lock:
                if rec["cancel"]:
                    return ("cancelled", "you stopped it")
            if time.monotonic() >= end:
                return ("unverified", "time limit")
            return None

        for n in range(task.MAX_STEPS + 1):
            stop = interrupted()
            if stop:
                return finish(*stop)
            if n == task.MAX_STEPS:
                return finish("unverified", "step limit")
            try:
                if expect:  # we just opened this app: wait (briefly) for exactly it to come forward, then pin it
                    wait_until = min(end, time.monotonic() + 3)
                    while screen.frontmost()[2] != expect:
                        if time.monotonic() >= wait_until:
                            return finish("unverified", "the app I opened didn't come to the front")
                        time.sleep(0.1)
                    pinned, expect = None, None
                elif pinned and screen.frontmost()[0] != pinned[0]:
                    return finish("unverified", "another app came forward")
                snap = screen.observe(pid=pinned[0] if pinned else None, ocr=True,
                                      deadline=min(end, time.monotonic() + 4))
            except screen.Unavailable as exc:  # our own reason strings, never screen text
                why = str(exc)
                if pinned is None and apps and not history:
                    snap = None  # nothing readable yet (Hey Jev's own window, say): opening the goal's app may help
                else:
                    if "Hey Jev is in front" in why:
                        why = "Hey Jev's own window was in front; switch to the app first"
                    return finish("failed", f"couldn't read the screen: {why}")
            except Exception as exc:
                return finish("failed", f"couldn't read the screen ({type(exc).__name__})")
            stop = interrupted()
            if stop:
                return finish(*stop)
            if snap is None:
                pass  # no window to pin yet
            elif pinned is None:
                pinned = (snap.pid, snap.started, snap.window_token)
            elif (snap.pid, snap.started) != pinned[:2]:
                return finish("unverified", "the app changed under me")
            elif snap.window_token != pinned[2]:  # never adopted: a change can't be proven to be ours
                return finish("unverified", "the window changed")
            items = task.shareable(snap)[0] if snap is not None else []
            first = first if first is not None else items
            sig = task.signature(snap, items) if snap is not None else ("none",)
            if last_ok is not None:
                idle = idle + 1 if sig == last_ok else 0
                if idle >= task.MAX_IDLE:
                    return finish("unverified", "stuck: nothing changed")
            tried = [a for s_, a in seen if s_ == sig]
            if snap is not None:
                screen.remember(snap)  # a press or type by number resolves against exactly this list
            field = self._task_field(snap, end) if snap is not None else None
            got = self._ask_jev(rec, min(end, time.monotonic() + task.JEV_TIMEOUT), task.decide,
                                self.task_jev, goal, snap, items, history, tried, field, typed, apps)
            stop = interrupted()  # stop or the deadline wins over any answer, late or failed
            if stop:
                return finish(*stop)
            if got[0] == "timeout":
                return finish("unverified", "Jev didn't answer in time")
            if got[0] == "error":
                return finish("failed", f"Jev couldn't decide ({got[1]})")
            kind, conf, item = got[1]
            diagnostics.record(rec["id"], "task_decide", kind, step=n, confidence=round(conf, 2))
            stop = interrupted()  # a stop or the deadline during the Jev call wins over its answer
            if stop:
                return finish(*stop)
            if kind == "invalid":
                return finish("unverified", "Jev's answer didn't fit this screen")
            if conf < task.GATE:
                return finish("unverified", "not sure what to do next")
            if kind == "stuck":
                return finish("unverified", "nothing here helps")
            if kind == "done":
                check = task.postcondition(goal, items, first)
                if check:
                    return finish("completed", "done, checked on screen")
                return finish("unverified", "Jev judged it done; not checked" if check is None
                              else "Jev judged it done, but the screen doesn't show it")
            if kind == "open_app":
                what = ("app.open", {"app": item.get("name")})
            elif kind == "press_item":
                what = ("screen.press", {"number": item.n})
            elif kind == "type_text":
                what = ("screen.type", {"text": task.typed_text(goal), "number": item.n})
            else:
                what = ("screen.submit", {"element": field["token"]})
            desc = (f"open_app:{item.get('bundle_id')}" if kind == "open_app"
                    else f"{kind}:{item.token if item else field['token'] if field else ''}")
            if desc in tried:
                repeats += 1
                if repeats >= task.MAX_REPEATS:
                    return finish("unverified", "going in circles")
            else:
                repeats = 0
            seen.append((sig, desc))
            decided = (snap.pid, snap.window_token) if snap is not None else None

            def still_pinned(decided=decided):
                """Just before dispatch: the same app in front, the same window, as when Jev decided."""
                try:
                    if screen.frontmost()[0] != decided[0]:
                        return "another app came forward"
                    if screen.current_window(decided[0], min(end, time.monotonic() + 1)) != decided[1]:
                        return "the window changed under me"
                except Exception:
                    return "couldn't re-check the screen"
                return None
            step = self._new_step(rec, kind, what[0])
            state = self._step(rec, step, what[1], covered=covered, until=end,
                               check=still_pinned if decided is not None else None)
            history.append({"open_app": f"opened {item.get('name')}" if kind == "open_app" else "",
                            "press_item": f"pressed '{item.label}'" if item and kind == "press_item" else "pressed",
                            "type_text": f"typed the quoted text into '{item.label}'" if item and kind == "type_text" else "typed",
                            "submit": "pressed Return"}[kind] + (" (checked)" if state == "completed" else f" ({state})"))
            if state != "completed":  # never retried: a press that may have landed stays as it is
                why = {"unverified": "a step went through but couldn't be checked",
                       "unknown": "a step may not have gone through",
                       "failed": "a step couldn't be done",
                       "declined": "you said no to the next step",
                       "needs_clarification": "the next step was ambiguous",
                       "cancelled": "you stopped it"}.get(state, f"a step ended {state}")
                return finish("cancelled" if state == "cancelled" else state if state == "failed" else "unverified", why)
            if kind == "type_text":
                typed.add(item.token)
            if kind == "open_app":
                expect = (step.get("target") or {}).get("bundle_id") or item.get("bundle_id")  # only this app may be adopted
            last_ok = sig

    def _ask_jev(self, rec, deadline, fn, *args):
        """Run a Jev call on its own thread and wait for it, the deadline, or a stop, whichever comes first.
        -> ("ok", result) | ("timeout", None) | ("cancelled", None) | ("error", name). A call that outlives the wait is
        abandoned, tracked with the other abandoned reads, and its answer ignored; nothing claims it stopped."""
        import screen
        box = {}

        def run():
            try:
                box["ok"] = fn(*args)
            except BaseException as exc:
                box["err"] = type(exc).__name__
        t = threading.Thread(target=run, daemon=True)
        t.start()
        while t.is_alive():
            with self.lock:
                if rec["cancel"]:
                    break
            if time.monotonic() >= deadline:
                break
            t.join(0.05)
        if t.is_alive():
            with screen._abandoned_lock:
                screen._abandoned.append((t, False))
            with self.lock:
                return ("cancelled", None) if rec["cancel"] else ("timeout", None)
        if "err" in box:
            return ("error", box["err"])
        return ("ok", box["ok"])

    def _task_field(self, snap, end):
        """The focused field, when text could go into it: {"confirm": accepts Return} or None."""
        import screen
        try:
            deadline = min(end, time.monotonic() + 1.5)
            ref = screen.focused_field(snap.pid, deadline)
            if ref is None:
                return None
            f = screen.field_facts(ref, deadline)
            if f["secure"] or not f["enabled"] or not f["insertable"] or f["window"] != snap.window_token:
                return None
            return {"confirm": screen.can_confirm(ref, deadline), "token": screen.token(ref)}
        except Exception:
            return None

    def _stop(self, rec, i, state):
        with self.lock:
            steps = rec["steps"]
            for s in steps[i + 1:]:
                s["state"] = "skipped"
            done = sum(s["state"] == "completed" for s in steps)
            extra = {"not_started": [s["clause"] for s in steps[i + 1:]]}
            if steps[i]["state"] in ("unknown", "unverified"):
                extra["uncertain_step"] = {"index": i, "state": steps[i]["state"]}
            if state == "cancelled":
                return self._finish(rec, "cancelled", **extra)
            if done:
                return self._finish(rec, "partial", stopped_state=state, **extra)
            self._finish(rec, state, **extra)

    def _emit(self, kind, view, step):
        """UI and speech hooks never break the engine."""
        try:
            self.on_event(kind, view, step)
        except Exception as exc:
            print(f"  on_event {kind} failed: {exc!r}")

    def _set(self, step, **kw):
        with self.lock:
            step.update(kw)

    def _step(self, rec, step, args, covered=False, until=None, check=None):
        """covered: a step inside a task whose OK the user gave, when the in-task setting lets that OK cover it.
        until: the task's shared deadline, which caps confirmation, waits and execution. check(): the task's own
        re-check just before dispatch (foreground, window, field); a reason string stops the step undispatched."""
        action = self.actions.get(step["action"]) if step["action"] else None
        if not action:
            self._set(step, state="unsupported")
            return "unsupported"
        t = time.monotonic()
        import actions as actions_mod
        actions_mod.RESOLVE_UNTIL = until  # resolvers cap their own budget by the task's shared deadline
        try:
            got = action["resolve"](args)
        except Exception as exc:  # nothing dispatched yet
            got = ("none", f"could not resolve: {exc}")
        finally:
            actions_mod.RESOLVE_UNTIL = None
        act = step["action"]
        screen_step = act.startswith("screen.")
        diagnostics.record(rec["id"], "resolve", got[0], (time.monotonic() - t) * 1000, step=step["index"],
                           action=act, args=loggable(act, args),
                           target=loggable(act, got[1]) if got[0] == "target" else None,
                           choices=(len(got[1]) if screen_step else [c.get("path") or c.get("name") for c in got[1]])
                           if got[0] == "choices" else None,
                           reason=got[1] if got[0] == "none" else None)
        picked = None
        if got[0] == "choices" and action["effect"] not in TIEBREAK_EFFECTS:  # only duplicate apps are Jev's to break
            self._set(step, state="needs_clarification", facts={"choices": got[1]})
            if got[1] and all(isinstance(c.get("target"), dict) for c in got[1]):  # "the first one" can answer it
                with self.lock:
                    self.offered = {"action": act, "choices": got[1], "until": time.monotonic() + ANSWER_WINDOW}
            return "needs_clarification"
        if got[0] == "choices":
            t = time.monotonic()
            picked, facts = self._break_tie(step, got[1])
            diagnostics.record(rec["id"], "tiebreak", "picked" if picked else "asked", (time.monotonic() - t) * 1000,
                               step=step["index"], picked=picked.get("path") if picked else None, **facts)
            if picked is None:
                self._set(step, state="needs_clarification", facts={"choices": got[1], **facts})
                return "needs_clarification"
            got = ("target", picked)
            self._set(step, facts=facts)
        if got[0] == "none":
            self._set(step, state="failed", detail=got[1], facts={"error": "not_found"})
            return "failed"
        target = got[1]
        self._set(step, target=target)
        forced = picked is not None and action["effect"] == "quit"  # a guessed quit target always asks
        policy = self.policy()
        risky = target.get("confirm") and policy.get("risky", "ask") == "ask"
        if forced or risky or (policy.get(action["effect"], "ask") == "ask" and not covered):
            t = time.monotonic()
            verdict = self._confirm(rec, step, target, until)
            diagnostics.record(rec["id"], "confirm", verdict, (time.monotonic() - t) * 1000, step=step["index"],
                               prompt="<screen control>" if step["action"].startswith("screen.")
                               else describe(step["action"], target), forced=forced)
            if verdict != "confirmed":
                self._set(step, state="declined" if verdict != "cancelled" else "skipped", detail=verdict)
                return "declined" if verdict != "cancelled" else "cancelled"
            actions_mod.RESOLVE_UNTIL = until
            try:
                again = action["resolve"](args)
            except Exception:
                again = ("none", None)
            finally:
                actions_mod.RESOLVE_UNTIL = None
            still = again[0] == "target" and _same_target(again[1], target) or \
                picked is not None and again[0] == "choices" and any(_same_target(c, target) for c in again[1])
            if not still:
                self._set(step, state="failed", detail="target_changed")
                return "failed"
        settled = self._settled(rec, until)
        if settled == "cancelled":
            self._set(step, state="skipped")
            return "cancelled"
        if not settled:  # an earlier step's effect is still out: nothing overtakes it
            self._set(step, state="failed", detail="an earlier action hasn't finished")
            return "failed"
        if until is not None and time.monotonic() >= until:
            self._set(step, state="failed", detail="time limit")
            return "failed"
        why = check() if check else None
        if why:
            self._set(step, state="failed", detail=why)
            return "failed"
        with self.lock:  # dispatch boundary: a cancel that lands before this line stops the step, after it cannot
            if rec["cancel"]:
                step["state"] = "skipped"
                return "cancelled"
            step["state"] = "running"
        self._emit("step", self._view(rec), dict(step))
        diagnostics.record(rec["id"], "dispatch", "running", step=step["index"], action=step["action"],
                           target=loggable(step["action"], target))
        started = time.monotonic()
        state = self._execute(action, step, target, until)
        diagnostics.record(rec["id"], "verify", state, (time.monotonic() - started) * 1000, step=step["index"],
                           detail=step.get("detail"), facts=loggable(step["action"], step.get("facts") or {}),
                           target=loggable(step["action"], target))
        return state

    def _settled(self, rec, until=None):
        """Wait up to PENDING_WAIT for any outstanding effect to land. True: clear. False: still out, so this step must
        not run. "cancelled": the request was cancelled while waiting."""
        end = time.monotonic() + PENDING_WAIT
        if until is not None:
            end = min(end, until)
        while self.effect_pending():
            if rec["cancel"]:
                return "cancelled"
            if time.monotonic() >= end:
                return False
            time.sleep(POLL)
        return True

    def _execute(self, action, step, target, until=None):
        deadline = time.monotonic() + action["timeout"]
        if until is not None:
            deadline = min(deadline, until)
        try:
            action["run"](target, deadline)
        except Failed as exc:
            self._set(step, state="failed", detail=str(exc))
            return "failed"
        except Exception as exc:  # Timeout, or anything unexpected after dispatch: may or may not have happened
            self._set(step, state="unknown", detail=str(exc) or type(exc).__name__)
            return "unknown"
        if action["verify"] is None:
            self._set(step, state="unverified", detail=action["proves"])
            return "unverified"
        facts = {}
        while True:
            try:
                verdict, facts = action["verify"](target, deadline)
            except Failed as exc:
                verdict, facts = "wait", {"read_error": str(exc)}
            except Exception as exc:
                self._set(step, state="unknown", facts=facts, detail=str(exc) or type(exc).__name__)
                return "unknown"
            if verdict != "wait":
                state = {"done": "completed"}.get(verdict, verdict)
                self._set(step, state=state, facts=facts)
                return state
            if time.monotonic() + POLL >= deadline:
                self._set(step, state="unknown", facts=facts, detail="no readback before deadline")
                return "unknown"
            time.sleep(POLL)

    def _break_tie(self, step, choices):
        """-> (chosen target, facts) or (None, facts). Anything malformed or failing keeps the choices explicit."""
        if not self.tiebreak:
            return None, {}
        try:
            need = float(self.threshold())
            if not 0 <= need <= 1:  # Always ask (inf) or a bad setting: never consult the model
                return None, {"tiebreak": {"skipped": "always_ask"}}
            got = self.tiebreak(step["clause"], choices)
            if not isinstance(got, tuple) or len(got) != 2:
                raise ValueError(f"malformed pick {got!r}")
            i, score = got
            if isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < len(choices):
                raise ValueError(f"bad index {i!r}")
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1:
                raise ValueError(f"bad score {score!r}")  # NaN and infinity fail the range check too
        except Exception as exc:
            return None, {"tiebreak": {"error": str(exc) or type(exc).__name__}}
        facts = {"tiebreak": {"score": round(float(score), 3), "threshold": need}}
        return (dict(choices[i]) if score >= need else None), facts

    def _confirm(self, rec, step, target, until=None):
        if not self.ask:
            return "no_confirmation_ui"
        with self.lock:
            if rec["cancel"]:
                return "cancelled"
            p = {"token": uuid.uuid4().hex, "id": rec["id"], "step": step["index"], "decision": None,
                 "text": describe(step["action"], target), "source": rec["source"]}
            self.pending = p
            step["state"] = "awaiting_confirmation"
        self.ask(dict(p))
        end = time.monotonic() + CONFIRM_TTL
        if until is not None:
            end = min(end, until)
        with self.lock:
            while p["decision"] is None:
                left = end - time.monotonic()
                if left <= 0:
                    p["decision"] = "timed_out"
                    break
                self.lock.wait(left)
            self.pending = None
            decision = p["decision"]
        if self.ask:
            self.ask(None)  # close the pop-down
        return decision
