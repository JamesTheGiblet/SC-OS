"""
Signed capsule envelope. Ed25519 over canonical JSON.
"""

import base64
import hashlib
import json
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def canonical(obj: dict) -> bytes:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def digest(capsule_wire: dict) -> str:
    """SHA-256 hex of the capsule's canonical JSON."""
    return hashlib.sha256(canonical(capsule_wire)).hexdigest()


@dataclass(frozen=True)
class Envelope:
    capsule: dict
    signature: bytes
    pubkey_id: str

    def to_wire(self) -> dict:
        return {
            "capsule": self.capsule,
            "sig": base64.b64encode(self.signature).decode("ascii"),
            "alg": "ed25519",
            "pubkey_id": self.pubkey_id,
        }


def sign(capsule_wire: dict, priv: Ed25519PrivateKey, pubkey_id: str) -> Envelope:
    return Envelope(capsule_wire, priv.sign(canonical(capsule_wire)), pubkey_id)


def open_envelope(wire: dict) -> Envelope:
    if wire.get("alg", "ed25519") != "ed25519":
        raise ValueError(f"unsupported alg: {wire['alg']}")
    return Envelope(
        capsule=wire["capsule"],
        signature=base64.b64decode(wire["sig"]),
        pubkey_id=wire["pubkey_id"],
    )


def verify(env: Envelope, pub: Ed25519PublicKey) -> bool:
    try:
        pub.verify(env.signature, canonical(env.capsule))
        return True
    except InvalidSignature:
        return False
