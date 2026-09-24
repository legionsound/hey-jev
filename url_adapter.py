"""URL resolution, opening, and verification for url.open.

resolve_url(spoken) -> ("target", {"url": str}) | ("none", reason)
run_url_open(target, deadline) -> None
verify_url_open(target, deadline) -> ("done"|"wait"|"failed"|"unverified", facts)

Strict comparison: equal after lowercasing host only, and treating empty
path as "/". A leading "www." on either host is ignored, and an
http->https upgrade is accepted (downgrade is not). Explicit port, path,
RAW query string, and fragment must match. Any other difference
(incl. redirects) -> unverified.

Capability split (honest, per dictionary evidence):
- Chrome exposes a unique per-tab id (scripting.sdef, class tab, property
  id). Open records it; verify finds that exact tab by id. Done is only
  ever reported for Chrome.
- Safari exposes no public tab id (index only, recyclable). Open still
  dispatches a new tab, but verify always reports unverified.
- Other browsers: dispatched via `open -b`, no readback -> unverified.
"""

import subprocess
import sys
import time
import urllib.parse

SUPPORTED_BROWSERS = ("com.apple.Safari", "org.google.Chrome",
                      "com.google.Chrome")
CHROME_BIDS = ("org.google.Chrome", "com.google.Chrome")
_CTRL_RE = __import__("re").compile(r"[\x00-\x20\x7f]")

_HANDLER_HELPER = (
    "import sys",
    "from Cocoa import NSWorkspace, NSURL",
    "url = NSURL.URLWithString_(sys.argv[1])",
    "if url is None: print(''); sys.exit(2)",
    "app = NSWorkspace.sharedWorkspace().URLForApplicationToOpenURL_(url)",
    "if app is None: print(''); sys.exit(3)",
    "from Cocoa import NSBundle",
    "b = NSBundle.bundleWithURL_(app)",
    "bid = b.bundleIdentifier() if b is not None else ''",
    "print(str(bid or ''))",
)


def _now():
    return time.time()


def _remaining(deadline):
    try:
        return float(deadline) - _now()
    except TypeError:
        return 0.0


def _require_time(deadline, what="deadline already passed"):
    rem = _remaining(deadline)
    if rem <= 0:
        raise TimeoutError(what)
    return rem


def _split_safe(url):
    try:
        return urllib.parse.urlsplit(url)
    except ValueError as e:
        raise ValueError(f"malformed url: {e}")


