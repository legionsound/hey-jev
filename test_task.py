"""Multi-step tasks through the real engine, with the screen and Jev faked: gates, stops and what Jev is shown."""
import json
import unittest
from unittest.mock import patch

import actions
import diagnostics
import planner
import screen
import task
from engine import Engine


def item(n, label, source="ax", role="AXButton", frame=(10, 10, 80, 30), pressable=True, **kw):
    return screen.Item(n, source, role, label, frame, pressable, ref=kw.pop("ref", None) or object(), **kw)


WIN = object()


def snap(items, window=WIN, pid=7, started="s", vouched=True):
    """vouched: Accessibility declares the whole window ordinary text, so OCR lines may be shared (unless in a field)."""
    return screen.Snapshot(pid, "Pad", "com.pad", "Pad", (0, 0, 400, 300), items, window_ref=window, started=started,
                           text_frames=[(0, 0, 400, 300)] if vouched else [])


class Screen:
    """A scripted screen: each observe returns the next snapshot (the last one repeats). Presses flip a value."""

    def __init__(self, test, snaps):
        self.snaps, self.i, self.presses, self.state = list(snaps), 0, [], {"exists": "yes", "AXValue": "0"}
        self.front = None  # None: whatever app the current snapshot belongs to
        fns = {"observe": self.observe, "signature": lambda pid, d: {"n": self.i},
               "element_state": lambda ref, d: dict(self.state), "press": self.press,
               "focused_field": lambda pid, d: None, "process_start": lambda pid, d: "s",
               "frontmost": lambda: (self.front or self.current().pid, "Pad", "com.pad"),
               "current_window": lambda pid, d: self.current().window_token}
        for name, fn in fns.items():
            p = patch.object(screen, name, fn)
            p.start()
            test.addCleanup(p.stop)

    def current(self):
        return self.snaps[min(self.i, len(self.snaps) - 1)]

    def observe(self, pid=None, ocr=True, deadline=None):
        return self.current()

    def press(self, ref, deadline):
        self.presses.append(ref)
        self.state = {**self.state, "AXValue": str(len(self.presses))}
        self.i += 1  # the next observation shows the next screen
        return 0


