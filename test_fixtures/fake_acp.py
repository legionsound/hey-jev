"""Fake ACP v1 agent for tests. Scripted stdio JSON-RPC, newline-delimited.

Speaks ACP v1 (what codex-acp / claude-agent-acp speak): `initialize`,
`session/new`, `session/prompt` (streams `session/update` notifications, then
responds with `stopReason`), `session/cancel` notification, and an
agent-initiated `session/request_permission` round trip.

Prompt text selects the script (first text block of `session/prompt` params):
- default: two `agent_message_chunk` + one `tool_call`/`tool_call_update`,
  finishes `end_turn`.
- contains "permission": emits a tool call, asks `session/request_permission`
  (options allow_once / reject_once), streams the granted option id, finishes
  `end_turn`. No reply in 10 s, or outcome cancelled -> finishes `cancelled`.
- contains "hang": never responds, ignores `session/cancel`. Client must kill.
  Tests the deadline/kill path.
- contains "crash": exits(2) immediately with no response.

Usage: `python3 test_fixtures/fake_acp.py`, then drive over stdio.
Not a test itself; acp_client tests spawn it.
"""

import json
import sys
import threading
import time

PROTOCOL_VERSION = 1


class Fake:
    def __init__(self):
        self.out_lock = threading.Lock()
        self.next_id = 1000  # agent-initiated request ids
        self.next_sess = 0
        self.pending = {}  # request id -> {"event": Event, "result": obj}
        self.cancel = threading.Event()
        self.stdin_lock = threading.Lock()

    def send(self, obj):
        with self.out_lock:
            sys.stdout.write(json.dumps(obj) + "\n")
            sys.stdout.flush()

    def notify_update(self, session_id, update):
        self.send({"jsonrpc": "2.0", "method": "session/update",
                   "params": {"sessionId": session_id, "update": update}})

    def handle(self, msg):
        mid = msg.get("id")
        method = msg.get("method")
        if method is None and ("result" in msg or "error" in msg):
            self.on_response(msg)  # client reply to our permission request
            return
        if method == "initialize":
            self.send({"jsonrpc": "2.0", "id": mid,
                       "result": {"protocolVersion": PROTOCOL_VERSION,
                                  "agentCapabilities": {}}})
        elif method == "session/new":
            self.next_sess += 1
            self.send({"jsonrpc": "2.0", "id": mid,
                       "result": {"sessionId": f"fake-sess-{self.next_sess}"}})
        elif method == "session/prompt":
            params = msg.get("params", {})
            t = threading.Thread(target=self.run_prompt,
                                 args=(mid, params), daemon=True)
            t.start()
        elif method == "session/cancel":
            # "hang" script ignores this on purpose (wedged adapter).
            params = msg.get("params", {})
            if "hang" not in self.prompt_text(params):
                self.cancel.set()
        elif mid is not None:
            self.send({"jsonrpc": "2.0", "id": mid,
                       "error": {"code": -32601,
                                 "message": f"unknown method {method}"}})

    def on_response(self, msg):
        # Client reply to our session/request_permission.
        rid = msg.get("id")
        slot = self.pending.pop(rid, None)
        if slot is not None:
            slot["result"] = msg.get("result")
            slot["event"].set()

    @staticmethod
    def prompt_text(params):
        prompt = params.get("prompt", [])
        if isinstance(prompt, str):
            return prompt
        parts = []
        for block in prompt:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)

    def request_permission(self, session_id, title):
        rid = self.next_id
        self.next_id += 1
        slot = {"event": threading.Event(), "result": None}
        self.pending[rid] = slot
        self.send({"jsonrpc": "2.0", "id": rid,
                   "method": "session/request_permission",
                   "params": {"sessionId": session_id,
                              "toolCall": {"title": title, "kind": "execute"},
                              "options": [
                                  {"optionId": "allow", "name": "Allow",
                                   "kind": "allow_once"},
                                  {"optionId": "reject", "name": "Reject",
                                   "kind": "reject_once"}]}})
        if not slot["event"].wait(timeout=10):
            return None
        return slot["result"]

    def run_prompt(self, mid, params):
        session_id = params.get("sessionId", "fake-sess-0")
        text = self.prompt_text(params)
        if "crash" in text:
            sys.stdout.flush()
            sys.stderr.flush()
            import os
            os._exit(2)
        if "hang" in text:
            time.sleep(3600)  # wedged; client must kill. Ignores cancel.
            return
        self.cancel.clear()
        msg_id = "msg-agent-1"
        chunks = ["Fake answer part one. ", "Fake answer part two."]
        if "permission" in text:
            tc = "tool-1"
            self.notify_update(session_id, {"sessionUpdate": "tool_call",
                                            "toolCallId": tc, "title": "ls",
                                            "kind": "execute",
                                            "status": "in_progress"})
            res = self.request_permission(session_id, "Run `ls`?")
            outcome = (res or {}).get("outcome", {}) if isinstance(res, dict) else {}
            if outcome.get("outcome") != "selected":
                self.notify_update(session_id,
                                   {"sessionUpdate": "tool_call_update",
                                    "toolCallId": tc, "status": "failed"})
                self.send({"jsonrpc": "2.0", "id": mid,
                           "result": {"stopReason": "cancelled"}})
                return
            opt = outcome.get("optionId", "")
            self.notify_update(session_id,
                               {"sessionUpdate": "tool_call_update",
                                "toolCallId": tc, "status": "completed"})
            chunks = [f"Permission granted: {opt}. ", "Done."]
        else:
            tc = "tool-1"
            self.notify_update(session_id, {"sessionUpdate": "tool_call",
                                            "toolCallId": tc, "title": "ls",
                                            "kind": "execute",
                                            "status": "in_progress"})
            self.notify_update(session_id,
                               {"sessionUpdate": "tool_call_update",
                                "toolCallId": tc, "status": "completed"})
        for ch in chunks:
            if self.cancel.is_set():
                self.send({"jsonrpc": "2.0", "id": mid,
                           "result": {"stopReason": "cancelled"}})
                return
            self.notify_update(session_id,
                               {"sessionUpdate": "agent_message_chunk",
                                "messageId": msg_id,
                                "content": {"type": "text", "text": ch}})
            time.sleep(0.05)
        if self.cancel.is_set():
            reason = "cancelled"
        else:
            reason = "end_turn"
        self.send({"jsonrpc": "2.0", "id": mid,
                   "result": {"stopReason": reason}})


def main():
    fake = Fake()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            fake.handle(json.loads(line))
        except (json.JSONDecodeError, AttributeError):
            continue


if __name__ == "__main__":
    main()
