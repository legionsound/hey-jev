"""Apple's built-in language models (Foundation Models), through the bundled Swift helper `heyjev-fm`.

One warm helper process, JSON lines over stdin/stdout, one request at a time. Answers only: the helper gives the
model no tools. A timed-out or crashed helper is killed and started fresh for the next question; nothing is replayed.
"""
import json
import os
import subprocess
import threading
import queue
import uuid

MODELS = ("on_device", "private_cloud")
_lock = threading.Lock()
_proc = None
_lines = None


def helper_path():
    """Inside the app bundle (Resources) first, then the checkout's bin/ for development runs."""
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(os.environ.get("RESOURCEPATH", ""), "heyjev-fm"), os.path.join(here, "bin", "heyjev-fm")):
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _start():
    global _proc, _lines
    path = helper_path()
    if not path:
        raise Unavailable("Apple models aren't installed with this copy of Hey Jev.")
    try:
        _proc = subprocess.Popen([path], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 text=True, bufsize=1)
    except OSError as exc:
        raise Unavailable(f"Couldn't start the Apple model helper: {exc.strerror}.") from None
    _lines = queue.Queue()
    proc, lines = _proc, _lines

    def pump():
        with proc.stdout:
            for line in proc.stdout:
                lines.put(line)
        lines.put(None)  # helper exited
    threading.Thread(target=pump, daemon=True, name="heyjev-fm").start()


def _stop():
    global _proc
    if _proc is not None:
        try:
            _proc.kill()
            _proc.wait(timeout=2)  # reaped, so no zombie helper is left behind
            _proc.stdin.close()
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        _proc = None


class Unavailable(Exception):
    """Plain-language reason Apple answers can't run right now."""


def _call(request, timeout):
    """Send one request, wait for its reply. On timeout or a dead helper, kill it so the next call starts clean."""
    with _lock:
        if _proc is None or _proc.poll() is not None:
            _start()
        try:
            _proc.stdin.write(json.dumps(request) + "\n")
            _proc.stdin.flush()
            while True:
                line = _lines.get(timeout=timeout)
                if line is None:
                    raise RuntimeError("Apple model helper stopped.")
                reply = json.loads(line)
                if reply.get("op") == request["op"] and reply.get("id") == request.get("id"):
                    return reply
        except queue.Empty:
            _stop()
            raise TimeoutError("Apple model took too long.") from None
        except (OSError, ValueError, RuntimeError):
            _stop()
            raise


def models(timeout=5):
    """[{id, name, remote, available, reason}] this Mac reports. Unavailable if the helper is missing."""
    got = _call({"op": "models"}, timeout).get("models") or []
    return [m for m in got if m.get("id") in MODELS]


def ask(model, instructions, prompt, *, history=(), max_tokens=200, timeout=30):
    """One spoken answer. Raises Unavailable with the helper's reason, or RuntimeError on failure."""
    if model not in MODELS:
        raise Unavailable("Pick an Apple model in Settings.")
    reply = _call({"op": "ask", "id": uuid.uuid4().hex, "model": model, "instructions": instructions,
                   "history": list(history), "prompt": prompt, "max_tokens": max_tokens}, timeout)
    status = reply.get("status")
    if status == "finished" and (reply.get("text") or "").strip():
        return reply["text"].strip()
    if status == "unavailable":
        raise Unavailable(reply.get("error") or "This Apple model isn't available on this Mac.")
    raise RuntimeError(reply.get("error") or "Apple model returned no answer.")


def interrupt():
    """Stop: kill a helper blocked mid-answer, without the lock (the waiting call holds it). The waiting call sees
    the helper exit and fails; the next question starts a fresh helper."""
    proc = _proc
    if proc is not None and proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass


def shutdown():
    with _lock:
        if _proc is not None and _proc.poll() is None:
            try:
                _proc.stdin.write('{"op":"quit"}\n')
                _proc.stdin.flush()
            except OSError:
                pass
        _stop()
