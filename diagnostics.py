"""Local diagnostic log: one JSON line per stage of each request, keyed by the engine's request id.

~/Library/Logs/Hey Jev/requests.jsonl, folder 0700, files 0600, rotated at max_bytes with `backups` old files.
Never records keys, authorization headers, raw audio or provider payloads. It does keep what you said (the submit
and recognize records' transcript) and Jev's answers, so a failed command can be traced; screen text is kept only as
lengths. Never raises: a logging failure can't break a command.
"""
import datetime
import json
import logging
import logging.handlers
import os
import re
import threading

LOG_DIR = os.path.expanduser("~/Library/Logs/Hey Jev")
MAX_STR = 500
SECRET_KEY = re.compile(r"key|token|auth|secret|password|bearer|cookie", re.I)
# Bearer values, sk- keys, and long mixed-case-plus-digit runs (API keys). Lowercase hex such as request ids,
# hashes and the repo folder name stays readable.
SECRET_VALUE = re.compile(r"(?i:\bbearer\s+\S+)|\bsk-[A-Za-z0-9_-]{8,}"
                          r"|\b(?=[\w-]*[A-Z])(?=[\w-]*[a-z])(?=[\w-]*\d)[A-Za-z0-9_-]{32,}\b")

_lock = threading.Lock()
_logger = None
_instance = None
path = None


class _PrivateHandler(logging.handlers.RotatingFileHandler):
    """Every file it opens, including after rotation, is 0600."""

    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, "a", encoding="utf-8")


def init(instance, log_dir=None, max_bytes=2_000_000, backups=3):
    """Idempotent. Returns the log path, or None if logging is unavailable."""
    global _logger, _instance, path
    try:
        with _lock:
            if _logger is not None:
                return path
            d = log_dir or LOG_DIR
            os.makedirs(d, mode=0o700, exist_ok=True)
            os.chmod(d, 0o700)
            p = os.path.join(d, "requests.jsonl")
            handler = _PrivateHandler(p, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger = logging.getLogger(f"heyjev.diagnostics.{id(handler)}")
            logger.propagate = False
            logger.setLevel(logging.INFO)
            logger.addHandler(handler)
            _logger, _instance, path = logger, instance, p
            return p
    except Exception:
        return None


def reset():
    """Tests: close the handler so init can run again."""
    global _logger, _instance, path
    with _lock:
        if _logger:
            for h in list(_logger.handlers):
                h.close()
                _logger.removeHandler(h)
        _logger = _instance = path = None


def sanitize(value, depth=0):
    if depth > 6:
        return "…"
    if isinstance(value, dict):
        return {str(k): "[redacted]" if SECRET_KEY.search(str(k)) else sanitize(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v, depth + 1) for v in list(value)[:50]]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else str(value)
    s = SECRET_VALUE.sub("[redacted]", str(value))
    return s if len(s) <= MAX_STR else s[:MAX_STR] + "…"


def record(rid, stage, outcome, duration_ms=None, **fields):
    try:
        if _logger is None:
            return
        entry = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds"),
                 "instance": _instance, "rid": rid, "stage": stage, "outcome": sanitize(outcome),
                 "duration_ms": None if duration_ms is None else int(duration_ms)}
        entry.update(sanitize({k: v for k, v in fields.items() if k not in entry}))
        _logger.info(json.dumps(entry, ensure_ascii=False, default=str))
    except Exception:
        pass
