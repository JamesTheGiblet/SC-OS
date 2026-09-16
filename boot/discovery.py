"""Peer discovery. Hardcoded for now; mDNS later."""

from pathlib import Path
import json


def load_peers(config_path: str = "peers.json") -> list[str]:
    p = Path(config_path)
    if not p.exists():
        return []
    return json.loads(p.read_text()).get("peers", [])


def save_peers(peers: list[str], config_path: str = "peers.json") -> None:
    Path(config_path).write_text(json.dumps({"peers": peers}, indent=2))