class TaskTests(unittest.TestCase):
    def setUp(self):
        for p in (patch.object(diagnostics, "record", lambda *a, **k: None),):
            p.start()
            self.addCleanup(p.stop)
        self.asked, self.sent, self.jev_override = [], [], None

    def run_task(self, goal, answers, policy=None, cancel_after=None):
        plan = [{"clause": f"take over: {goal}", "action": "task.run", "args": {"goal": goal}}]
        replies = iter(answers)

        def jev(state, questions):
            self.sent.append((json.loads(state), questions))
            if cancel_after is not None and len(self.sent) > cancel_after:
                eng.cancel(rid)
            override = getattr(self, "jev_override", None)
            return override(state, questions) if override else next(replies)

        def ask(pending):
            if pending:
                self.asked.append(pending["text"])
                eng.decide(pending["token"], True)
        with patch.object(planner, "plan", lambda *a, **k: ("steps", plan)):
            eng = Engine(lambda _: {}, policy=lambda: {**actions.DEFAULT_POLICY, **(policy or {})}, ask=ask)
            eng.task_jev = jev
            self.eng = eng
            rid = self.rid = eng.submit(f"take over: {goal}", "cli")["id"]
            return eng.wait(rid, 20)

    def test_take_over_with_no_goal_asks_for_one(self):
        import siri
        self.assertEqual(planner.plan("take over", lambda c: self.fail("no classification")), ("clarify", "task_no_goal"))
        self.assertIn("Take over what", siri.line_for({"state": "needs_clarification", "steps": [],
                                                       "detail": "task_no_goal"}))

    def test_the_reason_the_screen_couldnt_be_read_is_kept(self):
        Screen(self, [snap([item(1, "Next")])])
        with patch.object(screen, "observe", lambda **k: (_ for _ in ()).throw(
                screen.Unavailable("Hey Jev is in front and no app handed off to it"))):
            v = self.run_task("go on", [])
        self.assertEqual(v["steps"][0]["detail"],
                         "couldn't read the screen: Hey Jev's own window was in front; switch to the app first")

    def test_the_words_that_start_a_task(self):
        self.assertEqual(task.goal_of("take over: find the cheapest flight"), "find the cheapest flight")
        self.assertEqual(task.goal_of("Work on turn on Loud mode."), "turn on Loud mode")
        for said in ["find the cheapest flight", "take over", "open Notes", "work on"]:
            self.assertIsNone(task.goal_of(said), said)
        kind, steps = planner.plan("take over: find a flight, then book it", lambda _: {})
        self.assertEqual((kind, steps[0]["action"], steps[0]["args"]), ("steps", "task.run", {"goal": "find a flight, then book it"}))

    def test_one_ok_for_the_task_then_jev_drives_verified_steps(self):
        loud = item(1, "Loud mode", role="AXCheckBox")
        Screen(self, [snap([loud]), snap([loud])])
        v = self.run_task("turn on Loud mode", [{"kind": ("press_item", 0.95), "item": ("i0", 0.9)},
                                                {"kind": ("done", 0.9)}])
        self.assertEqual(self.asked, ["Work on: turn on Loud mode (up to 15 steps)"])  # one OK; the press was covered
        self.assertEqual([s["state"] for s in v["steps"]], ["unverified", "completed"])
        self.assertEqual((v["state"], v["steps"][0]["detail"]), ("unverified", "Jev judged it done; not checked"))

    def test_done_is_verified_only_against_an_explicit_outcome_that_appeared(self):
        saved = item(1, "Saved", source="ocr", role="text", pressable=False)
        Screen(self, [snap([item(1, "Save")]), snap([saved])])
        v = self.run_task('save it until you see "Saved"', [{"kind": ("press_item", 0.9), "item": ("i0", 0.9)},
                                                            {"kind": ("done", 0.9)}])
        self.assertEqual((v["state"], v["steps"][0]["detail"]), ("completed", "done, checked on screen"))

    def test_text_that_was_already_there_proves_nothing(self):
        draft = item(1, "Draft", source="ocr", role="text", pressable=False)
        Screen(self, [snap([draft])])
        v = self.run_task('delete "Draft"', [{"kind": ("done", 0.9)}])  # a quoted name isn't a presence goal
        self.assertEqual(v["state"], "unverified")
        Screen(self, [snap([item(1, "Saved", source="ocr", role="text", pressable=False)])])
        v = self.run_task('save it until you see "Saved"', [{"kind": ("done", 0.9)}])  # already on screen at start
        self.assertEqual((v["state"], v["steps"][0]["detail"]), ("unverified", "Jev judged it done; not checked"))

    def test_evidence_is_a_whole_label_never_a_fragment(self):
        self.assertFalse(task.present("Saved", [item(1, "Not Saved")]))
        self.assertFalse(task.present("Saved file", [item(1, "Saved"), item(2, "file")]))  # not joined across items
        self.assertTrue(task.present("Saved", [item(1, "saved.")]))
        self.assertIsNone(task.postcondition('wait until you see "Saved"', [item(1, "Saved")], [item(1, "Saved")]))
        self.assertFalse(task.postcondition('wait until you see "Saved"', [item(1, "Not Saved")], []))

    def test_stop_during_a_hung_jev_call_is_immediate_and_cancelled(self):
        import time
        Screen(self, [snap([item(1, "Next")])])

        def hang(*a):
            time.sleep(1.0)
            return {"kind": ("done", 0.9)}
        self.jev_override = hang
        with patch.object(task, "JEV_TIMEOUT", 0.25):
            import threading
            threading.Timer(0.1, lambda: self.eng.cancel(self.rid)).start()
            t = time.monotonic()
            v = self.run_task("go on", [])
        self.assertEqual(v["state"], "cancelled")
        self.assertLess(time.monotonic() - t, 0.6)

    def test_the_deadline_and_stop_win_over_a_slow_jev(self):
        import time
        s = Screen(self, [snap([item(1, "Next")])])
        slow = [{"kind": ("press_item", 0.9), "item": ("i0", 0.9)}]

        def late(*a):
            time.sleep(0.2)
            return slow[0]
        with patch.object(task, "BUDGET", 0.1):
            self.jev_override = late
            v = self.run_task("go on", slow)
        self.assertEqual((v["state"], s.presses), ("unverified", []))
        self.assertIn(v["steps"][0]["detail"], ("time limit", "Jev didn't answer in time"))

    def test_a_jev_that_never_answers_is_abandoned_in_time(self):
        import time
        s = Screen(self, [snap([item(1, "Next")])])
        with patch.object(task, "JEV_TIMEOUT", 0.2):
            self.jev_override = lambda *a: time.sleep(5)
            t = time.monotonic()
            v = self.run_task("go on", [])
        self.assertLess(time.monotonic() - t, 2)
        self.assertEqual((v["steps"][0]["detail"], s.presses), ("Jev didn't answer in time", []))

    def test_stop_during_a_delayed_done_reply(self):
        import time
        Screen(self, [snap([item(1, "Next")])])

        def reply(*a):
            self.eng.cancel(self.rid)
            time.sleep(0.05)
            return {"kind": ("done", 0.99)}
        self.jev_override = reply
        v = self.run_task("go on", [])
        self.assertEqual(v["state"], "cancelled")

    def test_another_app_in_front_while_jev_decides_means_zero_effects(self):
        s = Screen(self, [snap([item(1, "Next")])])

        def reply(*a):
            s.front = 999  # the user switched apps during the model call
            return {"kind": ("press_item", 0.9), "item": ("i0", 0.9)}
        self.jev_override = reply
        v = self.run_task("go on", [])
        self.assertEqual((v["steps"][1]["detail"], s.presses), ("another app came forward", []))

    def test_an_unnamed_field_the_control_walk_skips_still_excludes_its_text(self):
        class N:
            def __init__(self, role, frame, kids=(), sub=None):
                self.role, self.frame, self.kids, self.sub = role, frame, list(kids), sub
        field = N("AXTextField", (10, 100, 300, 30))  # no label: walk_actionable never returns it
        win = N("AXWindow", (0, 0, 400, 300), [field])
        reads = {"AXRole": lambda n: ("ok", n.role), "AXSubrole": lambda n: ("absent", None),
                 "AXChildren": lambda n: ("ok", n.kids) if n.kids else ("absent", None)}
        with patch.object(screen, "_read", lambda el, name: reads[name](el)), \
                patch.object(screen, "_frame", lambda el: el.frame):
            frames, complete, _texts = screen.scan_fields(win)
        self.assertEqual((frames, complete), ([(10, 100, 300, 30)], True))
        bad = N("AXWindow", (0, 0, 400, 300), [N("AXGroup", (0, 0, 1, 1))])
        reads["AXRole"] = lambda n: ("unknown", None) if n.role == "AXGroup" else ("ok", n.role)
        with patch.object(screen, "_read", lambda el, name: reads[name](el)), \
                patch.object(screen, "_frame", lambda el: el.frame):
            self.assertFalse(screen.scan_fields(bad)[1])  # an unreadable node: fields can't all be known
        reads["AXRole"] = lambda n: ("ok", n.role)
        reads["AXChildren"] = lambda n: ("unknown", None) if n.role == "AXGroup" else (("ok", n.kids) if n.kids
                                                                                       else ("absent", None))
        hidden = N("AXWindow", (0, 0, 400, 300), [N("AXGroup", (0, 0, 400, 300), [N("AXTextField", (1, 1, 50, 20))])])
        with patch.object(screen, "_read", lambda el, name: reads[name](el)), \
                patch.object(screen, "_frame", lambda el: el.frame):
            self.assertFalse(screen.scan_fields(hidden)[1])  # an unreadable child list could hide a field
        reads["AXChildren"] = lambda n: ("ok", n.kids) if n.kids else ("absent", None)
        reads["AXSubrole"] = lambda n: ("unknown", None) if n.role == "AXTextField" else ("absent", None)
        with patch.object(screen, "_read", lambda el, name: reads[name](el)), \
                patch.object(screen, "_frame", lambda el: el.frame):
            self.assertFalse(screen.scan_fields(win)[1])  # an unreadable subrole could be a password field

    def test_a_line_straddling_a_text_region_or_in_a_broad_container_stays_local(self):
        line = item(2, "Public title PRIVATE OUTSIDE REGION", source="ocr", role="text", pressable=False,
                    frame=(0, 10, 200, 20))
        s1 = snap([item(1, "Next"), line], vouched=False)
        s1.text_frames = [(90, 0, 20, 40)]  # its centre is vouched for, most of it isn't
        Screen(self, [s1])
        self.run_task("go on", [{"kind": ("stuck", 0.9)}])
        self.assertNotIn("PRIVATE", json.dumps(self.sent[0][0]))
        self.assertNotIn("AXRow", screen.TEXT_ROLES)  # a row or cell can hold a field's text: never proof
        self.assertNotIn("AXCell", screen.TEXT_ROLES)
        inside = item(2, "Saved", source="ocr", role="text", pressable=False, frame=(92, 12, 16, 14))
        s2 = snap([item(1, "Next"), inside], vouched=False)
        s2.text_frames = [(90, 10, 20, 20)]
        self.sent.clear()
        Screen(self, [s2])
        self.run_task("go on", [{"kind": ("stuck", 0.9)}])
        self.assertIn("Saved", json.dumps(self.sent[0][0]))

    def test_ocr_nothing_vouches_for_stays_local(self):
        loose = item(2, "PRIVATE words", source="ocr", role="text", pressable=False, frame=(10, 200, 200, 20))
        Screen(self, [snap([item(1, "Next"), loose], vouched=False)])  # e.g. an app exposing no text regions
        self.run_task("go on", [{"kind": ("stuck", 0.9)}])
        self.assertNotIn("PRIVATE", json.dumps(self.sent[0][0]))

    def test_ocr_is_withheld_when_fields_cant_all_be_known(self):
        text = item(2, "account number 1234", source="ocr", role="text", pressable=False, frame=(10, 200, 200, 20))
        cut = snap([item(1, "Next"), text])
        cut.walk_complete = False  # the AX walk hit its cap: an unseen field could be anywhere
        Screen(self, [cut])
        self.run_task("go on", [{"kind": ("stuck", 0.9)}])
        self.assertNotIn("1234", json.dumps(self.sent[0][0]))
        beyond = snap([item(1, "Next"), item(2, "secret words", source="ocr", role="text", pressable=False,
                                              frame=(10, 300, 100, 20))])
        beyond.field_frames = [(0, 290, 300, 40)]  # a field past the item cap still excludes its text
        self.sent.clear()
        Screen(self, [beyond])
        self.run_task("go on", [{"kind": ("stuck", 0.9)}])
        self.assertNotIn("secret", json.dumps(self.sent[0][0]))

    def test_screen_text_stays_out_of_details_and_speech(self):
        import siri
        s = Screen(self, [snap([item(1, "Private Folder")])])
        s.press = lambda ref, d: (s.presses.append(ref), 0)[1]  # unverified press
        patch.object(screen, "press", s.press).start()
        logged = []
        with patch.object(diagnostics, "record", lambda *a, **k: logged.append(k)):
            v = self.run_task("go on", [{"kind": ("press_item", 0.9), "item": ("i0", 0.9)}])
        self.assertNotIn("Private", v["steps"][0]["detail"] + siri.line_for(v) + repr(logged))

    def test_steps_ask_each_time_when_the_task_ok_does_not_cover_them(self):
        a = item(1, "Next")
        Screen(self, [snap([a]), snap([a])])
        self.run_task("go on", [{"kind": ("press_item", 0.9), "item": ("i0", 0.9)}, {"kind": ("done", 0.9)}],
                      policy={"in_task": "ask"})
        self.assertEqual(len(self.asked), 2)  # the task, and the click

    def test_risky_buttons_ask_inside_tasks_unless_turned_off(self):
        buy = item(1, "Buy now")
        Screen(self, [snap([buy]), snap([buy])])
        self.run_task("get it", [{"kind": ("press_item", 0.9), "item": ("i0", 0.9)}, {"kind": ("done", 0.9)}])
        self.assertEqual(len(self.asked), 2)
        self.asked.clear()
        Screen(self, [snap([buy]), snap([buy])])
        self.run_task("get it", [{"kind": ("press_item", 0.9), "item": ("i0", 0.9)}, {"kind": ("done", 0.9)}],
                      policy={"risky": "auto", "task": "auto"})
        self.assertEqual(self.asked, [])  # everything automatic, as Johnny can choose

    def test_a_step_that_is_not_verified_ends_the_task_and_is_never_retried(self):
        b = item(1, "Next")
        s = Screen(self, [snap([b])])
        s.press = lambda ref, d: (s.presses.append(ref), 0)[1]  # nothing about the control changes
        patch.object(screen, "press", s.press).start()
        v = self.run_task("go on", [{"kind": ("press_item", 0.9), "item": ("i0", 0.9)}] * 5)
        self.assertEqual((v["state"], len(s.presses)), ("unverified", 1))
        self.assertIn("couldn't be checked", v["steps"][0]["detail"])

    def test_unsure_invalid_and_stuck_all_stop(self):
        for answers, why in [([{"kind": ("press_item", 0.4), "item": ("i0", 0.9)}], "not sure what to do next"),
                             ([{"kind": ("press_item", 0.9), "item": ("i9", 0.9)}], "Jev's answer didn't fit this screen"),
                             ([{"kind": ("fly", 0.9)}], "Jev's answer didn't fit this screen"),
                             ([{"kind": ("done", float("inf"))}], "Jev's answer didn't fit this screen"),
                             ([{"kind": ("stuck", 0.9)}], "nothing here helps")]:
            s = Screen(self, [snap([item(1, "Next"), item(2, "Back")])])  # two candidates, so Jev picks the item
            v = self.run_task("go on", answers)
            self.assertEqual((v["steps"][0]["detail"], s.presses), (why, []), answers)

    def test_ocr_text_is_context_never_a_press_target(self):
        Screen(self, [snap([item(1, "Pay", source="ocr", role="text", pressable=False), item(2, "Next"),
                            item(3, "Back")])])
        v = self.run_task("go on", [{"kind": ("press_item", 0.9), "item": ("i0", 0.9)}])
        self.assertEqual(v["steps"][0]["detail"], "Jev's answer didn't fit this screen")
        crit = self.sent[0][1]["item"]["criteria"]
        self.assertEqual(list(crit), ["i1", "i2"])

    def test_field_text_read_off_the_pixels_is_never_sent(self):
        field = item(1, "Message", role="AXTextField", pressable=False, frame=(10, 100, 300, 30))
        leak = item(2, "my password is hunter2", source="ocr", role="text", pressable=False, frame=(20, 105, 200, 20))
        Screen(self, [snap([field, leak, item(3, "Next")])])
        self.run_task("go on", [{"kind": ("stuck", 0.9)}])
        self.assertNotIn("hunter2", json.dumps(self.sent[0][0]))

    def test_a_window_the_user_brings_forward_stops_the_task(self):
        s = Screen(self, [snap([item(1, "Next")]), snap([item(1, "Next")], window=object())])
        v = self.run_task("go on", [{"kind": ("press_item", 0.9), "item": ("i0", 0.9)}, {"kind": ("done", 0.9)}])
        self.assertEqual((v["steps"][0]["detail"], len(s.presses)), ("the window changed", 1))

    def test_a_window_change_alongside_a_verified_step_never_authorizes_more(self):
        # the checkbox really changed, and meanwhile another window of the same app came forward
        other = object()
        s = Screen(self, [snap([item(1, "Loud mode", role="AXCheckBox")]), snap([item(1, "Delete all")], window=other)])
        patch.object(screen, "signature", lambda pid, d: {"window": WIN if not s.presses else other}).start()
        v = self.run_task("go on", [{"kind": ("press_item", 0.9), "item": ("i0", 0.9)}] * 3)
        self.assertEqual((v["steps"][0]["detail"], len(s.presses)), ("the window changed", 1))

    def test_another_app_coming_forward_stops_the_task(self):
        s = Screen(self, [snap([item(1, "Next")]), snap([item(1, "Other")])])
        real = s.observe

        def observe(pid=None, ocr=True, deadline=None):  # task observations after the first step see another app
            got = real(pid, ocr, deadline)
            return snap(got.items, started="other") if ocr and s.presses else got
        with patch.object(screen, "observe", observe):
            v = self.run_task("go on", [{"kind": ("press_item", 0.9), "item": ("i0", 0.9)}, {"kind": ("done", 0.9)}])
        self.assertEqual((v["steps"][0]["detail"], len(s.presses)), ("the app changed under me", 1))

    def test_going_in_circles_stops(self):
        a = item(1, "Toggle")
        s = Screen(self, [snap([a])])
        s.press = lambda ref, d: (s.presses.append(ref), s.state.update(AXValue=str(len(s.presses))), 0)[2]
        patch.object(screen, "press", s.press).start()
        v = self.run_task("go on", [{"kind": ("press_item", 0.9), "item": ("i0", 0.9)}] * 6)
        self.assertIn(v["steps"][0]["detail"], ("going in circles", "stuck: nothing changed"))
        self.assertLess(len(s.presses), 6)

    def test_step_limit(self):
        s = Screen(self, [snap([item(1, f"Next {k}")]) for k in range(1, 40)])
        with patch.object(task, "MAX_STEPS", 3):
            v = self.run_task("go on", [{"kind": ("press_item", 0.9), "item": ("i0", 0.9)}] * 10)
        self.assertEqual((v["steps"][0]["detail"], len(s.presses)), ("step limit", 3))

    def test_stop_mid_task(self):
        s = Screen(self, [snap([item(1, f"Next {k}")]) for k in range(1, 40)])
        v = self.run_task("go on", [{"kind": ("press_item", 0.9), "item": ("i0", 0.9)}] * 10, cancel_after=1)
        self.assertEqual(v["state"], "cancelled")
        self.assertLessEqual(len(s.presses), 1)

    def test_declining_the_task_does_nothing(self):
        s = Screen(self, [snap([item(1, "Next")])])
        plan = [{"clause": "take over: go on", "action": "task.run", "args": {"goal": "go on"}}]
        with patch.object(planner, "plan", lambda *a, **k: ("steps", plan)):
            eng = Engine(lambda _: {}, policy=lambda: dict(actions.DEFAULT_POLICY),
                         ask=lambda p: p and eng.decide(p["token"], False))
            eng.task_jev = lambda *a: self.fail("Jev must not be asked")
            v = eng.wait(eng.submit("take over: go on", "cli")["id"], 10)
        self.assertEqual((v["state"], s.presses), ("declined", []))



