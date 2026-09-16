"""
SocketTransport framing over real localhost TCP.
Run from the project root: python tests/test_transport.py
"""

import json
import socket
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hal.transport import SocketTransport, MAX_FRAME_BYTES


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def frame(d: dict) -> bytes:
    return (json.dumps(d) + "\n").encode()


def raw_client(port: int, chunks: list[bytes]) -> threading.Thread:
    def run():
        with socket.create_connection(("127.0.0.1", port)) as c:
            for ch in chunks:
                c.sendall(ch)
    t = threading.Thread(target=run)
    t.start()
    return t


def test_two_frames_in_one_write():
    port = free_port()
    srv = SocketTransport("listen", "127.0.0.1", port)
    a, b = {"pubkey_id": "a", "n": 1}, {"pubkey_id": "b", "n": 2}
    t = raw_client(port, [frame(a) + frame(b)])
    assert srv.recv() == ("a", a)
    assert srv.recv() == ("b", b)
    t.join()
    srv.close()


def test_frame_split_across_writes():
    port = free_port()
    srv = SocketTransport("listen", "127.0.0.1", port)
    msg = {"pubkey_id": "a", "statement": "line1\nline2", "blob": "x" * 200_000}
    data = frame(msg)
    t = raw_client(port, [data[:10], data[10:5000], data[5000:]])
    assert srv.recv() == ("a", msg)
    t.join()
    srv.close()


def test_listener_accepts_next_peer_after_close():
    port = free_port()
    srv = SocketTransport("listen", "127.0.0.1", port)
    t = raw_client(port, [frame({"pubkey_id": "first"})])
    assert srv.recv()[0] == "first"
    t.join()
    try:
        srv.recv()
        raise AssertionError("expected ConnectionError")
    except ConnectionError:
        pass
    t = raw_client(port, [frame({"pubkey_id": "second"})])
    assert srv.recv()[0] == "second"
    t.join()
    srv.close()


def test_oversized_frame_rejected():
    port = free_port()
    srv = SocketTransport("listen", "127.0.0.1", port)
    t = raw_client(port, [b"x" * (MAX_FRAME_BYTES + 70_000)])
    try:
        srv.recv()
        raise AssertionError("expected ConnectionError")
    except ConnectionError as e:
        assert "exceeds" in str(e)
    t.join()
    srv.close()


def test_transport_to_transport_both_directions():
    port = free_port()
    srv = SocketTransport("listen", "127.0.0.1", port)
    got = []

    def server():
        got.append(srv.recv())
        srv.send("client", {"pubkey_id": "srv", "ack": True})

    th = threading.Thread(target=server)
    th.start()
    cli = SocketTransport("connect", "127.0.0.1", port)
    cli.send("srv", {"pubkey_id": "cli", "hello": 1})
    cli.send("srv", {"pubkey_id": "cli", "hello": 2})
    assert cli.recv() == ("srv", {"pubkey_id": "srv", "ack": True})
    th.join()
    assert got == [("cli", {"pubkey_id": "cli", "hello": 1})]
    assert srv.recv() == ("cli", {"pubkey_id": "cli", "hello": 2})
    cli.close()
    srv.close()


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"{len(tests)} passed")
