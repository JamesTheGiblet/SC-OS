"""
Copy the firmware to an M5StickC PLUS2 running MicroPython, over its raw REPL.

    python firmware/m5stickc_plus2/deploy.py COM4            # copy sctalk.py and main.py, then reset
    python firmware/m5stickc_plus2/deploy.py COM4 --exec "print(1)"
    python firmware/m5stickc_plus2/deploy.py COM4 --remove-main   # stop auto-start (keeps sctalk.py)

Why not mpremote: opening the port toggles DTR/RTS, which resets this board
before the REPL can be reached. This keeps both low.
"""

import argparse
import sys
import time
from pathlib import Path

import serial

HERE = Path(__file__).resolve().parent
FILES = ("sctalk.py", "main.py")
CHUNK = 256


class RawRepl:
    def __init__(self, port: str):
        self.s = serial.Serial()
        self.s.port, self.s.baudrate, self.s.timeout = port, 115200, 0.2
        self.s.dtr = self.s.rts = False
        self.s.open()

    def _read_until(self, marker: bytes, timeout: float) -> bytes:
        buf = b""
        deadline = time.monotonic() + timeout
        while not buf.endswith(marker):
            if time.monotonic() > deadline:
                raise TimeoutError(f"waited for {marker!r}, got {buf[-200:]!r}")
            buf += self.s.read(1)
        return buf

    def enter(self) -> None:
        self.s.write(b"\r\x03\x03")             # stop a running program
        time.sleep(0.3)
        self.s.reset_input_buffer()
        self.s.write(b"\r\x01")                 # raw REPL
        self._read_until(b"raw REPL; CTRL-B to exit\r\n>", 5)

    def exec(self, code: str, timeout: float = 10) -> str:
        self.s.write(code.encode() + b"\x04")
        if self.s.read(2) != b"OK":
            raise RuntimeError("device did not accept the code")
        out = self._read_until(b"\x04", timeout)[:-1]
        err = self._read_until(b"\x04", timeout)[:-1]
        self._read_until(b">", timeout)
        if err:
            raise RuntimeError(err.decode(errors="replace"))
        return out.decode(errors="replace")

    def put(self, local: Path, remote: str) -> None:
        data = local.read_bytes()
        self.exec(f"f = open({remote!r}, 'wb')")
        for i in range(0, len(data), CHUNK):
            self.exec(f"f.write({data[i:i + CHUNK]!r})")
        self.exec("f.close()")
        size = self.exec(f"import os; print(os.stat({remote!r})[6])").strip()
        if int(size) != len(data):
            raise RuntimeError(f"{remote}: wrote {size} bytes, expected {len(data)}")

    def soft_reset(self) -> None:
        self.s.write(b"\x02\x04")               # leave raw REPL, soft reset: runs main.py

    def close(self) -> None:
        self.s.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("port")
    ap.add_argument("--exec", dest="code")
    ap.add_argument("--remove-main", action="store_true")
    args = ap.parse_args()

    repl = RawRepl(args.port)
    try:
        repl.enter()
        if args.code:
            print(repl.exec(args.code), end="")
            return 0
        if args.remove_main:
            repl.exec("import os\ntry:\n    os.remove('main.py')\nexcept OSError:\n    pass")
            print("main.py removed; the device boots to the REPL")
            return 0
        for name in FILES:
            repl.put(HERE / name, name)
            print(f"copied {name}")
        repl.soft_reset()
        print("reset: main.py is running")
        return 0
    except (TimeoutError, RuntimeError) as e:
        print(f"FAIL {e}", file=sys.stderr)
        return 1
    finally:
        repl.close()


if __name__ == "__main__":
    raise SystemExit(main())
