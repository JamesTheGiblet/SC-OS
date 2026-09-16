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

from hal.transport import SocketListener, SocketTransport, MAX_FRAME_BYTES


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


def test_listener_serves_many_peers_at_once():
    port = free_port()
    listener = SocketListener("127.0.0.1", port, poll_seconds=0.2)
    n = 6
    results: dict[int, tuple] = {}

    def serve_one():
        accepted = None
        while accepted is None:
            accepted = listener.accept()
        t, _ = accepted
        _, env = t.recv()
        t.send("x", {"pubkey_id": "srv", "echo": env["n"]})
        t.close()

    def client(i):
        c = SocketTransport("connect", "127.0.0.1", port)
        c.send("srv", {"pubkey_id": f"c{i}", "n": i})
        results[i] = c.recv()
        c.close()

    servers = [threading.Thread(target=serve_one) for _ in range(n)]
    clients = [threading.Thread(target=client, args=(i,)) for i in range(n)]
    for t in servers + clients:
        t.start()
    for t in servers + clients:
        t.join(timeout=10)
    listener.close()
    assert results == {i: ("srv", {"pubkey_id": "srv", "echo": i}) for i in range(n)}


def test_listener_accept_times_out_quietly():
    listener = SocketListener("127.0.0.1", free_port(), poll_seconds=0.1)
    assert listener.accept() is None
    listener.close()


def test_accepted_transport_does_not_reaccept_after_close():
    port = free_port()
    listener = SocketListener("127.0.0.1", port, poll_seconds=2)
    t = raw_client(port, [frame({"pubkey_id": "a"})])
    srv, _ = listener.accept()
    assert srv.recv()[0] == "a"
    t.join()
    try:
        srv.recv()
        raise AssertionError("expected ConnectionError")
    except ConnectionError:
        pass
    try:
        srv.recv()
        raise AssertionError("expected ConnectionError on a closed accepted transport")
    except ConnectionError as e:
        assert "closed" in str(e)
    listener.close()


def test_concurrent_sends_do_not_interleave():
    port = free_port()
    srv = SocketTransport("listen", "127.0.0.1", port)
    cli_holder = []
    th = threading.Thread(target=lambda: cli_holder.append(SocketTransport("connect", "127.0.0.1", port)))
    th.start()
    srv._ensure_conn()
    th.join()
    cli = cli_holder[0]
    big = "y" * 300_000

    def blast(tag):
        for i in range(10):
            srv.send("c", {"pubkey_id": tag, "i": i, "pad": big})

    senders = [threading.Thread(target=blast, args=(tag,)) for tag in ("a", "b", "c")]
    for t in senders:
        t.start()
    got = [cli.recv() for _ in range(30)]
    for t in senders:
        t.join()
    for tag in ("a", "b", "c"):
        assert [env["i"] for who, env in got if who == tag] == list(range(10))
    cli.close()
    srv.close()


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"{len(tests)} passed")
