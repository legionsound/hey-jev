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
                            "website": "going to a website or web address",
                            "screen": "a button, menu, link, tab, text field or other control inside the window on "
                                      "screen, or what is on the screen"}},
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
    "screen_action": {"type": "choice", "instructions": "What should happen on the screen, if anything?",
                      "criteria": {"list": "a question about what is visible or available on screen",
                                   "press": "an instruction to click, press or select a particular named or numbered thing",
                                   "type": "an instruction to type, write or enter given words into a text field or box",
                                   "none": "neither"}},
    "system_action": {"type": "choice", "instructions": "What should happen to the computer?",
                      "criteria": {"lock": None, "sleep": None, "none": None}},
}
TARGETS = ("app", "website", "volume", "display", "media", "system", "timer", "screen")
BRANCH = {"volume": "volume_action", "display": "display_action", "media": "media_action",
          "system": "system_action", "timer": "timer_action", "screen": "screen_action"}

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

# Explicit browser choice. Supported names map to bundle ids; any other
# trailing "in <Word>" after a URL or site name clarifies with zero
# navigation, so there is no blacklist to maintain. Anything else is prose.
BROWSERS = {"safari": "com.apple.Safari", "chrome": "org.google.Chrome",
            "google chrome": "org.google.Chrome"}
BROWSER_QUAL = re.compile(r"\bin\s+(?:(?:my|the|your)\s+)?([A-Za-z][A-Za-z0-9]*)(?:\s+([A-Za-z][A-Za-z0-9]*))?(?:\s+browsers?)?", re.I)
# Grammar, never a browser name: "log in to google.com" must not clarify.
QUAL_STOPWORDS = frozenset({"to", "the", "a", "an", "my", "your", "this", "that", "its"})


def browser_qualifier(clause):
    """-> (clause without the qualifier, bundle id or None, unsupported name or None).

    Only the recognized qualifier is removed, so the URL's path, query
    and fragment survive untouched. A trailing "in <Word>" only counts as
    a browser choice when the rest still names a URL or site ("search in
    page", "example.com/in-depth" stay prose). Later matches win, so an
    earlier grammatical "in" ("log in to ...") never masks a trailing
    browser clause."""
    matches = list(BROWSER_QUAL.finditer(clause))
    if not matches:
        return clause, None, None
    for m in reversed(matches):
        w1, w2 = m.group(1).lower(), (m.group(2) or "").lower()
        if w2 in ("browser", "browsers"):
            w2 = ""
        if w1 in QUAL_STOPWORDS:
            continue
        if w2 and f"{w1} {w2}" in BROWSERS:
            name, end = f"{w1} {w2}", m.end()
        elif w1 in BROWSERS:
            name, end = w1, m.end(1)
        else:
            name, end = (f"{w1} {w2}" if w2 else w1), m.end()
            rest = re.sub(r"\s+", " ", (clause[:m.start()] + " " + clause[end:])).strip(" ,.")
            if url_span(rest) or site_url(rest):
                return clause, None, name
            continue
        rest = re.sub(r"\s+", " ", (clause[:m.start()] + " " + clause[end:])).strip(" ,.")
        if not (url_span(rest) or site_url(rest)):
            continue
        return rest, BROWSERS[name], None
    return clause, None, None


def app_browser(name):
    """Bundle id when an opened app is a supported browser, else None."""
    n = (name or "").strip().lower()
    if n in ("safari",):
        return BROWSERS["safari"]
    if n in ("chrome", "google chrome"):
        return BROWSERS["chrome"]
    return None


def url_span(clause):
    m = URL_SPAN.search(clause)
    return m[0].rstrip(".,!?") if m else ""


SITE_SPAN = re.compile(r"\b(?:go|navigate|head|take\s+me)\s+to\s+(?:the\s+)?(.+?)"
                       r"(?:\s+(?:website|web\s*site|site|homepage|page))?(?:\s+(?:please|for me|now))*[\s.!?]*$", re.I)


def site_url(clause):
    """A curated site for a spoken name with no dot ("go to YouTube"), or "". Unknown names are never guessed."""
    import url_adapter
    m = SITE_SPAN.search(clause)
    return url_adapter.site_for_name(m[1].strip(" .,!?")) or "" if m else ""


PRESS_SPAN = re.compile(r"\b(?:click|press|tap|hit|choose|select|pick)\s+(?:on\s+)?(?:the\s+)?(.+?)"
                        r"(?:\s+(?:button|menu|link|tab|item|checkbox|option|icon|toggle))?(?:\s+(?:please|for me|now))*[\s.!?]*$",
                        re.I)