def normalize_url(url):
    """Canonical parse for comparison. Raises ValueError on bad input."""
    p = _split_safe(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError(f"unsupported url: {url!r}")
    if p.username or p.password:
        raise ValueError(f"userinfo not supported: {url!r}")
    try:
        _ = p.port
    except ValueError as e:
        raise ValueError(f"malformed port: {e}")
    return p


def _bare_host(host):
    """Lowercased host with one leading www. removed."""
    h = (host or "").lower()
    if h.startswith("www."):
        h = h[4:]
    return h


def urls_equal(requested, observed):
    """Compare per contract. Returns (equal: bool, detail: str)."""
    try:
        a = normalize_url(requested)
        b = normalize_url(observed)
    except ValueError as e:
        return False, str(e)
    if a.scheme != b.scheme:
        if not (a.scheme == "http" and b.scheme == "https"):
            return False, "scheme differs"
    if _bare_host(a.hostname) != _bare_host(b.hostname):
        return False, "host differs"
    # Explicit ports compared directly: omitted != explicit, even when
    # the explicit value equals the scheme default. Only the two cleared
    # normalizations (host case, empty path) apply.
    if a.port != b.port:
        return False, "port differs"
    if (a.path or "/") != (b.path or "/"):
        return False, "path differs"
    # Raw query comparison: %20 vs + are NOT equal.
    if a.query != b.query:
        return False, "query differs"
    if a.fragment != b.fragment:
        return False, "fragment differs"
    return True, "match"


def _rebuild(p):
    """Rebuild normalized URL, preserving IPv6 brackets, no userinfo."""
    host = (p.hostname or "").lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    try:
        port = p.port
    except ValueError:
        raise ValueError("malformed port")
    if port is not None:
        netloc += f":{port}"
    return urllib.parse.urlunsplit(
        (p.scheme, netloc, p.path or "", p.query, p.fragment))


SITE_FOR_NAME = {
    "youtube": "https://www.youtube.com/",
    "google": "https://www.google.com/",
    "gmail": "https://mail.google.com/",
    "github": "https://github.com/",
    "reddit": "https://www.reddit.com/",
    "netflix": "https://www.netflix.com/",
    "amazon": "https://www.amazon.com/",
    "wikipedia": "https://www.wikipedia.org/",
    "twitter": "https://x.com/",
    "x": "https://x.com/",
    "facebook": "https://www.facebook.com/",
    "instagram": "https://www.instagram.com/",
    "linkedin": "https://www.linkedin.com/",
    "chatgpt": "https://chatgpt.com/",
    "claude": "https://claude.ai/",
}


def site_for_name(name):
    """Curated site URL for a whole spoken name, or None. Never guesses."""
    key = (name or "").strip().lower()
    return SITE_FOR_NAME.get(key)


def resolve_url(spoken):
    """Resolve spoken text to a URL target. Bare domain -> https://."""
    s = (spoken or "").strip()
    if not s:
        return ("none", "empty url")
    if _CTRL_RE.search(s) or " " in s or "\t" in s:
        return ("none", f"not a url: {spoken!r}")
    low = s.lower()
    for bad in ("javascript:", "data:", "file:", "ftp:", "about:"):
        if low.startswith(bad):
            return ("none", f"unsupported scheme in {spoken!r}")
    url = s if "://" in s else "https://" + s
    try:
        p = _split_safe(url)
        if p.scheme not in ("http", "https"):
            return ("none", f"unsupported scheme in {spoken!r}")
        if not p.hostname:
            return ("none", f"not a url: {spoken!r}")
        if p.username or p.password:
            return ("none", "userinfo not supported")
        rebuilt = _rebuild(p)
    except ValueError as e:
        return ("none", str(e))
    return ("target", {"url": rebuilt})


def default_browser_for_url(url, timeout, _run=subprocess.run,
                            _exe=None):
    """Bundle id of the default handler for url via NSWorkspace.

    Always runs the fixed helper as a bounded subprocess under the app's
    own runtime (sys.executable), so the deadline applies to every native
    call. Raises on lookup failure; never returns a fallback.
    """
    exe = _exe or sys.executable
    cmd = [exe, "-c", "\n".join(_HANDLER_HELPER), url]
    try:
        proc = _run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise TimeoutError("browser lookup timed out")
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"browser lookup failed: {e}")
    bid = (proc.stdout or "").strip()
    if proc.returncode != 0 or not bid:
        raise RuntimeError("browser lookup failed: no default handler")
    return bid


def _osascript_argv(lines, extra_args, timeout, _run=subprocess.run):
    """Run a fixed AppleScript with -e flags; user data only in argv."""
    cmd = ["osascript"]
    for ln in lines:
        cmd += ["-e", ln]
    cmd += ["--"] + list(extra_args)
    return _run(cmd, capture_output=True, text=True, timeout=timeout)


def osascript_smoke(timeout=10):
    """Harmless real execution proving -e + argv delivery works."""
    proc = _osascript_argv(["on run argv", "return item 1 of argv",
                            "end run"], ["ping"], timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"smoke failed: {(proc.stderr or '')[:200]}")
    if (proc.stdout or "").strip() != "ping":
        raise RuntimeError("smoke round-trip mismatch")
    return True


SAFARI_OPEN = (
    "on run argv",
    "set theURL to item 1 of argv",
    'tell application "Safari"',
    "activate",
    "if (count of windows) is 0 then make new document",
    "tell front window to set newTab to make new tab",
    "set URL of newTab to theURL",
    "end tell",
    "end run",
)

CHROME_OPEN = (
    "on run argv",
    "set theURL to item 1 of argv",
    'tell application "Google Chrome"',
    "activate",
    "if (count of windows) is 0 then make new window",
    "tell front window to set newTab to make new tab",
    "set URL of newTab to theURL",
    'return (id of newTab as text)',
    "end tell",
    "end run",
)

