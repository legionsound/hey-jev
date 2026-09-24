"""Installed-app discovery and name resolution for app.open.

Discovered records only: scan roots recursively (never descending into
.app bundles), read bundle id + display name from Info.plist, emit only
records with a bundle id. No synthetic entries, no silent fuzzy match.
"""

import os
import plistlib
import unicodedata

ROOTS = ("/Applications", "/System/Applications",
         os.path.expanduser("~/Applications"))

# Single known app living outside the scanned roots.
EXTRA_APPS = ("/System/Library/CoreServices/Finder.app",)

# Short aliases resolved against discovered records only.
ALIASES = {
    "chrome": "google chrome",
    "vscode": "visual studio code",
}

_inventory = None


def _norm(s):
    s = unicodedata.normalize("NFC", s or "").casefold()
    s = s.replace("-", " ").replace("_", " ")
    return " ".join(s.split())


def _stem(path):
    return os.path.splitext(os.path.basename(path))[0]


def _read_bundle(path):
    plist = os.path.join(path, "Contents", "Info.plist")
    try:
        with open(plist, "rb") as f:
            info = plistlib.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict):
        return None
    bid = info.get("CFBundleIdentifier", "") or ""
    if not isinstance(bid, str) or not bid.strip():
        return None
    disp = (info.get("CFBundleDisplayName")
            or info.get("CFBundleName")
            or _stem(path))
    if not isinstance(disp, str) or not disp.strip():
        disp = _stem(path)
    return {"name": disp, "bundle_id": bid, "path": path}


def _scan(roots=None):
    apps, seen_paths = [], set()

    def _add(app_path):
        path = os.path.realpath(app_path)
        if path in seen_paths:
            return
        seen_paths.add(path)
        rec = _read_bundle(app_path)
        if rec is not None:
            rec["path"] = path
            apps.append(rec)

    for root in (ROOTS if roots is None else roots):
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, _ in os.walk(root):
            if dirpath.endswith(".app"):
                dirnames[:] = []
                continue
            for e in list(dirnames):
                if not e.endswith(".app"):
                    continue
                dirnames.remove(e)
                _add(os.path.join(dirpath, e))
    for extra in EXTRA_APPS if roots is None else ():
        if os.path.isdir(extra):
            _add(extra)
    # Preserve distinct installations; stable order for determinism.
    apps.sort(key=lambda a: (str(a.get("name", "")).casefold(), a["path"]))
    return apps


def refresh():
    """Rescan roots; engine calls once after a 'none' result."""
    global _inventory
    _inventory = _scan()


def list_apps():
    global _inventory
    if _inventory is None:
        _inventory = _scan()
    return [dict(a) for a in _inventory]


def _set_inventory(apps):
    """Test hook: inject a deterministic inventory."""
    global _inventory
    _inventory = [dict(a) for a in apps]


def _names(app):
    """All matchable names: display name + bundle file stem."""
    seen = {_norm(str(app.get("name", "")))}
    stem = _norm(_stem(str(app.get("path", ""))))
    if stem and stem not in seen:
        seen.add(stem)
    return seen


def resolve_app(spoken):
    """-> ("target", app) | ("choices", [app, ...]) | ("none", reason)."""
    q = _norm(spoken)
    if not q:
        return ("none", "empty app name")
    q = ALIASES.get(q, q)
    apps = list_apps()
    by_name = {}
    for a in apps:
        for n in _names(a):
            by_name.setdefault(n, []).append(a)
    if q in by_name:
        # Deduplicate same record reachable via name + stem.
        hits, seen = [], set()
        for h in by_name[q]:
            if h["path"] not in seen:
                seen.add(h["path"])
                hits.append(h)
        if len(hits) == 1:
            return ("target", dict(hits[0]))
        return ("choices", [dict(h) for h in hits])
    # Whole-token prefix only: "saf" must not match "safari";
    # multi-token query must match whole leading tokens.
    # Single-token query also matches any whole token anywhere:
    # "resolve" matches both "DaVinci Resolve" and "Uninstall Resolve".
    qtok = q.split()
    def _tok_match(name):
        toks = _norm(name).split()
        if len(qtok) == 1:
            return qtok[0] in toks
        return toks[:len(qtok)] == qtok
    partial = [a for a in apps
               if any(_tok_match(n) for n in _names(a))]
    # Deduplicate by path for determinism.
    uniq = {a["path"]: a for a in partial}
    partial = sorted(uniq.values(),
                     key=lambda a: (str(a.get("name", "")).casefold(), a["path"]))
    if len(partial) == 1:
        return ("target", dict(partial[0]))
    if len(partial) > 1:
        return ("choices", [dict(h) for h in partial])
    return ("none", f"no installed app matches {spoken!r}")
