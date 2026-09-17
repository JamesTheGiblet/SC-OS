"""
M5StickC PLUS2 as an SC-OS edge device (MicroPython), over USB serial to run_gateway.py.

Tilt the stick past 40 degrees: it reports "tilt_risk" as a threshold (not while a task waits). Alice's
verify rule sends a task back; the LED blinks while the task waits. Press A (the
big front button) if the report was real, B (the side button) if it wasn't. The
outcome goes back to Alice and moves the rule's trust. Tilt back under 20 degrees
to re-arm.

Pins (M5StickC PLUS2): hold power 4, red LED 19 (active high), button A 37,
button B 39 (buttons active low), IMU MPU6886 on I2C SDA 21 / SCL 22 at 0x68.
Lines that aren't JSON frames are notes for a person; the gateway ignores them.
"""

import math
import os
import select
import sys
import time

import machine
from machine import I2C, Pin

from sctalk import Talk

HOLD = Pin(4, Pin.OUT, value=1)          # keep power on when running from the battery
LED = Pin(19, Pin.OUT, value=0)
BTN_A = Pin(37, Pin.IN)
BTN_B = Pin(39, Pin.IN)

TILT_REPORT_DEG = 40
TILT_REARM_DEG = 20
MPU = 0x68

i2c = I2C(0, scl=Pin(22), sda=Pin(21), freq=400000)


def imu_init():
    i2c.writeto_mem(MPU, 0x6B, b"\x00")  # wake
    time.sleep_ms(50)
    i2c.writeto_mem(MPU, 0x1C, b"\x00")  # accelerometer +-2 g


def tilt_degrees():
    raw = i2c.readfrom_mem(MPU, 0x3B, 6)
    ax, ay, az = [((raw[i] << 8 | raw[i + 1]) ^ 0x8000) - 0x8000 for i in (0, 2, 4)]
    norm = math.sqrt(ax * ax + ay * ay + az * az) or 1
    return math.degrees(math.acos(max(-1.0, min(1.0, az / norm))))


def note(text):
    print("# " + text)


def main():
    name = "m5-" + "".join("%02x" % b for b in machine.unique_id()[-3:])
    boot_tag = "".join("%02x" % b for b in os.urandom(2))
    talk = Talk(name, boot_tag, print)

    try:
        imu_init()
        imu_ok = True
    except OSError as e:
        imu_ok = False
        note("IMU not found: %s; buttons still report" % e)

    poll = select.poll()
    poll.register(sys.stdin, select.POLLIN)
    note("%s ready: tilt past %d degrees to report" % (name, TILT_REPORT_DEG))
    talk.report("status", "%s online" % name, 1.0, trigger="announce")

    armed = True
    last_a = last_b = 1
    blink_at = time.ticks_ms()
    while True:
        # link: tasks and acks from the gateway
        while poll.poll(0):
            line = sys.stdin.readline()
            what = talk.receive_line(line)
            if what is None and line.strip():
                # no braces in the note, or the gateway would try to parse it as a frame
                note("ignored %d bytes: %s" % (len(line), repr(line[:40]).replace("{", "(").replace("}", ")")))
            elif what == "task":
                note("task: %s  (A = yes, B = no)" % talk.task["s"])
            elif what == "refuse":
                note("refused by the master")

        # sensor
        if imu_ok:
            try:
                deg = tilt_degrees()
            except OSError:
                deg = 0
            # one question at a time: a new report would replace the task still waiting
            if armed and deg > TILT_REPORT_DEG and talk.task is None:
                talk.report("tilt_risk", "tilted %d degrees" % deg, 0.9)
                armed = False
            elif not armed and deg < TILT_REARM_DEG:
                armed = True

        # buttons: carry out the task
        a, b = BTN_A.value(), BTN_B.value()
        if talk.task is not None:
            if last_a == 1 and a == 0:
                talk.complete(True, "confirmed by button A")
                note("reported success")
            elif last_b == 1 and b == 0:
                talk.complete(False, "denied by button B")
                note("reported failure")
        last_a, last_b = a, b

        # LED blinks while a task waits
        if talk.task is not None:
            if time.ticks_diff(time.ticks_ms(), blink_at) > 250:
                LED.value(1 - LED.value())
                blink_at = time.ticks_ms()
        else:
            LED.value(0)

        # wait for input rather than sleeping: the stdin buffer holds only ~260 bytes,
        # and an ACK and a task arrive back to back
        poll.poll(50)


main()
