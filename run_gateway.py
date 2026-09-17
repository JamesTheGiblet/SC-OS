"""
Edge gateway. Start run_alice.py first.

    python run_gateway.py --serial COM5 [--baud 115200] [--host 127.0.0.1] [--port 7707]
    python run_gateway.py --stdio                       # frames on stdin/stdout, no device needed

Reads one JSON frame per line from the device link, upgrades each stripped
capsule and sends it to Alice as agent://<src>, over that device's own signed
session. Alice's replies and tasks are downgraded and written back as frames.

    device -> gateway   {"src": "m5-a1b2c3", "cap": {"v": "1.0", "id": "r1", "to": "alice", ...}}
    gateway -> device   {"dst": "m5-a1b2c3", "cap": {..., "re": "r1"}}

Lines that don't start with "{" (boot messages, a REPL banner) are ignored.
Logs go to stderr, so with --stdio stdout carries only frames.
Serial needs pyserial: pip install pyserial.
"""

import argparse
import json
import sys
import time
from pathlib import Path

from edge.gateway import EdgeGateway
from hal.transport import SocketTransport
from peer import PeerRejected
from validator import CapsuleRejected

ROOT = Path(__file__).resolve().parent
ALICE = "agent://alice"
FRAME_GAP_SECONDS = 0.05


def log(msg: str) -> None:
    print(f"[gateway] {msg}", file=sys.stderr, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    link = ap.add_mutually_exclusive_group(required=True)
    link.add_argument("--serial", metavar="PORT", help="serial port of the device, e.g. COM5 or /dev/ttyUSB0")
    link.add_argument("--stdio", action="store_true", help="read frames from stdin, write to stdout")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--host", default="127.0.0.1", help="Alice's address")
    ap.add_argument("--port", type=int, default=7707)
    args = ap.parse_args()

    if args.serial:
        try:
            import serial
        except ImportError:
            log("FAIL pyserial is not installed: pip install pyserial")
            return 1
        try:
            # Set DTR/RTS before opening: toggling them on open resets an ESP32.
            dev = serial.Serial()
            dev.port, dev.baudrate, dev.timeout = args.serial, args.baud, None
            dev.dtr = dev.rts = False
            dev.open()
        except serial.SerialException as e:
            log(f"FAIL cannot open {args.serial}: {e}")
            return 1
        lines = (raw.decode("utf-8", "replace") for raw in iter(dev.readline, b""))

        def write(frame: dict) -> None:
            dev.write((json.dumps(frame, separators=(",", ":")) + "\n").encode())
            dev.flush()
            time.sleep(FRAME_GAP_SECONDS)       # let a small device drain its input buffer
        where = f"serial {args.serial} at {args.baud} baud"
    else:
        lines = iter(sys.stdin.readline, "")

        def write(frame: dict) -> None:
            print(json.dumps(frame, separators=(",", ":")), flush=True)
        where = "stdin/stdout"

    gateway = EdgeGateway(ALICE, lambda: SocketTransport("connect", args.host, args.port),
                          ROOT, write, log)
    log(f"bridging {where} <-> {ALICE} at {args.host}:{args.port}")
    try:
        for line in lines:
            start = line.find("{")              # a port opening can leave junk bytes before a frame
            if start < 0 or "#" in line[:start]:  # "# ..." lines are device notes for a person
                continue
            line = line[start:].strip()
            try:
                c = gateway.handle_frame(json.loads(line))
                log(f"[{c.sender.removeprefix('agent://')}] {c.intent.value.upper()} {c.semantics.topic} "
                    f"trigger={c.trigger.value}" + (f" outcome={c.outcome.status}" if c.outcome else "")
                    + " -> alice")
            except json.JSONDecodeError as e:
                log(f"REJECT not JSON: {e}")
            except CapsuleRejected as e:
                log(f"REJECT {e.code}: {e.detail}")
            except (PeerRejected, OSError) as e:
                log(f"FAIL cannot reach {ALICE} at {args.host}:{args.port}: {e}")
    except KeyboardInterrupt:
        log("stopping")
    finally:
        gateway.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