NUMBER = re.compile(r"^(?:number\s+|item\s+|#\s*)?(\d{1,3}|[a-z]+(?:[\s-][a-z]+)?)$", re.I)


def press_args(clause):
    """{"number": n} for "click 12" / "press number twelve", {"label": "Share"} for a name, None when nothing is named."""
    m = PRESS_SPAN.search(clause)
    if not m:
        return None
    said = m[1].strip(" .,!?\"“”'")
    n = NUMBER.match(said)
    if n:
        word = n[1].lower()
        value = int(word) if word.isdigit() else words_value(word.replace("-", " ").split())
        if value is not None:
            return {"number": value}
    return {"label": said} if said else None


TYPE_SPAN = re.compile(r"^\W*(?:please\s+)?(?:type|write|enter|input)\s+(?P<rest>.+)$", re.I)
FIELD_TAIL = re.compile(r"\s+(?:into\s+(?:the\s+)?(?P<f1>.+?)(?:\s+(?:field|box|bar|area))?"
                        r"|in\s+(?:the\s+)?(?P<f2>.+?)\s+(?:field|box|bar|area))[\s.!?]*$", re.I)
QUOTED = re.compile(r'^\s*["“](?P<text>[^"”]*)["”](?P<after>.*)$')


def type_args(clause):
    """{"text": literal, "field": name or absent} for "type hello into the Search field", None when there is no text.
    Quoted text is taken exactly. Unquoted text loses only a trailing full stop, which recognizers add."""
    m = TYPE_SPAN.match(clause)
    if not m:
        return None
    rest, field = m["rest"], None
    q = QUOTED.match(rest)
    if q:
        text, tail = q["text"], q["after"]
        if tail.strip(" .!?,"):  # anything after the quote must be a whole field phrase, never silently dropped
            f = FIELD_TAIL.fullmatch(tail)
            if not f:
                return None
            field = f["f1"] or f["f2"]
    else:
        f = FIELD_TAIL.search(rest)
        if f:
            rest, field = rest[:f.start()], f["f1"] or f["f2"]
        elif re.search(r"\s(?:into|in)(?:\s+the)?[\s.!?]*$", rest, re.I):
            return None  # "type hello into": an unfinished field phrase is not text
        text = rest.strip()
        text = text[:-1] if text.endswith(".") and not text.endswith("..") else text
    if not text or re.fullmatch(r"(?:in|into)(?:\s.*)?", text, re.I):
        return None
    if field is not None and field.strip(" .,!?").lower() in ("", "the", "a", "an"):
        return None  # "into the" with no name: unfinished
    return {"text": text, **({"field": field.strip(" .,!?")} if field else {})}


def app_name(clause):
    m = APP_SPAN.search(clause)
    return m[1].strip(" .,!?") if m else ""


UNITS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                     "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
TENS = {w: 10 * i for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split(), 2)}
MARKER = re.compile(r"%|\bper\s*cent\b", re.I)
ANCHORS = {"to", "by", "at", "on", "volume", "spotify", "it", "up", "down", "set", "make", "put", "turn"}


def words_value(words):
    """Strict: "zero".."nineteen", "twenty".."ninety" [+ "one".."nine"], "[a|one] hundred". None for anything else."""
    if words in (["hundred"], ["a", "hundred"], ["one", "hundred"]):
        return 100
    if len(words) == 1 and words[0] in UNITS:
        return UNITS[words[0]]
    if len(words) == 1 and words[0] in TENS:
        return TENS[words[0]]
    if len(words) == 2 and words[0] in TENS and 1 <= UNITS.get(words[1], 0) <= 9:
        return TENS[words[0]] + UNITS[words[1]]
    return None


def percent(clause):
    """None when no percent marker is spoken, else (value or None when invalid, relative).
    The argument is every word between the marker and the nearest anchor ("to", "by", "volume"...), and all of it
    must parse: "-10", ".5", "12.5", "one hundred and 5", "a million" are invalid, never a guessed suffix."""
    m = MARKER.search(clause)
    if not m:
        return None
    words = clause[:m.start()].lower().split()
    arg = []
    while words and words[-1] not in ANCHORS:
        arg.insert(0, words.pop())
    relative = bool(words) and words[-1] == "by"
    if len(arg) == 1 and re.fullmatch(r"\d{1,3}", arg[0]):
        n = int(arg[0])
    else:
        n = words_value(" ".join(arg).replace("-", " ").split())
    return (n if n is not None and 0 <= n <= 100 else None, relative)


