"""Claude Code and Codex as full agent sessions, over the Agent Client Protocol (JSON-RPC 2.0, one JSON per line on
stdio), the same way Buzz runs them. The adapter runs the agent's own harness: its tools, hooks, MCP servers,
instructions and memory load from the user's config. Hey Jev is only the client: it sends the prompt, shows activity,
routes permission requests to the user, and never approves anything itself.

One session per agent per app run. Stop sends session/cancel, then kills the adapter if the turn does not settle.
A dead adapter is started fresh for the next prompt; nothing is replayed.
"""
import itertools
import json
import os
import shutil
import subprocess
import threading

ADAPTERS = {"claude": "claude-agent-acp", "codex": "codex-acp"}
NAMES = {"claude": "Claude Code", "codex": "Codex"}
# Where adapters and node live when the app's PATH is the bare launchd one.
SEARCH = [os.path.expanduser("~/Library/Application Support/Buzz/node-tools/bin"), os.path.expanduser("~/.local/bin"),
          os.path.expanduser("~/.npm-global/bin"), "/opt/homebrew/bin", "/usr/local/bin"]
CANCEL_GRACE = 5  # seconds a cancelled turn gets to settle before the adapter is killed


class Unavailable(Exception):
    """Plain-language reason this agent can't run right now (missing adapter, not signed in)."""


def _which(name):
    path = os.pathsep.join([os.environ.get("PATH", "")] + SEARCH)
    return shutil.which(name, path=path)


def find_adapter(name):
    """Path of the installed ACP adapter for "claude" or "codex", or None."""
    return _which(ADAPTERS[name]) if name in ADAPTERS else None


def _command(adapter):
    """Adapters are node scripts (#!/usr/bin/env node); run them with an explicit node so the app's PATH doesn't matter."""
    with open(adapter, "rb") as f:
        head = f.readline()
    if b"node" in head:
        node = _which("node")
        if not node:
            raise Unavailable("Node.js isn't installed, and the agent connector needs it.")
        return [node, adapter]
    return [adapter]


