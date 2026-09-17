"""
Peer discovery. Hardcoded for now; mDNS later.

peers.json lists peers as "host:port" strings, e.g. {"peers": ["192.168.1.20:7707"]}.
It is machine-specific, so it isn't committed.
"""

from pathlib import Path
import json


def load_peers(config_path: str = "peers.json") -> list[str]:
    p = Path(config_path)
    if not p.exists():
        return []
    return json.loads(p.read_text()).get("peers", [])


def save_peers(peers: list[str], config_path: str = "peers.json") -> None:
    Path(config_path).write_text(json.dumps({"peers": peers}, indent=2))


def parse_peer(entry: str, default_port: int) -> tuple[str, int]:
    """"host:port" or "host" -> (host, port). IPv6 literals go in brackets: "[::1]:7707"."""
    entry = entry.strip()
    if not entry:
        raise ValueError("empty peer entry")
    if entry.startswith("["):
        host, _, rest = entry[1:].partition("]")
        return host, int(rest[1:]) if rest.startswith(":") else default_port
    if entry.count(":") == 1:
        host, port = entry.split(":")
        return host, int(port)
    return entry, default_port