CHROME_READ = (
    "on run argv",
    "set wantId to item 1 of argv",
    'tell application "Google Chrome"',
    "repeat with w in every window",
    "repeat with t in (tabs of w)",
    "if (id of t as text) is equal to wantId then",
    'return \"OK:\" & (URL of t as text)',
    "end if",
    "end repeat",
    "end repeat",
    'return \"GONE:\"',
    "end tell",
    "end run",
)


def run_url_open(target, deadline, _run=subprocess.run,
                 _default_browser_fn=None):
    """Open target URL in a new tab/window. Records identity for verify."""
    url = target.get("url", "")
    try:
        normalize_url(url)
    except ValueError as e:
        raise RuntimeError(str(e))
    rem = _require_time(deadline)
    lookup = _default_browser_fn or (
        lambda to: default_browser_for_url(url, to, _run))
    browser = lookup(rem)  # errors propagate; no fallback, no launch
    target["opened_with"] = browser
    if browser in CHROME_BIDS:
        rem = _require_time(deadline)
        try:
            proc = _osascript_argv(CHROME_OPEN, [url], rem, _run)
        except subprocess.TimeoutExpired:
            raise TimeoutError("open timed out")
        if proc.returncode != 0:
            raise RuntimeError(
                f"osascript failed: {(proc.stderr or '')[:300]}")
        tab_id = (proc.stdout or "").strip()
        if not tab_id:
            raise RuntimeError("no tab identity returned")
        target["tab"] = f"{browser}#{tab_id}"
        return None
    if browser == "com.apple.Safari":
        rem = _require_time(deadline)
        try:
            proc = _osascript_argv(SAFARI_OPEN, [url], rem, _run)
        except subprocess.TimeoutExpired:
            raise TimeoutError("open timed out")
        if proc.returncode != 0:
            raise RuntimeError(
                f"osascript failed: {(proc.stderr or '')[:300]}")
        target["tab"] = None  # no public tab id; verify reports unverified
        return None
    # Any other default browser: real bounded dispatch, no readback.
    rem = _require_time(deadline)
    try:
        proc = _run(["open", "-b", browser, url],
                    capture_output=True, text=True, timeout=rem)
    except subprocess.TimeoutExpired:
        raise TimeoutError("open timed out")
    if proc.returncode != 0:
        raise RuntimeError(f"open failed: {(proc.stderr or '')[:300]}")
    target["tab"] = None
    return None


def _read_chrome_tab(tab_id, timeout, _run=subprocess.run):
    """Returns (status, url): status in OK/GONE/UNREADABLE."""
    if not tab_id:
        return ("UNREADABLE", None)
    try:
        proc = _osascript_argv(CHROME_READ, [tab_id], timeout, _run)
    except subprocess.TimeoutExpired:
        return ("UNREADABLE", None)
    if proc.returncode != 0:
        return ("UNREADABLE", None)
    out = (proc.stdout or "").strip()
    if out.startswith("OK:"):
        return ("OK", out[3:] or None)
    return ("GONE", None)


def verify_url_open(target, deadline, _run=subprocess.run):
    """Verify the opened tab's URL. Done is only ever reported for Chrome."""
    url = target.get("url", "")
    browser = target.get("opened_with")
    if browser in CHROME_BIDS:
        ident = target.get("tab", "")
        prefix = browser + "#"
        if ident.startswith(prefix):
            ident = ident[len(prefix):]
        if not ident:
            return ("unverified", {"observed": None,
                                   "reason": "missing tab identity"})
        rem = _remaining(deadline)
        status, observed = _read_chrome_tab(ident, max(rem, 0.0), _run=_run)
        if status == "GONE":
            return ("unverified", {"observed": None,
                                   "reason": "tab closed; id not reused"})
        if observed is None:
            return ("wait", {"reason": "tab not readable yet"})
        try:
            normalize_url(observed)
        except ValueError:
            return ("wait", {"observed": observed,
                             "reason": "page not loaded yet"})
        ok, detail = urls_equal(url, observed)
        if ok:
            return ("done", {"observed": observed})
        return ("unverified", {"observed": observed, "reason": detail})
    if browser == "com.apple.Safari":
        return ("unverified", {"observed": None,
                               "reason": "safari exposes no tab id; opened only",
                               "opened_with": browser})
    return ("unverified", {"observed": None,
                           "reason": "unsupported browser" if browser
                           else "unknown browser",
                           "opened_with": browser})
