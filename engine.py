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
        self.actions = actions
        self.effect_pending = pending  # an earlier effect that may still land holds every later dispatch
        self.instance = uuid.uuid4().hex[:12]
        self.lock = threading.Condition()
        self.ledger = {}  # id -> record
        self.queue = collections.deque()
        self.pending = None  # the one confirmation awaiting a decision
        self.running = None
        threading.Thread(target=self._worker, daemon=True).start()

    # ------------------------------------------------------------------ public API
    def submit(self, text, source, rid=None):
        """Reserve the id and enqueue. Returns a status dict immediately."""
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
            rec = {"id": rid, "sha": _sha(text), "text": text, "source": source, "state": "queued",
                   "queued_at": time.monotonic(), "done_at": None, "steps": [], "cancel": False}
            self.ledger[rid] = rec
            self.queue.append(rec)
            self.lock.notify_all()
            return self._view(rec)

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
                if self.pending and self.pending["id"] == rid and self.pending["decision"] is None:
                    self.pending["decision"] = "cancelled"
                self.lock.notify_all()
            return self._view(rec)

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
                diagnostics.record(rec["id"], "done", v["state"], (time.monotonic() - started) * 1000,
                                   **{k: v[k] for k in ("stopped_state", "uncertain_step", "not_started", "error", "detail")
                                      if k in v})
                _local.rid = None
                self._emit("done", v, None)

    def _run(self, rec):
        self._emit("start", self._view(rec), None)
        t = time.monotonic()
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
            try:
                said = self.answer(rec["text"])
            except Exception as exc:  # no effect was involved: a definite failure with the provider's reason
                with self.lock:
                    return self._finish(rec, "failed", error="answer_failed", detail=str(exc)[:300])
            with self.lock:
                return self._finish(rec, "answered", say=said)
        steps = [{"index": i, "clause": s["clause"], "action": s["action"], "state": "not_started",
                  "target": None, "facts": {}, "detail": None} for i, s in enumerate(payload)]
        with self.lock:
            rec["steps"] = steps
        for i, (step, planned) in enumerate(zip(steps, payload)):
            with self.lock:
                if rec["cancel"]:
                    return self._stop(rec, i, "cancelled")
            state = self._step(rec, step, planned["args"])
            if state != "completed":
                return self._stop(rec, i, state)
        with self.lock:
            self._finish(rec, "completed")

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

    def _step(self, rec, step, args):
        action = self.actions.get(step["action"]) if step["action"] else None
        if not action:
            self._set(step, state="unsupported")
            return "unsupported"
        t = time.monotonic()
        try:
            got = action["resolve"](args)
        except Exception as exc:  # nothing dispatched yet
            got = ("none", f"could not resolve: {exc}")
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
        if forced or target.get("confirm") or self.policy().get(action["effect"], "ask") == "ask":
            t = time.monotonic()
            verdict = self._confirm(rec, step, target)
            diagnostics.record(rec["id"], "confirm", verdict, (time.monotonic() - t) * 1000, step=step["index"],
                               prompt="<screen control>" if step["action"].startswith("screen.")
                               else describe(step["action"], target), forced=forced)
            if verdict != "confirmed":
                self._set(step, state="declined" if verdict != "cancelled" else "skipped", detail=verdict)
                return "declined" if verdict != "cancelled" else "cancelled"
            try:
                again = action["resolve"](args)
            except Exception:
                again = ("none", None)
            still = again[0] == "target" and _same_target(again[1], target) or \
                picked is not None and again[0] == "choices" and any(_same_target(c, target) for c in again[1])
            if not still:
                self._set(step, state="failed", detail="target_changed")
                return "failed"
        settled = self._settled(rec)
        if settled == "cancelled":
            self._set(step, state="skipped")
            return "cancelled"
        if not settled:  # an earlier step's effect is still out: nothing overtakes it
            self._set(step, state="failed", detail="an earlier action hasn't finished")
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
        state = self._execute(action, step, target)
        diagnostics.record(rec["id"], "verify", state, (time.monotonic() - started) * 1000, step=step["index"],
                           detail=step.get("detail"), facts=loggable(step["action"], step.get("facts") or {}),
                           target=loggable(step["action"], target))
        return state

    def _settled(self, rec):
        """Wait up to PENDING_WAIT for any outstanding effect to land. True: clear. False: still out, so this step must
        not run. "cancelled": the request was cancelled while waiting."""
        end = time.monotonic() + PENDING_WAIT
        while self.effect_pending():
            if rec["cancel"]:
                return "cancelled"
            if time.monotonic() >= end:
                return False
            time.sleep(POLL)
        return True

    def _execute(self, action, step, target):
        deadline = time.monotonic() + action["timeout"]
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

    def _confirm(self, rec, step, target):
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
