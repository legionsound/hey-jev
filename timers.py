"""Timer and reminder state: duration parsing, the running list, and the alert loop."""
import itertools
import re
import threading
import time

NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
                "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
                "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "ninety": 90, "couple": 2, "few": 3}
UNITS = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "m": 60, "min": 60, "mins": 60,
         "minute": 60, "minutes": 60, "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600}
DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b(\s+and\s+a\s+half)?")


def _digits(text):
    """'twenty five minutes' -> '25 minutes', 'half an hour' -> '30 minutes'."""
    t = re.sub(r"\bhalf an? hour\b", "30 minutes", text.lower())
    t = re.sub(r"\ba couple of\b", "couple", t)
    t = re.sub(r"\b(an?|few|couple)\s+(hours?|minutes?|seconds?)\b", lambda m: f"{NUMBER_WORDS[m[1]]} {m[2]}", t)
    words = t.replace("-", " ").split()
    out, i = [], 0
    while i < len(words):
        w = words[i].strip(",.!?")
        if w in NUMBER_WORDS and w not in ("a", "an", "few", "couple"):
            n = NUMBER_WORDS[w]
            nxt = words[i + 1].strip(",.!?") if i + 1 < len(words) else ""
            if n >= 20 and nxt in NUMBER_WORDS and NUMBER_WORDS[nxt] < 10 and nxt not in ("a", "an"):
                n, i = n + NUMBER_WORDS[nxt], i + 1
            out.append(str(n))
        else:
            out.append(words[i])
        i += 1
    return " ".join(out)


def parse_duration(text):
    """Total seconds mentioned in the sentence, or None."""
    total = 0
    for num, unit, half in DURATION.findall(_digits(text)):
        secs = UNITS[unit]
        total += float(num) * secs + (secs / 2 if half else 0)
    return int(total) or None


def parse_reminder(text):
    """What to remind about: the part after 'to', minus any duration. 'remind me in 5 min to call mum' -> 'call mum'."""
    m = re.search(r"\bto\s+(.+)$", _digits(text))
    if not m:
        return None
    what = DURATION.sub("", m[1])
    what = re.sub(r"\bplease\b", "", what).strip(" .,!?")
    what = re.sub(r"\s*\b(in|for|after)$", "", what).strip(" .,!?")
    return what or None


def say_duration(secs):
    secs = max(0, int(round(secs)))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    parts = [f"{n} {u}{'' if n == 1 else 's'}" for n, u in ((h, "hour"), (m, "minute"), (s, "second")) if n]
    if h or m >= 10:  # skip seconds once it's a long wait
        parts = parts[:2] if h else parts[:1]
    return " and ".join(parts) or "no time"


def short_duration(secs):
    h, rem = divmod(int(secs), 3600)
    m, s = divmod(rem, 60)
    return " ".join(f"{n} {u}" for n, u in ((h, "hr"), (m, "min"), (s, "sec")) if n) or "0 sec"


TIMERS, LOCK = [], threading.Lock()
_IDS = itertools.count(1)
on_reminder_set = None  # siri sets this to prepare the spoken alert in the background


def add(secs, label=None, said=""):
    t = {"id": next(_IDS), "end": time.time() + secs, "secs": secs, "label": label, "line": None}
    with LOCK:
        TIMERS.append(t)
        TIMERS.sort(key=lambda x: x["end"])
    if label and on_reminder_set:
        threading.Thread(target=on_reminder_set, args=(t, said), daemon=True).start()
    return t


def snapshot():
    """(name, seconds left) for each running timer, soonest first."""
    now = time.time()
    with LOCK:
        return [((t["label"] or "").capitalize() or short_duration(t["secs"]) + " timer", max(0, t["end"] - now))
                for t in TIMERS]


def start_loop(on_done):
    """Fires on_done(timer) when a timer runs out."""
    def loop():
        while True:
            time.sleep(0.25)
            with LOCK:
                due = [t for t in TIMERS if t["end"] <= time.time()]
                for t in due:
                    TIMERS.remove(t)
            for t in due:
                try:
                    on_done(t)
                except Exception as e:
                    print(f"  timer alert failed: {e}")
    threading.Thread(target=loop, daemon=True).start()
