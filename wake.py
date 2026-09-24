"""The wake phrase: validation, matching at the start of a transcript, and recognition hints for each backend.

Matching is exact on normalized words. The default "Hey Jev" keeps its known mishearings; a custom phrase matches
only itself plus aliases the user typed in. Nothing fuzzy is added silently, so ordinary talk can't become a command.
"""
import re
import unicodedata

DEFAULT = "Hey Jev"
# How speech recognizers tend to hear "Hey Jev". Used for the default phrase only.
DEFAULT_PATTERN = r"(?:hey|hi|hay|okay|ok|a)\W+(?:jev|jevs|jeff|jeffs|jef|jeb|jab|chev|jeve|jav)"
MAX_WORDS, MAX_CHARS, MAX_ALIASES = 4, 40, 6
WORD = re.compile(r"[^\W_]+(?:'[^\W_]+)*")  # any script's letters and digits, inner apostrophes kept
# Single common words that ordinary talk contains: saving one still works,
# but the settings form warns that it can wake Hey Jev by accident.
COMMON = frozenset({"ok", "hey", "yes", "no", "stop", "go", "play", "the"})


def norm(text):
    """One spelling for comparison: NFC, lower-cased, curly apostrophes straightened."""
    return unicodedata.normalize("NFC", text or "").lower().replace("’", "'")


def words(text):
    """Words, punctuation dropped, nothing else lost: "Okay, José!" -> ["okay", "josé"]."""
    return WORD.findall(norm(text))


def validate(phrase):
    """-> the phrase as typed, trimmed. Raises ValueError with a plain reason."""
    phrase = " ".join((phrase or "").split())
    ws = words(phrase)
    if not ws:
        raise ValueError("Enter a wake phrase.")
    if len(ws) > MAX_WORDS or len(phrase) > MAX_CHARS:
        raise ValueError(f"Keep the wake phrase to {MAX_WORDS} words or fewer.")
    if " ".join(ws) != " ".join(words(re.sub(r"[,.!?;:]", " ", phrase))) or re.search(r"[^\w\s'’,.!?-]", phrase):
        raise ValueError("Use letters, numbers and spaces only.")
    return phrase


def short_warning(phrase):
    """Warning text when a saved phrase risks accidental wakes, else "".

    Any valid 1-4 word phrase saves; a single short or common word
    ("Jo", "Hi", "stop") still saves but earns this warning."""
    ws = words(phrase or "")
    if len(ws) == 1 and (len(ws[0]) < 4 or ws[0] in COMMON):
        return "Short phrases can wake Hey Jev by accident."
    return ""


def parse_aliases(text):
    """Comma-separated extra spellings the user approves, e.g. "OK Zorblat, Okay Zorblot"."""
    out = []
    for part in (text or "").split(","):
        if part.strip():
            out.append(validate(part))
    if len(out) > MAX_ALIASES:
        raise ValueError(f"Up to {MAX_ALIASES} extra spellings.")
    return out


class Wake:
    def __init__(self, phrase=DEFAULT, aliases=()):
        self.phrase = validate(phrase)
        self.aliases = [validate(a) for a in aliases]
        self.is_default = words(self.phrase) == words(DEFAULT)
        alts = [DEFAULT_PATTERN] if self.is_default else []
        for p in [self.phrase, *self.aliases]:
            alts.append(r"\W+".join(re.escape(w).replace("'", "['’]") for w in words(p)))
        self.regex = re.compile(r"^\W*(?:" + "|".join(alts) + r")\b\W*", re.I)

    def match(self, text):
        """-> the command after the phrase ("" when only the phrase was said), or None when it isn't there."""
        text = unicodedata.normalize("NFC", text or "")
        m = self.regex.match(text)
        return text[m.end():].strip(" .,!?") if m else None

    @property
    def prompt(self):
        """Whisper's initial prompt: examples that make the phrase a likely transcription."""
        p = self.phrase
        return f"{p}, open Spotify. {p}, pause the music. {p}, turn the volume down."

    @property
    def hints(self):
        """Apple contextual strings: the phrase and its approved spellings."""
        return [self.phrase, *self.aliases]

    def hint_text(self):
        return f"Say “{self.phrase}”, then your command"