SETTINGS = {"name": "System Settings", "path": "/System/Applications/System Settings.app",
            "bundle_id": "com.apple.systempreferences"}


class OpenAppTests(unittest.TestCase):
    """A task can open the app its goal names, then carries on only in exactly that app."""
    run_task = TaskTests.run_task

    def setUp(self):
        TaskTests.setUp(self)
        import app_catalog
        old = app_catalog._inventory
        app_catalog._set_inventory([SETTINGS, {"name": "Notes", "path": "/Apps/Notes.app", "bundle_id": "com.notes"}])
        self.addCleanup(setattr, app_catalog, "_inventory", old)
        self.opened, self.front_after_open = [], SETTINGS["bundle_id"]
        fake = actions.entry("open", lambda a: ("target", dict(SETTINGS)) if a.get("app") == "System Settings"
                             else ("none", "no such app"),
                             lambda t, d: self.opened.append(t["bundle_id"]), lambda t, d: ("done", {}), "running")
        p = patch.dict(actions.ACTIONS, {"app.open": fake})
        p.start()
        self.addCleanup(p.stop)

    def screen(self, after_open):
        s = Screen(self, [after_open])

        def observe(pid=None, ocr=True, deadline=None):
            if not self.opened:
                raise screen.Unavailable("Hey Jev is in front and no app handed off to it")
            return after_open
        front = lambda: (99, "Hey Jev", "com.heyjev") if not self.opened else (after_open.pid, after_open.app,
                                                                                 self.front_after_open)
        for name, fn in (("observe", observe), ("frontmost", front)):
            p = patch.object(screen, name, fn)
            p.start()
            self.addCleanup(p.stop)
        return s

    def settings_snap(self):
        dark = item(1, "Dark", role="AXRadioButton")
        return screen.Snapshot(40, "System Settings", SETTINGS["bundle_id"], "Appearance", (0, 0, 400, 300), [dark],
                               window_ref=WIN, started="s", text_frames=[])

    def test_apps_the_goal_names(self):
        self.assertEqual([a["name"] for a in task.apps_in_goal("turn on dark mode in system settings")],
                         ["System Settings"])
        self.assertEqual([a["name"] for a in task.apps_in_goal("write it in Notes")], ["Notes"])
        self.assertEqual(task.apps_in_goal("note it"), [])  # whole names only, never a fragment

    def test_from_hey_jevs_own_window_it_opens_the_named_app_and_carries_on_there(self):
        s = self.screen(self.settings_snap())
        v = self.run_task("turn on dark mode in System Settings",
                          [{"kind": ("open_app", 0.9), "app": ("a0", 0.9)},
                           {"kind": ("press_item", 0.9), "item": ("i0", 0.9)}, {"kind": ("done", 0.9)}])
        first = self.sent[0]
        self.assertEqual(set(first[1]["kind"]["criteria"]), {"open_app", "done", "stuck"})  # nothing on screen to press
        self.assertEqual(first[0]["app"], None)
        self.assertEqual(self.opened, [SETTINGS["bundle_id"]])
        self.assertEqual(len(s.presses), 1)  # the press landed in System Settings
        self.assertNotIn("open_app", self.sent[1][1]["kind"]["criteria"])  # it's in front now
        self.assertEqual([(st["action"], st["state"]) for st in v["steps"]][:3],
                         [("task.run", "unverified"), ("app.open", "completed"), ("screen.press", "completed")])

    def test_a_different_app_coming_forward_after_the_open_stops(self):
        self.front_after_open = "com.other"
        s = self.screen(self.settings_snap())
        with patch.object(time_mod(), "sleep", lambda _: None):
            v = self.run_task("turn on dark mode in System Settings",
                              [{"kind": ("open_app", 0.9), "app": ("a0", 0.9)},
                               {"kind": ("press_item", 0.9), "item": ("i0", 0.9)}])
        self.assertEqual((v["steps"][0]["detail"], s.presses), ("the app I opened didn't come to the front", []))

    def test_with_no_app_named_an_unreadable_start_still_fails(self):
        self.screen(self.settings_snap())
        v = self.run_task("go on", [])
        self.assertEqual(self.sent, [])
        self.assertIn("couldn't read the screen", v["steps"][0]["detail"])


def time_mod():
    import time
    return time

if __name__ == "__main__":
    unittest.main()
