"""Named recipes: data-only multi-step shortcuts stored in shared prefs.

A recipe is a name plus an ordered list of command clauses using existing
actions. Recipes expand in the planner to steps and run through the engine
as one request. No search/typing support (typing is built separately).
"""
import json
import re

import model_settings

KEY = "named_recipes"
MAX_STEPS = 5
RUN_PREFIX = re.compile(r"^\s*run\s+(.+?)[\s.!?]*$", re.I)


def _store():
    return model_settings.PREFS


def all_recipes():
    try:
        raw = _store().stringForKey_(KEY)
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError, AttributeError):
        return {}


def get(name):
    data = all_recipes()
    key = (name or "").strip().lower()
    for k, v in data.items():
        if k.lower() == key:
            return [dict(s) for s in v]
    return None


def validate(name, steps):
    """Raises ValueError with a plain-words reason when invalid."""
    if not (name or "").strip():
        raise ValueError("Give the recipe a name.")
    if not isinstance(steps, list) or not steps:
        raise ValueError("A recipe needs at least one step.")
    if len(steps) > MAX_STEPS:
        raise ValueError(f"A recipe holds at most {MAX_STEPS} steps.")
    try:
        from actions import ACTIONS
    except ImportError:
        ACTIONS = {}
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            raise ValueError(f"Step {i + 1} is not a step.")
        action = s.get("action", "")
        args = s.get("args", {})
        clause = s.get("clause", "")
        if action.startswith("screen."):
            raise ValueError(f"Step {i + 1}: screen control is not allowed in recipes.")
        if isinstance(args, dict):
            for v in args.values():
                if isinstance(v, str) and re.search(r"screen\.press|element\s*#?\d+", v, re.I):
                    raise ValueError(f"Step {i + 1}: screen press by number is not allowed in recipes.")
        if action not in ACTIONS:
            raise ValueError(f"Step {i + 1}: unknown action {action!r}.")
        if not isinstance(args, dict) or not isinstance(clause, str) or not clause.strip():
            raise ValueError(f"Step {i + 1} needs a clause and args.")
    return True


def save(name, steps):
    validate(name, steps)
    data = all_recipes()
    # Replace case-insensitively so "Focus" and "focus" stay one recipe.
    for k in list(data.keys()):
        if k.lower() == name.strip().lower():
            del data[k]
    data[name.strip()] = [{"clause": s["clause"], "action": s["action"], "args": dict(s["args"])} for s in steps]
    _store().setObject_forKey_(json.dumps(data), KEY)
    return name.strip()


def delete(name):
    data = all_recipes()
    for k in list(data.keys()):
        if k.lower() == (name or "").strip().lower():
            del data[k]
            _store().setObject_forKey_(json.dumps(data), KEY)
            return True
    return False


def match(text):
    """-> steps list when text names a recipe, else None. 'run <name>' or a bare name.

    Stored steps are re-validated: a matching but corrupt record raises
    ValueError so the planner can clarify with zero dispatch."""
    for candidate in ((text or "").strip(), RUN_PREFIX.match(text or "") and RUN_PREFIX.match(text or "").group(1)):
        if not candidate:
            continue
        data = all_recipes()
        key = candidate.strip().lower()
        for k, v in data.items():
            if k.lower() == key:
                steps = [dict(s) for s in v] if isinstance(v, list) else None
                try:
                    validate(k, steps)
                except ValueError as e:
                    raise ValueError(f"Recipe {k!r} is invalid: {e}")
                except Exception:
                    raise ValueError(f"Recipe {k!r} is invalid: stored steps are corrupt.")
                return steps
    return None
