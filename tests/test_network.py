"""
Getting ready for two machines: peers.json entries, clock offset from a hello,
node files found from the project root, and a signed session over this
machine's network address instead of loopback.
Run from the project root: python tests/test_network.py
"""

import socket
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from boot.discovery import load_peers, parse_peer, save_peers
from handshake import clock_offset, make_hello
from hal.transport import SocketListener, SocketTransport, local_addresses
from peer import Node
from store import Store


def test_parse_peer():
    assert parse_peer("192.168.1.20:7800", 7707) == ("192.168.1.20", 7800)
    assert parse_peer("laptop.local", 7707) == ("laptop.local", 7707)
    assert parse_peer(" 10.0.0.5:9 ", 7707) == ("10.0.0.5", 9)
    assert parse_peer("[fe80::1]:7800", 7707) == ("fe80::1", 7800)
    assert parse_peer("[fe80::1]", 7707) == ("fe80::1", 7707)
    for bad in ("", "host:notaport"):
        try:
            parse_peer(bad, 7707)
            raise AssertionError(f"{bad!r} must be rejected")
        except ValueError:
            pass


def test_peers_file_round_trip():
    path = str(Path(tempfile.mkdtemp()) / "peers.json")
    assert load_peers(path) == []
    save_peers(["192.168.1.20:7707"], path)
    assert load_peers(path) == ["192.168.1.20:7707"]


def test_clock_offset():
    hello = make_hello("agent://bob", "agent://alice")
    assert clock_offset(hello, hello.created) == 0
    assert clock_offset(hello, hello.created + timedelta(seconds=40)) == -40   # sender is behind
    assert clock_offset(hello, hello.created - timedelta(seconds=12)) == 12    # sender is ahead


def test_local_addresses_are_not_loopback():
    for a in local_addresses():
        socket.inet_aton(a)
        assert not a.startswith("127.")


def test_run_scripts_use_project_root_from_any_directory():
    """Started from elsewhere, run_bob must use the project's keys, not make new ones."""
    elsewhere = tempfile.mkdtemp()
    out = subprocess.run([sys.executable, str(ROOT / "run_bob.py"), "--name", "nettest-cwd",
                          "--host", "127.0.0.1", "--port", "1"],
                         cwd=elsewhere, capture_output=True, text=True, timeout=60,
                         env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
    assert out.returncode == 1, out.stdout + out.stderr
    assert "FAIL cannot reach 127.0.0.1:1" in out.stdout, out.stdout + out.stderr
    assert "Traceback" not in out.stderr, out.stderr
    assert not (Path(elsewhere) / "store").exists() and not (Path(elsewhere) / "keys").exists()
    for leftover in (ROOT / "store" / "nettest-cwd.db", ROOT / "store" / "nettest-cwd.db-wal",
                     ROOT / "store" / "nettest-cwd.db-shm"):
        leftover.unlink(missing_ok=True)


def test_signed_session_over_a_network_address():
    addrs = local_addresses()
    if not addrs:
        print("    skipped: no non-loopback address")
        return
    tmp = Path(tempfile.mkdtemp())
    alice = Node("agent://alice", Store(str(tmp / "alice.db")), key_dir=str(tmp / "keys"),
                 pins_path=str(tmp / "alice.pins.json"))
    bob = Node("agent://bob", Store(str(tmp / "bob.db")), key_dir=str(tmp / "keys"),
               pins_path=str(tmp / "bob.pins.json"))

    listener = SocketListener("0.0.0.0", 0)
    port = listener.sock.getsockname()[1]
    got: dict = {}

    def serve():
        accepted = None
        while accepted is None:
            accepted = listener.accept()
        transport, addr = accepted
        session = alice.session(transport)
        got["hello"] = session.recv()
        got["addr"] = addr
        session.send(alice.hello("agent://bob"))
        transport.close()

    t = threading.Thread(target=serve)
    t.start()
    transport = SocketTransport("connect", addrs[0], port)
    session = bob.session(transport)
    session.send(bob.hello("agent://alice"))
    reply = session.recv()
    t.join(timeout=10)
    transport.close()
    listener.close()

    assert got["hello"].sender == "agent://bob" and reply.sender == "agent://alice"
    assert session.last_pin == "new"
    assert abs(clock_offset(reply)) < 5


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"{len(tests)} passed")
