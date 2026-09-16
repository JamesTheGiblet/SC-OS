"""
Transport: the only place hardware exists.
Swap implementations without touching the kernel.
"""

import json
import socket
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


class SocketTransport:
    """
    Network. TCP. Phone dials out; laptop listens.

    Framing: one JSON object per line. json.dumps escapes newlines inside
    strings, so a raw b"\\n" only ever ends a frame. Bytes after a frame are
    kept for the next recv.

    Point-to-point: one peer at a time; `peer` in send() is not used for
    routing. A listener that loses its peer accepts the next connection.
    """

    def __init__(self, mode: str, host: str, port: int):
        self.mode = mode
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.conn: socket.socket | None = None
        self._buf = b""
        if mode == "listen":
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind((host, port))
            self.sock.listen(8)
        elif mode == "connect":
            self.sock.connect((host, port))
            self.conn = self.sock
        else:
            raise ValueError("mode must be listen or connect")

    def _ensure_conn(self) -> socket.socket:
        if self.conn is None:
            self.conn, _ = self.sock.accept()
            self._buf = b""
        return self.conn

    def _drop_conn(self) -> None:
        if self.mode == "listen" and self.conn is not None:
            self.conn.close()
            self.conn = None
        self._buf = b""

    def send(self, peer: str, envelope: dict) -> None:
        payload = (json.dumps(envelope, separators=(",", ":")) + "\n").encode()
        if len(payload) > MAX_FRAME_BYTES:
            raise ValueError(f"envelope is {len(payload)} bytes, max {MAX_FRAME_BYTES}")
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