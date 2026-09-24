"""Text to an ordered plan. Jev picks the action type per clause; Python pulls the arguments out of the words."""
import re

GATE = 0.65
MAX_CLAUSES = 5

QUESTIONS = {
    "category": {"type": "choice", "instructions": "What kind of request is this?",
                 "criteria": {"mac_command": "asks the computer to do something",
                              "information_request": "asks a general knowledge or factual question",
                              "chit_chat": "just talking, greeting, or thanking",
                              "unclear": "garbled, empty, or makes no sense"}},
    "compound": {"type": "noul", "instructions": "Does the request contain more than one distinct action?"},
    "target": {"type": "choice", "instructions": "What is the primary thing being controlled?",
               "criteria": {"app": "an application", "volume": "sound level", "display": "screen appearance or dark mode",
                            "media": "music playback", "system": "locking or sleeping the computer",
                            "timer": "setting, checking, or cancelling a timer or reminder",
                            "website": "going to a website or web address"}},
    "app_action": {"type": "choice", "instructions": "What should happen to the app?",
                   "criteria": {"open": "open, launch, or start the app itself", "quit": "quit, close, or kill the app",
                                "none": "the request is about playback, volume, or something inside the app, not opening or quitting it"}},
    "volume_action": {"type": "choice", "instructions": "What should happen to the volume, if anything?",
                      "criteria": {"up": None, "down": None, "mute": None, "unmute": None,
                                   "set": "set to a specific level", "none": None}},
    "volume_scope": {"type": "choice", "instructions": "Which volume should change?",
                     "criteria": {"spotify": "Spotify's own in-app volume when Spotify is explicitly named",
                                  "system": "the Mac's overall output volume, including unqualified volume requests"}},
    "volume_level": {"type": "score", "instructions": "If a volume level is asked for, how loud?",
                     "criteria": ["silent", "quiet", "medium", "loud", "max"]},
    "display_action": {"type": "choice", "instructions": "What should happen to dark mode?",
                       "criteria": {"dark_on": None, "dark_off": None, "toggle": None, "none": None}},
    "media_action": {"type": "choice", "instructions": "What should happen to music playback?",
                     "criteria": {"play": None, "pause": None, "next": None, "previous": None, "none": None}},
    "timer_action": {"type": "choice", "instructions": "What should happen with a timer or reminder?",
                     "criteria": {"set": "start a timer or set a reminder", "check": "ask how much time is left",
                                  "cancel": "stop or cancel a timer", "none": None}},
    "system_action": {"type": "choice", "instructions": "What should happen to the computer?",
                      "criteria": {"lock": None, "sleep": None, "none": None}},
}
TARGETS = ("app", "website", "volume", "display", "media", "system", "timer")
BRANCH = {"volume": "volume_action", "display": "display_action", "media": "media_action",
          "system": "system_action", "timer": "timer_action"}

# "then" style joins only. A bare "and" is never a split point: "rock and roll" stays whole.
SPLIT = re.compile(r"\s*(?:,\s*and\s+then|\band\s+then|,\s*then|\bthen|,\s*after\s+that|\bafter\s+that|,\s*and)\b\s*", re.I)
PROTECT = re.compile(r'"[^"]*"|“[^”]*”|\b[a-z][a-z0-9+.-]*://\S+?(?=[.,!?]*(?:\s|$))', re.I)
APP_SPAN = re.compile(r"\b(?:open|launch|start|run|quit|close|kill|exit)\s+(?:up\s+)?(?:the\s+)?(?:app\s+)?(.+?)"
                      r"(?:\s+(?:app|application))?(?:\s+(?:please|for me|now))*[\s.!?]*$", re.I)


def split_clauses(text):
    """Ordered clauses. Quoted text and URLs are never split. Duplicates and order are kept."""
    held = []
    masked = PROTECT.sub(lambda m: held.append(m[0]) or f"\x00{len(held) - 1}\x00", text)
    parts = [p.strip(" ,.") for p in SPLIT.split(masked)]
    return [re.sub(r"\x00(\d+)\x00", lambda m: held[int(m[1])], p) for p in parts if p.strip(" ,.")]


URL_SPAN = re.compile(r"\b(?:https?://\S+|(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?(?:[/?#]\S*)?)", re.I)


def url_span(clause):
    m = URL_SPAN.search(clause)
    return m[0].rstrip(".,!?") if m else ""


def app_name(clause):
    m = APP_SPAN.search(clause)
    return m[1].strip(" .,!?") if m else ""


UNITS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                     "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
TENS = {w: 10 * i for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split(), 2)}
# Everything that can be part of a spoken number, so the match takes the whole phrase, never just its tail.
NUM_WORD = r"(?:" + "|".join([*UNITS, *TENS, "a", "and", "hundred", "thousand", "minus", "negative", "point"]) + r")"
PERCENT = re.compile(r"(?:[-+\u2212]\s*)?(?:\d[\d.,]*|" + NUM_WORD + r"(?:[\s-]+" + NUM_WORD + r")*)\s*(?:%|\bper\s*cent\b)", re.I)


