"""
Transport: the only place hardware exists.
Swap implementations without touching the kernel.
"""

import json
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


class SocketTransport:
    """Network. TCP. Phone dials out; laptop listens."""

    def __init__(self, mode: str, host: str, port: int):
        import socket
        self.mode = mode
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if mode == "listen":
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind((host, port))
            self.sock.listen(8)
            self.conn = None
        elif mode == "connect":
            self.sock.connect((host, port))
            self.conn = self.sock
        else:
            raise ValueError("mode must be listen or connect")

    def send(self, peer: str, envelope: dict) -> None:
        payload = (json.dumps(envelope) + "\n").encode()
        if self.conn is None:
            self.conn, _ = self.sock.accept()
        self.conn.sendall(payload)

    def recv(self) -> tuple[str, dict]:
        if self.conn is None:
            self.conn, _ = self.sock.accept()
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = self.conn.recv(4096)
            if not chunk:
                raise ConnectionError("peer closed")
            buf += chunk
        env = json.loads(buf.decode())
        return env.get("pubkey_id", "unknown"), env