"""
Transport: the only place hardware exists.
Swap implementations without touching the kernel.
"""

import json
import socket
import threading
from pathlib import Path
from typing import Protocol


class Transport(Protocol):
    def send(self, peer: str, envelope: dict) -> None: ...
    def recv(self) -> tuple[str, dict]: ...


class FileTransport:
    """Same machine. Mailbox directory, one file per message."""

    def __init__(self, root: str, peer_id: str):
        self.root = Path(root)
        self.peer_id = peer_id
        self.inbox = self.root / f"{peer_id}.inbox"
        self.inbox.mkdir(parents=True, exist_ok=True)
        self._seq = 0

    def send(self, peer: str, envelope: dict) -> None:
        target = self.root / f"{peer}.inbox"
        target.mkdir(parents=True, exist_ok=True)
        self._seq += 1
        fname = target / f"{self.peer_id}-{self._seq:08d}.json"
        fname.write_text(json.dumps(envelope))

    def recv(self) -> tuple[str, dict]:
        for f in sorted(self.inbox.glob("*.json")):
            data = json.loads(f.read_text())
            sender = f.stem.split("-")[0]
            f.unlink()
            return sender, data
        raise BlockingIOError("no message")


MAX_FRAME_BYTES = 1 << 20   # 1 MiB per message


def local_addresses() -> list[str]:
    """IPv4 addresses other machines might reach this one on (not loopback). Best effort."""
    found: list[str] = []
    try:
        found += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    try:
        # no packet is sent: connecting a UDP socket only picks the outbound interface
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    return sorted({a for a in found if not a.startswith(("127.", "0."))})


class SocketTransport:
    """
    Network. TCP. Phone dials out; laptop listens.

    Framing: one JSON object per line. json.dumps escapes newlines inside
    strings, so a raw b"\\n" only ever ends a frame. Bytes after a frame are
    kept for the next recv.

    Point-to-point: one peer at a time; `peer` in send() is not used for
    routing. A listener that loses its peer accepts the next connection.
    For many peers at once, use SocketListener: one SocketTransport per
    accepted connection.

    send() is safe to call from several threads; recv() belongs to one reader.
    """

    def __init__(self, mode: str, host: str = "", port: int = 0):
        self.mode = mode
        self.conn: socket.socket | None = None
        self._buf = b""
        self._send_lock = threading.Lock()
        if mode == "accepted":      # built by from_connection()
            return
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if mode == "listen":
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind((host, port))
            self.sock.listen(8)
        elif mode == "connect":
            self.sock.connect((host, port))
            self.conn = self.sock
        else:
            raise ValueError("mode must be listen or connect")

    @classmethod
    def from_connection(cls, conn: socket.socket) -> "SocketTransport":
        """Wrap one already-accepted connection. Closed for good when it drops."""
        t = cls("accepted")
        t.sock = conn
        t.conn = conn
        return t

    def _ensure_conn(self) -> socket.socket:
        if self.conn is None:
            if self.mode != "listen":
                raise ConnectionError("connection closed")
            self.conn, _ = self.sock.accept()
            self._buf = b""
        return self.conn

    def _drop_conn(self) -> None:
        if self.mode in ("listen", "accepted") and self.conn is not None:
            self.conn.close()
            self.conn = None
        self._buf = b""

    def send(self, peer: str, envelope: dict) -> None:
        payload = (json.dumps(envelope, separators=(",", ":")) + "\n").encode()
        if len(payload) > MAX_FRAME_BYTES:
            raise ValueError(f"envelope is {len(payload)} bytes, max {MAX_FRAME_BYTES}")
        with self._send_lock:
            conn = self._ensure_conn()
            try:
                conn.sendall(payload)
            except OSError:
                self._drop_conn()
                raise

    def recv(self) -> tuple[str, dict]:
        conn = self._ensure_conn()
        while b"\n" not in self._buf:
            if len(self._buf) > MAX_FRAME_BYTES:
                self._drop_conn()
                raise ConnectionError(f"frame exceeds {MAX_FRAME_BYTES} bytes")
            try:
                chunk = conn.recv(65536)
            except OSError:
                self._drop_conn()
                raise
            if not chunk:
                self._drop_conn()
                raise ConnectionError("peer closed")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        env = json.loads(line.decode())
        # NOTE: pubkey_id is self-declared by the sender, not verified here.
        return env.get("pubkey_id", "unknown"), env

    def close(self) -> None:
        if self.conn is not None and self.conn is not self.sock:
            self.conn.close()
        self.conn = None
        self.sock.close()


class SocketListener:
    """Accepts many peers. Each accept() returns its own SocketTransport."""

    def __init__(self, host: str, port: int, poll_seconds: float = 1.0):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(16)
        self.sock.settimeout(poll_seconds)   # so the accept loop can notice shutdown

    def accept(self) -> tuple[SocketTransport, tuple] | None:
        """A new connection, or None if none arrived within poll_seconds."""
        try:
            conn, addr = self.sock.accept()
        except TimeoutError:
            return None
        conn.settimeout(socket.getdefaulttimeout())
        return SocketTransport.from_connection(conn), addr

    def close(self) -> None:
        self.sock.close()