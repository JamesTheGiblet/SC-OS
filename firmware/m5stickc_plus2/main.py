"""
M5StickC PLUS2 as an SC-OS edge device (MicroPython), over USB serial to run_gateway.py.

Tilt the stick past its tilt margin (40 degrees unless an operator setup changes it):
it reports "tilt_risk" as a threshold (not while a task waits). Alice's verify rule sends a task back; it shows on the screen and the
LED blinks. Press A (the big front button) if the report was real, B (the side
button) if it wasn't. The outcome goes back to Alice and moves the rule's trust.
Tilt back under half the margin to re-arm.

IR edge sensors look down at the front (left pin 26; right pin 36 once wired with
a 10 kohm pull-up). When one loses the surface the stick beeps at once and shows EDGE on screen, then
reports "edge_risk" (confirm with A or B); it re-arms after both see surface
for a second. Button C (power, short press) turns the
backlight on and off.

The screen shows the clock, tilt, accelerometer, gyroscope, IMU and chip
temperature, battery voltage, the link to Alice, the waiting task and the last
outcome. The buzzer beeps twice when a task arrives, chirps on success and gives
a low tone on failure.

Pins: hold power 4, red LED 19 (active high); screen, sensors and buttons in
st7789.py and sensors.py. Lines that aren't JSON frames are notes for a person;
the gateway ignores them.
"""

import os
import select
import sys
import time

import machine
from machine import Pin

import st7789 as tft
import json

from sctalk import Talk, apply_setup
import sensors as sensor_info
from sensors import Buzzer, EdgeSensors, ReadingSender, Sensors, iso_utc

EDGE_REARM_MS = 1000          # both sensors on surface this long before another edge report

HOLD = Pin(4, Pin.OUT, value=1)          # keep power on when running from the battery
LED = Pin(19, Pin.OUT, value=0)

SETUP_FILE = "setup.json"            # operator overrides, kept across reboots


