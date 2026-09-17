"""
M5StickC PLUS2 as an SC-OS edge device (MicroPython), over USB serial to run_gateway.py.

Tilt the stick past 40 degrees: it reports "tilt_risk" as a threshold (not while a
task waits). Alice's verify rule sends a task back; it shows on the screen and the
LED blinks. Press A (the big front button) if the report was real, B (the side
button) if it wasn't. The outcome goes back to Alice and moves the rule's trust.
Tilt back under 20 degrees to re-arm. Button C (power, short press) turns the
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
from sctalk import Talk
import sensors as sensor_info
from sensors import Buzzer, Sensors

HOLD = Pin(4, Pin.OUT, value=1)          # keep power on when running from the battery
LED = Pin(19, Pin.OUT, value=0)

TILT_REPORT_DEG = 40
TILT_REARM_DEG = 20
SAMPLE_MS = 200


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

    def draw(self, name, clock, tilt, accel, gyro, temp, chip, volts, armed, link, task, last):
        def put(*args, **kw):
            if self.rows.put(*args, **kw):
                self.between_rows()
        t = "%02d:%02d:%02d" % clock[3:6] if clock else "--:--:--"
        put("head", 0, 14, "%-21s%s" % (name, t), tft.WHITE, tft.BLUE)
        if tilt is None:
            put("tilt", 16, 20, "NO IMU", tft.RED, big=True)
        else:
            colour = tft.YELLOW if tilt > TILT_REPORT_DEG else (tft.GREEN if armed else tft.GREY)
            put("tilt", 16, 20, "TILT %3d" % tilt, colour, big=True)
        if accel:
            put("acc", 38, 11, "acc %+5.2f %+5.2f %+5.2f g" % accel, tft.CYAN)
            put("gyr", 49, 11, "gyr %+5d %+5d %+5d dps" % tuple(int(g) for g in gyro), tft.CYAN)
            put("env", 60, 11, "imu %4.1fC cpu %3dC  %4.2fV" % (temp, chip, volts), tft.WHITE)
        else:
            put("env", 60, 11, "cpu %3dC  bat %4.2fV" % (chip, volts), tft.WHITE)
        put("link", 72, 11, link, tft.GREY)
        if task:
            lines = wrap(task, 29)[:2]
            put("task1", 86, 11, lines[0], tft.BLACK, tft.YELLOW)
            put("task2", 97, 11, lines[1] if len(lines) > 1 else "", tft.BLACK, tft.YELLOW)
            put("task3", 108, 11, "A = yes          B = no", tft.BLACK, tft.YELLOW)
        else:
            put("task1", 86, 11, "", tft.BLACK)
            put("task2", 97, 11, "tilt past %d deg to report" % TILT_REPORT_DEG, tft.GREY)
            put("task3", 108, 11, "", tft.BLACK)
        colour = tft.GREEN if last.startswith("sent success") else (
            tft.RED if last.startswith("sent failure") else tft.GREY)
        put("last", 122, 13, last, colour)


def main():
    name = "m5-" + "".join("%02x" % b for b in machine.unique_id()[-3:])
    boot_tag = "".join("%02x" % b for b in os.urandom(2))
    talk = Talk(name, boot_tag, print)
    sensors = Sensors()
    buzzer = Buzzer()
    poll = select.poll()
    poll.register(sys.stdin, select.POLLIN)
    link = "link: waiting for alice"

    def read_link():
        """Handle every frame waiting on the link: tasks and acks from the gateway."""
        nonlocal link
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
                talk.describe(sensor_info.DESCRIPTION, sensor_info.ABSENT)
                note("sent sensor list (%d sensors)" % len(sensor_info.DESCRIPTION))

    screen = Screen(read_link)          # reads the link between row pushes, too
    if not sensors.imu_ok:
        note("IMU not found; buttons still report")
    note("%s ready: tilt past %d degrees to report" % (name, TILT_REPORT_DEG))
    talk.report("status", "%s online" % name, 1.0, trigger="announce")
    talk.describe(sensor_info.DESCRIPTION, sensor_info.ABSENT)

    armed = True
    backlight = True
    last = "last: none yet"
    held = set()
    blink_at = sample_at = time.ticks_ms()
    reading = (None, None, None, None)

    while True:
        read_link()

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
                if armed and tilt > TILT_REPORT_DEG and talk.task is None:
                    talk.report("tilt_risk", "tilted %d degrees" % tilt, 0.9)
                    armed = False
                    link = "sent tilt_risk %d deg" % tilt
                elif not armed and tilt < TILT_REARM_DEG:
                    armed = True
            clock = sensors.clock() if sensors.rtc_ok else None
            screen.draw(name, clock, tilt, accel, gyro, temp, sensors.chip_celsius(), sensors.battery_volts(),
                        armed, link, talk.task["s"] if talk.task else None, last)

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