def step_for(ans, target, clause, inherited_browser=None):
    """One (confidence, action, args) for this target, or None when Jev is not sure."""
    if target == "app":
        act, conf = ans["app_action"]
        name = app_name(clause)
        if act == "none" or conf < GATE or not name:
            return None
        return (conf, f"app.{act}", {"app": name})
    if target == "website":
        rest, bid, unsupported = browser_qualifier(clause)
        if unsupported:
            return (ans["target"][1], "clarify", "unsupported_browser")  # named browser we don't drive: no navigation
        url = url_span(rest) or site_url(rest)
        if not url:
            return None
        args = {"url": url}
        if bid or inherited_browser:  # explicit qualifier in this clause wins over an earlier app.open
            args["browser"] = bid or inherited_browser
        return (ans["target"][1], "url.open", args)
    act, conf = ans.get(BRANCH[target], ("none", 0.0))  # an unanswered branch acts on nothing
    if act == "none" or conf < GATE:
        return None
    if target == "timer":
        return (conf, f"timer.{act}", {"text": clause})
    if target == "screen":
        if act == "list":
            return (conf, "screen.list", {})
        if act == "type":
            args = type_args(clause)
            return (conf, "screen.type", args) if args else None
        args = press_args(clause)
        return (conf, "screen.press", args) if args else None
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


UI_WORDS = re.compile(r"^\W*(?:please\s+)?(?:click|tap|double[\s-]click|type)\b|"
                      r"\b(?:button|checkbox|check\s+box|link|menu\s+item|icon|toggle)\b", re.I)


SUBMIT_WORDS = re.compile(r"^\W*(?:please\s+)?(?:(?:press|hit|tap)\s+(?:the\s+)?(?:enter|return)(?:\s+key)?|submit"
                          r"(?:\s+(?:it|that|this))?)(?:\s+(?:please|now))?[\s.!?]*$", re.I)


def pick(ans, clause, inherited_browser=None):
    """Trust Jev's target if it is fairly sure, else the single most confident action anywhere.
    "Click" and "tap", or a named UI part ("the Loud mode checkbox"), always mean a control on screen:
    Jev's target question hears "Loud" as volume, and that must never turn a click into a volume change."""
    if SUBMIT_WORDS.match(clause):  # Return in the selected field: exact words only, never inferred
        return (ans["target"][1], "screen.submit", {})
    if UI_WORDS.search(clause):
        return step_for(ans, "screen", clause)
    target, tconf = ans["target"]
    s = step_for(ans, target, clause, inherited_browser) if tconf >= 0.5 else None
    if s is None:
        cands = [x for x in (step_for(ans, t, clause, inherited_browser) for t in TARGETS) if x]
        s = max(cands, key=lambda x: x[0]) if cands else None
    return s


def judge(ans, clause, can_answer=False, inherited_browser=None):
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
    s = pick(ans, clause, inherited_browser)
    if s and s[1] == "clarify":
        return ("clarify", s[2])
    if not s:
        return ("answer", None) if can_answer and cat == "information_request" else ("clarify", "no_action")
    return ("step", {"clause": clause, "action": s[1], "args": s[2]})


def plan(text, classify, can_answer=False):
    """-> ("steps", [{"clause", "action", "args"}]), ("reply", key), ("answer", None) or ("clarify", reason).
    Every clause is judged before anything runs; one unclear clause stops the whole request."""
    try:
        import recipes
        hit = recipes.match(text)
        if hit is not None:
            if not hit:
                return ("clarify", "no_action")
            return ("steps", hit)
    except ValueError:
        return ("clarify", "invalid_recipe")
    except Exception:
        pass
    clauses = split_clauses(text)
    if not clauses:
        return ("clarify", "empty")
    if len(clauses) > MAX_CLAUSES:
        return ("clarify", "too_many_steps")
    if len(clauses) == 1:
        kind, got = judge(classify(clauses[0]), clauses[0], can_answer)
        return ("steps", [got]) if kind == "step" else (kind, got)
    steps = []
    browser = None  # same-request only: an earlier "open Safari/Chrome" applies to later website steps
    for clause in clauses:
        kind, got = judge(classify(clause), clause, inherited_browser=browser)
        if kind != "step":
            return ("clarify", got if kind == "clarify" else "no_action")
        if got["action"] == "app.open":
            bid = app_browser(got["args"].get("app"))
            if bid:
                browser = bid
        steps.append(got)
    return ("steps", steps)
