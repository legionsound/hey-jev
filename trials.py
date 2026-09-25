"""Readable trial history from the diagnostics log: one block per request, oldest first.

  what was heard (and how fast), how it was planned, what Jev said (low-confidence answers marked),
  each step's action, outcome and reason, what was spoken, and how long it took.
Startups print the exact revision they ran, so every attempt maps to a build. Nothing here reads screen text:
the log never stores it.
"""
import datetime
import json
import os

LOG = os.path.expanduser("~/Library/Logs/Hey Jev/requests.jsonl")
LOW = 0.65  # the planner's gate: answers below it are marked


def load(path=LOG, since_minutes=None, last=None):
    """-> request dicts oldest first, each run of one instance's requests headed by that instance's startup.

    Requests are rebuilt whole from the full log first, keyed by (instance, rid) so a reused id after a restart
    stays two attempts, and each carries its instance's startup as "build". Filters then pick whole requests by
    their first event: --since keeps those that began within the window, --last the N that began most recently.
    The heading startup is kept even when it is older than the window.
    """
    cutoff = None
    if since_minutes is not None:
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=since_minutes)
    startups, requests = {}, {}
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return []
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        ts = datetime.datetime.fromisoformat(d.get("ts", "1970-01-01T00:00:00+00:00"))
        inst = d.get("instance")
        if d.get("stage") == "startup" and d.get("outcome") == "starting":
            startups.setdefault(inst, {"kind": "startup", "ts": ts, "revision": d.get("revision"),
                                       "dirty": d.get("dirty"), "instance": inst, "backend": d.get("transcription")})
            continue
        rid = d.get("rid")
        if not rid:
            continue
        r = requests.get((inst, rid))
        if r is None:
            r = requests[inst, rid] = {"kind": "request", "ts": ts, "rid": rid, "instance": inst, "records": []}
        r["records"].append(d)
    reqs = sorted(requests.values(), key=lambda r: r["ts"])
    for r in reqs:
        r["build"] = startups.get(r["instance"])
    if cutoff:
        reqs = [r for r in reqs if r["ts"] >= cutoff]
    if last:
        reqs = reqs[-last:]
    events, prev = [], object()
    for r in reqs:
        if r["instance"] != prev and r["build"]:
            events.append(r["build"])  # repeated if instances interleave, so each block reads under its own build
        prev = r["instance"]
        events.append(r)
    return events


def summarize(r):
    """One request -> a dict of the parts a person reads."""
    b = r.get("build") or {}
    s = {"rid": r["rid"], "instance": r["instance"], "build": b.get("revision"), "dirty": b.get("dirty"),
         "time": r["ts"].astimezone().strftime("%H:%M:%S"), "steps": [], "jev": []}
    for d in r["records"]:
        stage, out = d.get("stage"), d.get("outcome")
        if stage == "submit":
            s["heard"], s["source"] = d.get("text"), d.get("source")
        elif stage == "recognize":
            s["stt_ms"] = d.get("stt_ms")
        elif stage == "classify" and out == "ok":
            ans = d.get("answers") or {}
            main = {k: ans[k] for k in ("category", "target") if k in ans}
            target = (ans.get("target") or [None])[0]
            relevant = {"category", "target", f"{target}_action"} | ({"volume_scope"} if target == "volume" else set())
            low = {k: v for k, v in ans.items() if k in relevant and isinstance(v, list) and len(v) == 2 and v[1] < LOW}
            s["jev"].append({"clause": d.get("clause"), "ms": d.get("duration_ms"), **main,
                             **({"unsure": low} if low else {})})
        elif stage == "plan":
            s["plan"] = d.get("steps") if out == "steps" else f"{out}: {d.get('detail')}"
        elif stage == "confirm":
            s["steps"].append({"confirm": out})
        elif stage == "resolve" and out != "target":
            s["steps"].append({"action": d.get("action"), "resolve": out, "why": d.get("reason")})
        elif stage == "verify":
            s["steps"].append({"step": d.get("step"), "result": out, "why": d.get("detail"),
                               "facts": {k: v for k, v in (d.get("facts") or {}).items() if k != "items"}})
        elif stage in ("task", "task_decide"):
            s["steps"].append({stage: out, **({"why": d.get("detail")} if d.get("detail") else {}),
                               **({"confidence": d.get("confidence")} if d.get("confidence") is not None else {})})
        elif stage == "done":
            s["result"], s["ms"] = out, d.get("duration_ms")
            s["why"] = d.get("step_detail") or d.get("detail")
        elif stage == "speak":
            s["said"] = d.get("line")
        elif stage in ("answer", "choose_control", "classify_items") and out == "error":
            s["steps"].append({stage: "error", "why": d.get("error")})
    return s


def render(events):
    out = []
    for e in events:
        if e["kind"] == "startup":
            out.append(f"── started {e['ts'].astimezone():%H:%M:%S} · build {e['revision']}"
                       f"{' (modified)' if e['dirty'] else ''} · {e['backend'] or '?'} · instance {e['instance']}")
            continue
        s = summarize(e)
        out.append(f"{s['time']}  “{s.get('heard', '?')}”  [{s.get('source', '?')}"
                   f"{', stt ' + str(s['stt_ms']) + ' ms' if s.get('stt_ms') else ''}]  → {s.get('result', 'unfinished')}"
                   f"{' (' + str(s['why']) + ')' if s.get('why') else ''}  {s.get('ms') or 0:.0f} ms")
        for j in s["jev"]:
            unsure = f"  unsure: {j['unsure']}" if j.get("unsure") else ""
            out.append(f"    jev {j.get('ms') or 0:.0f} ms  category={j.get('category')} target={j.get('target')}{unsure}")
        if s.get("plan") is not None:
            out.append(f"    plan {s['plan']}")
        for st in s["steps"]:
            out.append("    " + "  ".join(f"{k}={v}" for k, v in st.items() if v not in (None, {}, [])))
        if s.get("said"):
            out.append(f"    said: {s['said']}")
    return "\n".join(out)
