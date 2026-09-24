"""Same-user Unix socket into the running engine. Validates and enqueues; never runs Jev or actions itself."""
import errno
import fcntl
import json
import math
import os
import socket
import stat
import struct
import threading

RUN_DIR = os.path.expanduser("~/Library/Application Support/Hey Jev/run")
SOCK = os.path.join(RUN_DIR, "jev.sock")
LOCK = os.path.join(RUN_DIR, "jev.lock")
MAX_REQUEST = 16 * 1024
SEND_TIMEOUT = 5.0
MAX_WAIT = 120.0
MAX_CLIENTS = 16
SOL_LOCAL, LOCAL_PEERCRED = 0, 1  # <sys/un.h>; returns struct xucred


def peer_uid(conn):
    raw = conn.getsockopt(SOL_LOCAL, LOCAL_PEERCRED, 76)
    version, uid = struct.unpack_from("II", raw)
    if version != 0:  # XUCRED_VERSION
        raise OSError("unexpected peer credential version")
    return uid


def _private_dir(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise PermissionError(f"{path} is not a directory owned by this user")
    os.chmod(path, 0o700)


class Bridge:
    def __init__(self, engine, run_dir=RUN_DIR):
        self.engine = engine
        self.sock_path = os.path.join(run_dir, "jev.sock")
        self.lock_path = os.path.join(run_dir, "jev.lock")
        self.run_dir = run_dir
        self.slots = threading.BoundedSemaphore(MAX_CLIENTS)
        self.server = None
        self.lock_fd = None

    def start(self):
        """Take the single-instance lock, clear only our own stale socket, bind 0600, serve."""
        _private_dir(self.run_dir)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise RuntimeError("another Hey Jev instance owns the command socket")
        self.lock_fd = fd
        try:
            st = os.lstat(self.sock_path)
            if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid():
                raise PermissionError(f"{self.sock_path} exists and is not our socket")
            os.unlink(self.sock_path)  # stale: we hold the lock, so no live listener owns it
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            server.bind(self.sock_path)
        finally:
            os.umask(old)
        os.chmod(self.sock_path, 0o600)
        server.listen(MAX_CLIENTS)
        self.server = server
        threading.Thread(target=self._accept, daemon=True).start()

    def stop(self):
        if self.server:
            self.server.close()
            self.server = None
            try:
                os.unlink(self.sock_path)
            except FileNotFoundError:
                pass
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None

    def _accept(self):
        while self.server:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            if not self.slots.acquire(blocking=False):
                try:  # bounded; a client that already hung up must not take down the only accept loop
                    conn.settimeout(1.0)
                    self._reply(conn, {"v": 1, "state": "busy", "detail": "too_many_clients"})
                except OSError:
                    pass
                finally:
                    conn.close()
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            if peer_uid(conn) != os.getuid():
                return
            conn.settimeout(SEND_TIMEOUT)
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_REQUEST:
                    return self._reply(conn, {"v": 1, "state": "rejected", "detail": "request_too_large"})
            line = buf.split(b"\n", 1)[0]
            self._reply(conn, self.handle(line))
        except (OSError, socket.timeout):
            pass
        finally:
            conn.close()
            self.slots.release()

    def handle(self, line):
        """One request line -> one response dict. Unknown versions, ops and fields are rejected."""
        try:
            req = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return {"v": 1, "state": "rejected", "detail": "not_json"}
        if not isinstance(req, dict) or req.get("v") != 1:
            return {"v": 1, "state": "rejected", "detail": "bad_version"}
        op, rid = req.get("op"), req.get("id")
        allowed = {"command": {"v", "op", "id", "text", "wait"}, "status": {"v", "op", "id", "wait"},
                   "cancel": {"v", "op", "id"}}
        if not isinstance(op, str) or op not in allowed or set(req) - allowed[op]:
            return {"v": 1, "state": "rejected", "detail": "bad_op_or_field"}
        if not isinstance(rid, str) or not 0 < len(rid) <= 128:
            return {"v": 1, "state": "rejected", "detail": "bad_id"}
        wait = req.get("wait", 0)
        if isinstance(wait, bool) or not isinstance(wait, (int, float)) or not math.isfinite(wait) or wait < 0:
            return {"v": 1, "state": "rejected", "detail": "bad_wait"}
        wait = min(float(wait), MAX_WAIT)
        if op == "cancel":
            return self.engine.cancel(rid)
        if op == "command":
            text = req.get("text")
            if not isinstance(text, str) or not text.strip():
                return {"v": 1, "id": rid, "state": "rejected", "detail": "bad_text"}
            first = self.engine.submit(text, "cli", rid)
            if first["state"] in ("busy", "id_conflict"):
                return first
        return self.engine.wait(rid, wait) if wait else self.engine.status(rid)

    @staticmethod
    def _reply(conn, obj):
        data = json.dumps(obj, ensure_ascii=False).encode() + b"\n"
        if len(data) > 64 * 1024:
            data = json.dumps({"v": 1, "id": obj.get("id"), "state": obj.get("state"),
                               "detail": "response_too_large; query status"}).encode() + b"\n"
        conn.sendall(data)