def words_value(words):
    """Plain whole numbers only: "seventy five", "a hundred", "one hundred and five". None for anything else."""
    total, cur, seen = 0, 0, False
    for w in words:
        if w in UNITS or w in TENS:
            cur, seen = cur + UNITS.get(w, TENS.get(w)), True
        elif w == "hundred":
            cur, seen = (cur or 1) * 100, True
        elif w == "thousand":
            total, cur, seen = total + (cur or 1) * 1000, 0, True
        elif w not in ("a", "and"):
            return None  # minus, negative, point
    return total + cur if seen else None


def percent(clause):
    """None when no percent is spoken, else (value or None when invalid, relative). Valid = whole number 0..100.
    The whole number phrase is parsed: "-10", "12.5", "two hundred" and "one hundred and five" are invalid, not 10/5/100/5."""
    m = PERCENT.search(clause)
    if not m:
        return None
    phrase = re.sub(r"\s*(?:%|per\s*cent)$", "", m[0], flags=re.I).strip().lower()
    words = re.split(r"[\s-]+", phrase)
    while words and words[0] in ("a", "and") and words[1:2] != ["hundred"]:
        words.pop(0)  # "to a" / "and" before the number are not part of it
    relative = bool(re.search(r"\bby\s*$", clause[:m.start()], re.I))
    if re.fullmatch(r"\d+", phrase):
        n = int(phrase)
    else:
        n = None if re.search(r"\d", phrase) else words_value(words)  # signs, decimals, "1,5"
    return (n if n is not None and 0 <= n <= 100 else None, relative)


def step_for(ans, target, clause):
    """One (confidence, action, args) for this target, or None when Jev is not sure."""
    if target == "app":
        act, conf = ans["app_action"]
        name = app_name(clause)
        if act == "none" or conf < GATE or not name:
            return None
        return (conf, f"app.{act}", {"app": name})
    if target == "website":
        url = url_span(clause)
        return (ans["target"][1], "url.open", {"url": url}) if url else None
    act, conf = ans[BRANCH[target]]
    if act == "none" or conf < GATE:
        return None
    if target == "timer":
        return (conf, f"timer.{act}", {"text": clause})
    if target == "volume":
        scope, scope_conf = ans["volume_scope"]
        spotify = scope == "spotify" and (scope_conf >= 0.5 or re.search(r"\bspotify\b", clause, re.I))
        kind = 'spotify_volume' if spotify else 'volume'
        pct = percent(clause) if act in ("set", "up", "down") else None
        if pct is not None:  # a spoken number beats Jev's five-word scale
            n, relative = pct
            if n is None or relative and act == "set":
                return (conf, "clarify", "bad_percent")  # never swap in a guessed level
            if relative:  # "up by 10 percent": move by exactly that much
                return (conf, f"{kind}.{act}", {"delta": n if act == "up" else -n})
            return (conf, f"{kind}.set", {"level": f"{n}%", "percent": n})  # "up to 60%" is a set
        return (conf, f"{kind}.{act}", {"level": ans["volume_level"][0]})
    return (conf, f"{target}.{act}", {})


def pick(ans, clause):
    """Trust Jev's target if it is fairly sure, else the single most confident action anywhere."""
    target, tconf = ans["target"]
    s = step_for(ans, target, clause) if tconf >= 0.5 else None
    if s is None:
        cands = [x for x in (step_for(ans, t, clause) for t in TARGETS) if x]
        s = max(cands, key=lambda x: x[0]) if cands else None
    return s


def judge(ans, clause, can_answer=False):
    """One clause -> ("step", step) | ("reply", key) | ("answer", None) | ("clarify", reason)."""
    cat, cconf = ans["category"]
    if ans["target"][0] == "timer" and ans["target"][1] >= GATE and not ans["compound"][0]:
        s = step_for(ans, "timer", clause)  # "how long is left?" reads like a question but is a timer command
        if s:
            return ("step", {"clause": clause, "action": s[1], "args": s[2]})
    if cat == "chit_chat" and cconf >= GATE:
        return ("reply", "chit_chat")
    if cat == "unclear" and cconf >= GATE:
        return ("clarify", "unclear")
    if cat == "information_request" and cconf >= GATE:
        return ("answer", None) if can_answer else ("reply", "info")
    if ans["compound"][0] and ans["compound"][1] >= GATE:
        return ("clarify", "compound_unsplit")  # say it as "X, then Y"
    s = pick(ans, clause)
    if s and s[1] == "clarify":
        return ("clarify", s[2])
    if not s:
        return ("answer", None) if can_answer and cat == "information_request" else ("clarify", "no_action")
    return ("step", {"clause": clause, "action": s[1], "args": s[2]})


def plan(text, classify, can_answer=False):
    """-> ("steps", [{"clause", "action", "args"}]), ("reply", key), ("answer", None) or ("clarify", reason).
    Every clause is judged before anything runs; one unclear clause stops the whole request."""
    clauses = split_clauses(text)
    if not clauses:
        return ("clarify", "empty")
    if len(clauses) > MAX_CLAUSES:
        return ("clarify", "too_many_steps")
    if len(clauses) == 1:
        kind, got = judge(classify(clauses[0]), clauses[0], can_answer)
        return ("steps", [got]) if kind == "step" else (kind, got)
    steps = []
    for clause in clauses:
        kind, got = judge(classify(clause), clause)
        if kind != "step":
            return ("clarify", got if kind == "clarify" else "no_action")
        steps.append(got)
    return ("steps", steps)
