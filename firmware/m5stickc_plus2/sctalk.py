"""
SC-OS edge protocol for a device, with no hardware imports, so it runs under
MicroPython on the device and CPython in tests.

The device reports a threshold as a stripped capsule, keeps the task the master
sends back, and answers it with a task_result carrying the outcome. Frames are
one JSON object per line, as run_gateway.py expects:

    device -> gateway   {"src": <name>, "cap": {...}}
    gateway -> device   {"dst": <name>, "cap": {...}}
"""

try:
    import ujson as json
except ImportError:
    import json

MASTER = "alice"


def _clip(text, n):
    return text if len(text) <= n else text[:n]


class Talk:
    def __init__(self, name, boot_tag, send_line):
        """
        name: this device's agent name (16 chars max, [a-z0-9-]).
        boot_tag: a few random hex characters, so message ids differ across reboots
                  (the gateway rejects an id it has already seen from this device).
        send_line: writes one line to the link.
        """
        self.name = name
        self.boot_tag = boot_tag
        self.send_line = send_line
        self.count = 0
        self.task = None          # the task being carried out: {"id", "t", "s"}
        self.acked = []           # ids of our messages the master acknowledged

    def _next_id(self):
        self.count += 1
        return _clip("%s%d" % (self.boot_tag, self.count), 16)

    def _send(self, cap):
        self.send_line(json.dumps({"src": self.name, "cap": cap}))
        return cap["id"]

    def report(self, topic, statement, confidence, trigger="threshold", intent="inform"):
        return self._send({
            "v": "1.0", "id": self._next_id(), "to": MASTER, "i": intent,
            "t": _clip(topic, 32), "c": round(float(confidence), 2),
            "s": _clip(statement, 120), "tr": trigger,
        })

    def complete(self, success, detail=""):
        """Report the outcome of the current task. Returns the message id, or None if no task."""
        if self.task is None:
            return None
        task, self.task = self.task, None
        cap = {
            "v": "1.0", "id": self._next_id(), "to": MASTER, "i": "inform",
            "t": task["t"], "c": 1.0,
            "s": _clip(("done: " if success else "failed: ") + task["s"], 120),
            "tr": "task_result", "re": task["id"],
            "o": "success" if success else "failure",
        }
        if detail:
            cap["od"] = _clip(detail, 60)
        return self._send(cap)

    def receive_line(self, line):
        """
        Handle one line from the link. Returns what happened:
        "task", "ack", "refuse", "other", or None for lines that aren't ours.
        """
        start = line.find("{")              # opening the port can leave junk bytes before a frame
        if start < 0:
            return None
        line = line[start:].strip()
        try:
            frame = json.loads(line)
        except ValueError:
            return None
        if not isinstance(frame, dict) or frame.get("dst") != self.name:
            return None
        cap = frame.get("cap") or {}
        if cap.get("tr") == "task" and cap.get("i") == "request":
            self.task = {"id": cap.get("id", ""), "t": cap.get("t", ""), "s": cap.get("s", "")}
            return "task"
        if cap.get("i") == "ack":
            if cap.get("re"):
                self.acked.append(cap["re"])
                del self.acked[:-16]
            return "ack"
        if cap.get("i") == "refuse":
            return "refuse"
        return "other"
