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


URL_SPAN = re.compile(r"\b(?:https?://\S+|(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?(?:/\S*)?)", re.I)


def url_span(clause):
    m = URL_SPAN.search(clause)
    return m[0].rstrip(".,!?") if m else ""


def app_name(clause):
    m = APP_SPAN.search(clause)
    return m[1].strip(" .,!?") if m else ""


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
        return (conf, f"{'spotify_volume' if spotify else 'volume'}.{act}", {"level": ans["volume_level"][0]})
    return (conf, f"{target}.{act}", {})


def pick(ans, clause):
    """Trust Jev's target if it is fairly sure, else the single most confident action anywhere."""
    target, tconf = ans["target"]
    s = step_for(ans, target, clause) if tconf >= 0.5 else None
    if s is None:
        cands = [x for x in (step_for(ans, t, clause) for t in TARGETS) if x]
        s = max(cands, key=lambda x: x[0]) if cands else None
    return s


def plan(text, classify, can_answer=False):
    """-> ("steps", [{"clause", "action", "args"}]) with action None for unsupported clauses,
          ("reply", key), ("answer", None) or ("clarify", reason)."""
    clauses = split_clauses(text)
    if not clauses:
        return ("clarify", "empty")
    if len(clauses) > MAX_CLAUSES:
        return ("clarify", "too_many_steps")
    if len(clauses) == 1:
        ans = classify(clauses[0])
        cat, cconf = ans["category"]
        if ans["target"][0] == "timer" and ans["target"][1] >= GATE and not ans["compound"][0]:
            s = step_for(ans, "timer", clauses[0])  # "how long is left?" reads like a question but is a timer command
            if s:
                return ("steps", [{"clause": clauses[0], "action": s[1], "args": s[2]}])
        if cat == "chit_chat" and cconf >= GATE:
            return ("reply", "chit_chat")
        if cat == "unclear" and cconf >= GATE:
            return ("clarify", "unclear")
        if cat == "information_request" and cconf >= GATE:
            return ("answer", None) if can_answer else ("reply", "info")
        if ans["compound"][0] and ans["compound"][1] >= GATE:
            return ("clarify", "compound_unsplit")  # say it as "X, then Y"
        s = pick(ans, clauses[0])
        if not s:
            return ("answer", None) if can_answer and cat == "information_request" else ("clarify", "no_action")
        return ("steps", [{"clause": clauses[0], "action": s[1], "args": s[2]}])
    steps = []
    for clause in clauses:
        s = pick(classify(clause), clause)
        steps.append({"clause": clause, "action": s[1] if s else None, "args": s[2] if s else {}})
    return ("steps", steps)
