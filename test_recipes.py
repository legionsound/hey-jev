"""Named recipes: validation, PREFS storage, planner expansion, engine run. No real prefs."""
import json
import unittest
from unittest.mock import patch

import planner
import recipes


class FakeStore:
    def __init__(self):
        self.d = {}

    def stringForKey_(self, k):
        return self.d.get(k)

    def setObject_forKey_(self, v, k):
        self.d[k] = v


class RecipeTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.p = patch.object(recipes, "_store", return_value=self.store)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_save_get_case_insensitive(self):
        steps = [{"clause": "open Safari", "action": "app.open", "args": {"app": "Safari"}}]
        recipes.save("Focus", steps)
        self.assertEqual(recipes.get("focus"), steps)
        recipes.save("FOCUS", steps + steps[:1])
        self.assertEqual(len(recipes.get("focus")), 2)

    def test_validation(self):
        good = {"clause": "x", "action": "app.open", "args": {"app": "Safari"}}
        with self.assertRaises(ValueError):
            recipes.save("", [good])
        with self.assertRaises(ValueError):
            recipes.save("n", [])
        with self.assertRaises(ValueError):
            recipes.save("n", [dict(good)] * 6)
        with self.assertRaises(ValueError):
            recipes.save("n", [{"clause": "x", "action": "nope.nope", "args": {}}])
        with self.assertRaises(ValueError):
            recipes.save("n", [{"clause": "press 3", "action": "screen.press", "args": {}}])
        with self.assertRaises(ValueError):
            recipes.save("n", [{"clause": "x", "action": "app.open", "args": {"app": "element 3"}}])

    def test_delete(self):
        recipes.save("a", [{"clause": "x", "action": "app.open", "args": {"app": "S"}}])
        self.assertTrue(recipes.delete("A"))
        self.assertIsNone(recipes.get("a"))
        self.assertFalse(recipes.delete("a"))

    def test_planner_expands_bare_and_run(self):
        steps = [{"clause": "open Safari", "action": "app.open", "args": {"app": "Safari"}}]
        recipes.save("focus", steps)
        kind, got = planner.plan("focus", lambda c: (_ for _ in ()).throw(AssertionError("no classify")))
        self.assertEqual((kind, got), ("steps", steps))
        kind, got = planner.plan("run focus", lambda c: (_ for _ in ()).throw(AssertionError("no classify")))
        self.assertEqual((kind, got), ("steps", steps))

    def test_engine_runs_recipe_as_one_request(self):
        import engine as engine_mod
        from test_engine import act, Calls
        steps = [{"clause": "a", "action": "app.open", "args": {"app": "Safari"}},
                 {"clause": "b", "action": "app.open", "args": {"app": "Notes"}}]
        recipes.save("both", steps)
        calls = Calls()
        actions = {"app.open": act(calls, "app.open")}
        eng = engine_mod.Engine(classify=lambda c: (_ for _ in ()).throw(AssertionError()),
                                policy=lambda: {"open": "auto"}, actions=actions)
        with patch.object(engine_mod.planner, "plan", wraps=planner.plan) as spied:
            rec = eng.submit("run both", "cli")["id"]
            out = eng.wait(rec, 5)
        self.assertEqual(out["state"], "completed")
        self.assertEqual([s["action"] for s in out["steps"]], ["app.open", "app.open"])
        self.assertEqual(calls.runs, ["app.open", "app.open"])


if __name__ == "__main__":
    unittest.main()