def load_overrides():
    try:
        with open(SETUP_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_overrides(overrides):
    with open(SETUP_FILE, "w") as f:
        json.dump(overrides, f)


def tilt_thresholds(description):
    """Report past the tilt sensor's margin_high; re-arm below half of it."""
    for d in description:
        if d["id"] == "tilt":
            return d["margin_high"], d["margin_high"] / 2
    return 40, 20
SAMPLE_MS = 200


def tz_offset_minutes():
    """Local time offset written by deploy.py; the clock itself keeps UTC."""
    try:
        with open("tz.txt") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def local_hms(clock, offset_min):
    seconds = (clock[3] * 3600 + clock[4] * 60 + clock[5] + offset_min * 60) % 86400
    return seconds // 3600, seconds // 60 % 60, seconds % 60


def note(text):
    print("# " + text)


def wrap(text, width):
    lines, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = (line + " " + word).strip()
    lines.append(line)
    return lines


class Screen:
    def __init__(self, between_rows):
        """between_rows() runs after each row pushed to the panel, to keep reading serial input."""
        self.d = tft.Display()
        self.rows = tft.Lines(self.d)
        self.between_rows = between_rows

    def draw(self, name, hms, tilt, accel, gyro, temp, chip, volts, tof, armed, link, task, last, report_deg):
        def put(*args, **kw):
            if self.rows.put(*args, **kw):
                self.between_rows()
        t = "%02d:%02d:%02d" % hms if hms else "--:--:--"
        put("head", 0, 14, "%-21s%s" % (name, t), tft.WHITE, tft.BLUE)
        if tilt is None:
            put("tilt", 16, 20, "NO IMU", tft.RED, big=True)
        else:
            colour = tft.YELLOW if tilt > report_deg else (tft.GREEN if armed else tft.GREY)
            put("tilt", 16, 20, "TILT %3d" % tilt, colour, big=True)
        if accel:
            put("acc", 38, 11, "acc %+5.2f %+5.2f %+5.2f g" % accel, tft.CYAN)
            put("gyr", 49, 11, "gyr %+5d %+5d %+5d dps" % tuple(int(g) for g in gyro), tft.CYAN)
            put("env", 60, 11, "imu %4.1fC cpu %3dC  %4.2fV" % (temp, chip, volts), tft.WHITE)
        else:
            put("env", 60, 11, "cpu %3dC  bat %4.2fV" % (chip, volts), tft.WHITE)
        put("tof", 72, 11, tof, tft.RED if "EDGE" in tof else tft.CYAN)
        put("link", 86, 11, link, tft.GREY)
        if task:
            put("task1", 97, 11, wrap(task, 29)[0], tft.BLACK, tft.YELLOW)
            put("task2", 108, 11, "A = yes          B = no", tft.BLACK, tft.YELLOW)
        else:
            put("task1", 97, 11, "tilt past %d deg to report" % report_deg, tft.GREY)
            put("task2", 108, 11, "", tft.BLACK)
        colour = tft.GREEN if last.startswith("sent success") else (
            tft.RED if last.startswith("sent failure") else tft.GREY)
        put("last", 122, 13, last, colour)


def main():
    name = "m5-" + "".join("%02x" % b for b in machine.unique_id()[-3:])
    boot_tag = "".join("%02x" % b for b in os.urandom(2))
    talk = Talk(name, boot_tag, print)
    sensors = Sensors()
    buzzer = Buzzer()
    edges = EdgeSensors()
    sender = ReadingSender()
    tz = tz_offset_minutes()
    overrides = load_overrides()
    base = (list(sensor_info.DESCRIPTION) + list(sensor_info.EDGE_DESCRIPTION)
            + ([sensor_info.TOF_DESCRIPTION] if sensors.tof else []))
    ids = [d["id"] for d in base]
    description, error = apply_setup(base, {k: v for k, v in overrides.items() if k in ids})
    if error:                               # a saved setup that no longer fits: back to defaults
        note("saved setup ignored: " + error)
        description, overrides = base, {}
    note("tof: " + ("VL53L0X on grove 32/33" if sensors.tof else "not connected"))
    report_deg, rearm_deg = tilt_thresholds(description)
    poll = select.poll()
    poll.register(sys.stdin, select.POLLIN)
    link = "link: waiting for alice"

    def read_link():
        """Handle every frame waiting on the link: tasks, setups and acks from the gateway."""
        nonlocal link, description, overrides, report_deg, rearm_deg
        while poll.poll(0):
            line = sys.stdin.readline()
            what = talk.receive_line(line)
            if what is None and line.strip():
                # no braces in the note, or the gateway would try to parse it as a frame
                note("ignored %d bytes: %s" % (len(line), repr(line[:40]).replace("{", "(").replace("}", ")")))
            elif what == "task":
                link = "alice: task received"
                buzzer.play(Buzzer.TASK)
                note("task: %s  (A = yes, B = no)" % talk.task["s"])
            elif what == "ack":
                link = "alice: ack %s" % (talk.acked[-1] if talk.acked else "")
            elif what == "refuse":
                link = "alice: refused"
                note("refused by the master")
            elif what == "describe":
                talk.describe(description, sensor_info.ABSENT)
                note("sent sensor list (%d sensors)" % len(description))
            elif what == "setup":
                setup, talk.setup = talk.setup, None
                new, error = apply_setup(description, setup["sensors"])
                if error:
                    talk.result(setup["re"], "__setup__", "apply setup", False, error)
                    link = "setup refused: " + error
                    note("setup refused: " + error)
                else:
                    description = new
                    for sid, fields in setup["sensors"].items():
                        overrides.setdefault(sid, {}).update(fields)
                    save_overrides(overrides)
                    report_deg, rearm_deg = tilt_thresholds(description)
                    talk.result(setup["re"], "__setup__", "apply setup", True,
                                "applied " + ", ".join(sorted(setup["sensors"])))
                    talk.describe(description, sensor_info.ABSENT)
                    link = "setup applied"
                    buzzer.play(Buzzer.SUCCESS)
                    note("setup applied: " + json.dumps(setup["sensors"]))

    screen = Screen(read_link)          # reads the link between row pushes, too
    if not sensors.imu_ok:
        note("IMU not found; buttons still report")
    note("%s ready: tilt past %d degrees to report" % (name, report_deg))
    talk.report("status", "%s online" % name, 1.0, trigger="announce")
    talk.describe(description, sensor_info.ABSENT)

    armed = True
    edge_armed, surface_since = True, time.ticks_ms()
    backlight = True
    last = "last: none yet"
    held = set()
    blink_at = sample_at = time.ticks_ms()
    reading = (None, None, None, None)

    while True:
        read_link()

        # edges: act locally at once, then tell Alice (one question at a time)
        edge = edges.update()
        sides = [sid.replace("edge_", "") for sid in sorted(edge) if edge[sid]]
        if sides:
            surface_since = time.ticks_ms()
            if edge_armed:
                edge_armed = False
                buzzer.play(Buzzer.EDGE)
                where = " and ".join(sides)
                note("EDGE under " + where)
                if talk.task is None:
                    talk.report("edge_risk", "edge under %s sensor%s" % (where, "s" if len(sides) > 1 else ""), 0.9)
                    link = "sent edge_risk " + where
                else:
                    link = "EDGE " + where + " (task waiting)"
        elif not edge_armed and time.ticks_diff(time.ticks_ms(), surface_since) >= EDGE_REARM_MS:
            edge_armed = True

        # buttons, on press
        now_held = set(sensors.pressed())
        pressed = now_held - held
        held = now_held
        if talk.task is not None and "A" in pressed:
            talk.complete(True, "confirmed by button A")
            last, link = "sent success (A)", "alice: result sent"
            buzzer.play(Buzzer.SUCCESS)
            note("reported success")
        elif talk.task is not None and "B" in pressed:
            talk.complete(False, "denied by button B")
            last, link = "sent failure (B)", "alice: result sent"
            buzzer.play(Buzzer.FAILURE)
            note("reported failure")
        if "C" in pressed:
            backlight = not backlight
            screen.d.backlight(backlight)

        # sensors and screen
        if time.ticks_diff(time.ticks_ms(), sample_at) >= SAMPLE_MS:
            sample_at = time.ticks_ms()
            if sensors.imu_ok:
                try:
                    reading = sensors.imu()
                except OSError:
                    reading = (None, None, None, None)
            accel, gyro, temp, tilt = reading
            # one question at a time: a new report would replace the task still waiting
            if tilt is not None:
                if armed and tilt > report_deg and talk.task is None:
                    talk.report("tilt_risk", "tilted %d degrees" % tilt, 0.9)
                    armed = False
                    link = "sent tilt_risk %d deg" % tilt
                elif not armed and tilt < rearm_deg:
                    armed = True
            clock = sensors.clock() if sensors.rtc_ok else None
            chip, volts = sensors.chip_celsius(), sensors.battery_volts()
            mm = sensors.distance_mm()
            tof = ("tof%5dmm" % mm) if mm is not None else ("tof  none " if sensors.tof else "tof  --   ")
            tof += "  edge " + " ".join("%s:%s" % (sid[5].upper(), "EDGE" if edge[sid] else "ok")
                                        for sid in sorted(edge))
            screen.draw(name, local_hms(clock, tz) if clock else None, tilt, accel, gyro, temp, chip, volts, tof,
                        armed, link, talk.task["s"] if talk.task else None, last, report_deg)

            # readings for Alice's plausibility checks, when they change or once a minute
            values = {"chip_temp": round(chip, 1), "battery": round(volts, 3)}
            if accel:
                values.update({"accel": [round(a, 3) for a in accel], "gyro": [round(g, 1) for g in gyro],
                               "tilt": round(tilt, 1), "imu_temp": round(temp, 1)})
            if clock:
                values["clock"] = iso_utc(clock)
            if mm is not None:
                values["tof"] = mm
            for sid in edge:
                values[sid] = 1 if edge[sid] else 0
            now_ms = time.ticks_ms()
            if sender.due(values, now_ms):
                talk.readings(values)
                sender.mark(values, now_ms)

        # LED blinks while a task waits
        if talk.task is not None:
            if time.ticks_diff(time.ticks_ms(), blink_at) > 250:
                LED.value(1 - LED.value())
                blink_at = time.ticks_ms()
        else:
            LED.value(0)

        # wait for input rather than sleeping: the stdin buffer holds only ~260 bytes,
        # and an ACK and a task arrive back to back
        buzzer.tick()
        poll.poll(20)


main()