class Session:
    """One adapter process and one ACP session. prompt() runs one turn; cancel() stops it."""

    def __init__(self, agent, cwd, command=None):
        self.agent, self.cwd, self.command = agent, cwd, command
        self.proc, self.session_id = None, None
        self.ids = itertools.count(1)
        self.lock = threading.Condition()
        self.replies = {}  # request id -> response message
        self.turn = None  # the running turn: {"text": [], "on_update", "permission"}
        self.write_lock = threading.Lock()

    # ------------------------------------------------------------------ transport
    def _send(self, msg):
        with self.write_lock:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()

    def _request(self, method, params, timeout):
        rid = next(self.ids)
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        with self.lock:
            ok = self.lock.wait_for(lambda: rid in self.replies or self.proc.poll() is not None, timeout)
            reply = self.replies.pop(rid, None)
        if reply is None:
            raise RuntimeError(f"{NAMES[self.agent]} stopped." if ok else f"{NAMES[self.agent]} didn't respond.")
        if "error" in reply:
            err = reply["error"]
            text = (err.get("message") or "error") + (f": {err['data']}" if err.get("data") else "")
            if "auth" in text.lower() or "login" in text.lower() or "sign in" in text.lower():
                raise Unavailable(f"{NAMES[self.agent]} isn't signed in. Sign in once in Terminal, then try again.")
            raise RuntimeError(f"{NAMES[self.agent]}: {text[:200]}")
        return reply.get("result") or {}

    def _read(self, proc):
        with proc.stdout:
            for line in proc.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "method" in msg and "id" in msg:
                    threading.Thread(target=self._answer_request, args=(msg,), daemon=True).start()
                elif "method" in msg:
                    self._notification(msg)
                elif "id" in msg:
                    with self.lock:
                        self.replies[msg["id"]] = msg
                        self.lock.notify_all()
        with self.lock:
            self.lock.notify_all()  # adapter exited: wake anyone waiting

    def _notification(self, msg):
        if msg["method"] != "session/update":
            return
        update = (msg.get("params") or {}).get("update") or {}
        turn = self.turn
        if turn is None:
            return
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            if content.get("type") == "text":
                turn["text"].append(content.get("text", ""))
        elif kind in ("tool_call", "tool_call_update", "plan"):
            try:
                turn["on_update"](kind, update)
            except Exception:
                pass

    def _answer_request(self, msg):
        """Requests from the agent. Only permission is supported; files and terminals run in the agent's own harness."""
        if msg["method"] == "session/request_permission":
            params = msg.get("params") or {}
            options = params.get("options") or []
            turn = self.turn
            chosen = None
            if turn is not None and not turn.get("cancelled"):
                try:
                    chosen = turn["permission"](params.get("toolCall") or {}, options)
                except Exception:
                    chosen = None
            if chosen is None or chosen not in {o.get("optionId") for o in options}:
                reject = next((o["optionId"] for o in options if o.get("kind") == "reject_once"), None)
                outcome = {"outcome": "selected", "optionId": reject} if reject and not (turn or {}).get("cancelled") \
                    else {"outcome": "cancelled"}
            else:
                outcome = {"outcome": "selected", "optionId": chosen}
            result = {"outcome": outcome}
            self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        else:
            self._send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "not supported"}})

    # ------------------------------------------------------------------ lifecycle
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, timeout=60):
        command = self.command
        if command is None:
            adapter = find_adapter(self.agent)
            if not adapter:
                raise Unavailable(f"The {NAMES[self.agent]} connector isn't installed on this Mac.")
            command = _command(adapter)
        env = dict(os.environ, PATH=os.pathsep.join([os.environ.get("PATH", "")] + SEARCH))
        try:
            self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, text=True, bufsize=1, cwd=self.cwd, env=env)
        except OSError as exc:
            raise Unavailable(f"Couldn't start {NAMES[self.agent]}: {exc.strerror}.") from None
        self.replies, self.session_id = {}, None
        threading.Thread(target=self._read, args=(self.proc,), daemon=True, name=f"acp-{self.agent}").start()
        try:
            self._request("initialize", {"protocolVersion": 1, "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}}, timeout)
            self.session_id = self._request("session/new", {"cwd": self.cwd, "mcpServers": []}, timeout)["sessionId"]
        except Exception:
            self.kill()
            raise

    def prompt(self, text, *, on_update=lambda kind, update: None, permission=lambda tool, options: None,
               timeout=1800):
        """One turn. -> (reply text, stop reason). permission(tool_call, options) -> optionId or None (reject)."""
        if not self.alive() or not self.session_id:
            self.start()
        self.turn = {"text": [], "on_update": on_update, "permission": permission, "cancelled": False}
        try:
            result = self._request("session/prompt", {"sessionId": self.session_id,
                                                      "prompt": [{"type": "text", "text": text}]}, timeout)
            stop = result.get("stopReason", "end_turn")
            return "".join(self.turn["text"]).strip(), ("cancelled" if self.turn["cancelled"] else stop)
        except RuntimeError:
            if self.turn["cancelled"]:
                return "".join(self.turn["text"]).strip(), "cancelled"
            raise
        finally:
            self.turn = None

    def cancel(self):
        """Stop the running turn: ask politely, then kill the adapter if it hasn't settled."""
        turn = self.turn
        if turn is None or not self.alive():
            return
        turn["cancelled"] = True
        try:
            self._send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": self.session_id}})
        except (OSError, ValueError):
            pass

        def reap():
            import time
            end = time.monotonic() + CANCEL_GRACE
            while time.monotonic() < end:
                if self.turn is not turn:
                    return
                time.sleep(0.1)
            self.kill()
        threading.Thread(target=reap, daemon=True).start()

    def kill(self):
        proc = self.proc
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=2)
                proc.stdin.close()
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        self.session_id = None
        with self.lock:
            self.lock.notify_all()
