"""
The M5StickC PLUS2's sensors: IMU (MPU6886: accelerometer, gyroscope, die
temperature), real-time clock (BM8563), battery voltage, the ESP32's own
temperature sensor, the three buttons, a VL53L0X time-of-flight distance
sensor on the Grove port when one is connected, and digital IR edge sensors. Also the buzzer, the one sound output.

Not read: the SPM1423 PDM microphone (clock 0, data 34). It answers when clocked,
but MicroPython's I2S has no PDM input on the ESP32, and counting its data edges
with the pulse counter didn't track loudness.

Pins: I2C SDA 21 / SCL 22 (IMU 0x68, RTC 0x51), battery ADC 38 (halved by a
divider), buttons A 37, B 39, C (power) 35, all active low.
"""

import math
import time

import esp32
from machine import ADC, I2C, PWM, Pin

MPU = 0x68
RTC = 0x51

# What this stick can sense, for the __sensors__ capsule the gateway builds. min/max are the
# physical range; margins are operating bounds (supplied here until a __setup__ capsule does it).
# tilt's margin_high is the report threshold.
DESCRIPTION = (
    {"id": "accel", "type": "accelerometer", "bus": "i2c0:0x68", "pin": "21,22", "unit": "g",
     "min": -2, "max": 2, "margin_low": -1.5, "margin_high": 1.5, "sample_ms": 200},
    {"id": "gyro", "type": "gyroscope", "bus": "i2c0:0x68", "pin": "21,22", "unit": "dps",
     "min": -2000, "max": 2000, "margin_low": -500, "margin_high": 500, "sample_ms": 200},
    {"id": "tilt", "type": "inclination", "bus": "i2c0:0x68", "pin": "21,22", "unit": "deg",
     "min": 0, "max": 180, "margin_low": 0, "margin_high": 40, "sample_ms": 200},
    {"id": "imu_temp", "type": "temperature", "bus": "i2c0:0x68", "pin": "21,22", "unit": "C",
     "min": -40, "max": 85, "margin_low": 0, "margin_high": 65, "sample_ms": 200},
    {"id": "chip_temp", "type": "temperature", "bus": "internal", "pin": "none", "unit": "C",
     "min": -40, "max": 125, "margin_low": 0, "margin_high": 85, "sample_ms": 200},
    {"id": "battery", "type": "voltage", "bus": "adc", "pin": "38", "unit": "V",
     "min": 0, "max": 5, "margin_low": 3.3, "margin_high": 4.35, "sample_ms": 200},
    {"id": "clock", "type": "rtc", "bus": "i2c0:0x51", "pin": "21,22", "unit": "s",
     "min": 0, "max": 86400, "margin_low": 0, "margin_high": 86400, "sample_ms": 1000},
    {"id": "buttons", "type": "buttons", "bus": "gpio", "pin": "37,39,35", "unit": "pressed",
     "min": 0, "max": 1, "margin_low": 0, "margin_high": 1, "sample_ms": 20},
)
# Added to the description only when the sensor answers at boot.
TOF_DESCRIPTION = {"id": "tof", "type": "distance", "bus": "i2c1:0x29", "pin": "32,33", "unit": "mm",
                   "min": 0, "max": 2000, "margin_low": 50, "margin_high": 1200, "sample_ms": 200}

# Digital IR reflectance modules looking down at the front: 1 = edge (nothing reflects).
# Their outputs pull high only weakly, so the pin's pull-up does it; an unplugged sensor
# then reads as an edge, which fails safe. Digital pins can't be detected, so list only
# the sensors that are wired.
EDGE_DESCRIPTION = (
    {"id": "edge_left", "type": "edge", "bus": "gpio", "pin": "26", "unit": "edge",
     "min": 0, "max": 1, "margin_low": 0, "margin_high": 0, "sample_ms": 20},
    # Right sensor on pin 36: that pin has no internal pull-up, so add a 10 kohm resistor from
    # G36 to 3V3 before uncommenting.
    # {"id": "edge_right", "type": "edge", "bus": "gpio", "pin": "36", "unit": "edge",
    #  "min": 0, "max": 1, "margin_low": 0, "margin_high": 0, "sample_ms": 20},
)

ABSENT = (
    "mic: SPM1423 PDM on pins 0,34; MicroPython has no PDM input",
    "chip_temp: large fixed offset; trend only",
)
ACCEL_LSB_PER_G = 16384.0       # +-2 g
GYRO_LSB_PER_DPS = 16.4         # +-2000 dps


def _s16(h, l):
    return ((h << 8 | l) ^ 0x8000) - 0x8000


def _bcd(b):
    return (b >> 4) * 10 + (b & 0x0F)


def _to_bcd(n):
    return (n // 10) << 4 | n % 10


class Sensors:
    def __init__(self):
        self.i2c = I2C(0, scl=Pin(22), sda=Pin(21), freq=400000)
        self.bat = ADC(Pin(38))
        self.bat.atten(ADC.ATTN_11DB)
        self.buttons = {"A": Pin(37, Pin.IN), "B": Pin(39, Pin.IN), "C": Pin(35, Pin.IN)}
        self.imu_ok = self._imu_init()
        self.rtc_ok = RTC in self.i2c.scan()
        self.tof = self._tof_init()
        self.tof_mm = None                               # last distance; None: no target or no sensor

    def _tof_init(self):
        """VL53L0X on the Grove port (SDA 32, SCL 33), if one is connected."""
        try:
            from vl53l0x import VL53L0X
            grove = I2C(1, scl=Pin(33), sda=Pin(32), freq=400000)
            if 0x29 not in grove.scan():
                return None
            tof = VL53L0X(grove)
            tof.start()
            return tof
        except Exception:
            return None

    def distance_mm(self):
        """Latest distance in mm, None when nothing is in range. Doesn't wait for the sensor."""
        if self.tof is None:
            return None
        try:
            mm = self.tof.read()
        except OSError:
            return self.tof_mm
        if mm is not None:
            self.tof_mm = mm if mm < 8190 else None
        return self.tof_mm

    def _imu_init(self):
        try:
            self.i2c.writeto_mem(MPU, 0x6B, b"\x00")    # wake
            time.sleep_ms(50)
            self.i2c.writeto_mem(MPU, 0x6C, b"\x00")    # accelerometer and gyroscope on
            self.i2c.writeto_mem(MPU, 0x1C, b"\x00")    # accelerometer +-2 g
            self.i2c.writeto_mem(MPU, 0x1B, b"\x18")    # gyroscope +-2000 dps
            self.i2c.writeto_mem(MPU, 0x1A, b"\x01")    # low-pass filter
            return True
        except OSError:
            return False

    def imu(self):
        """(accel g xyz, gyro dps xyz, die temperature C, tilt degrees from flat)."""
        r = self.i2c.readfrom_mem(MPU, 0x3B, 14)
        ax, ay, az = [_s16(r[i], r[i + 1]) / ACCEL_LSB_PER_G for i in (0, 2, 4)]
        temp = _s16(r[6], r[7]) / 326.8 + 25.0
        gx, gy, gz = [_s16(r[i], r[i + 1]) / GYRO_LSB_PER_DPS for i in (8, 10, 12)]
        norm = math.sqrt(ax * ax + ay * ay + az * az) or 1
        tilt = math.degrees(math.acos(max(-1.0, min(1.0, az / norm))))
        return (ax, ay, az), (gx, gy, gz), temp, tilt

    def battery_volts(self):
        return self.bat.read_uv() * 2 / 1e6

    def chip_celsius(self):
        """The ESP32's internal sensor. It has a large fixed offset: useful for change, not absolute."""
        return (esp32.raw_temperature() - 32) / 1.8

    def pressed(self):
        """Names of buttons held down now."""
        return [n for n, p in self.buttons.items() if p.value() == 0]

    def clock(self):
        """(year, month, day, hour, minute, second) in UTC, or None if the RTC lost power."""
        r = self.i2c.readfrom_mem(RTC, 0x02, 7)
        if r[0] & 0x80:                                  # voltage-low flag: time not reliable
            return None
        return (2000 + _bcd(r[6]), _bcd(r[5] & 0x1F), _bcd(r[3] & 0x3F),
                _bcd(r[2] & 0x3F), _bcd(r[1] & 0x7F), _bcd(r[0] & 0x7F))

    def set_clock(self, year, month, day, weekday, hour, minute, second):
        self.i2c.writeto_mem(RTC, 0x02, bytes([
            _to_bcd(second), _to_bcd(minute), _to_bcd(hour), _to_bcd(day),
            weekday, _to_bcd(month), _to_bcd(year % 100)]))


class ReadingSender:
    """
    Decides when readings are worth sending: when one moves past its deadband, or
    every HEARTBEAT_MS regardless, but never more often than MIN_INTERVAL_MS.
    """

    MIN_INTERVAL_MS = 5000
    HEARTBEAT_MS = 25000                  # under the master's 30 s idle timeout, so the session stays open
    DEADBAND = {"accel": 0.05, "gyro": 20, "tilt": 5, "imu_temp": 1.0, "chip_temp": 1.0, "battery": 0.05,
                "tof": 20, "edge_left": 1, "edge_right": 1}

    def __init__(self):
        self.sent = None
        self.sent_at = None

    def _moved(self, values):
        if self.sent is None:
            return True
        for sid, band in self.DEADBAND.items():
            new, old = values.get(sid), self.sent.get(sid)
            if new is None or old is None:
                continue
            pairs = zip(new, old) if isinstance(new, list) else ((new, old),)
            if any(abs(a - b) >= band for a, b in pairs):
                return True
        return False

    def due(self, values, now_ms):
        if self.sent_at is None:
            return True
        age = time.ticks_diff(now_ms, self.sent_at)
        if age < self.MIN_INTERVAL_MS:
            return False
        return age >= self.HEARTBEAT_MS or self._moved(values)

    def mark(self, values, now_ms):
        self.sent, self.sent_at = values, now_ms


def iso_utc(clock):
    return "%04d-%02d-%02dT%02d:%02d:%02dZ" % clock


class EdgeSensors:
    """
    Front edge detection from the digital IR modules in EDGE_DESCRIPTION.
    A module's OUT is EDGE_LEVEL when nothing reflects under it. An edge counts only
    after CONFIRM consecutive samples; it clears on the first sample with surface.
    Measured on the left module: 0.34 V over a table, and only 1.56 V in the air
    against a pull-down, but a clean 3.14 V with the pin's pull-up.
    """

    EDGE_LEVEL = 1
    CONFIRM = 3

    def __init__(self):
        self.pins = {}
        for d in EDGE_DESCRIPTION:
            n = int(d["pin"])
            try:
                self.pins[d["id"]] = Pin(n, Pin.IN, Pin.PULL_UP)
            except (ValueError, OSError):
                self.pins[d["id"]] = Pin(n, Pin.IN)               # pins 34-39: needs an external pull-up
        self.count = {sid: 0 for sid in self.pins}
        self.edge = {sid: False for sid in self.pins}

    def update(self):
        """Sample both sensors; returns {id: True if an edge is confirmed}."""
        for sid, pin in self.pins.items():
            if pin.value() == self.EDGE_LEVEL:
                self.count[sid] += 1
            else:
                self.count[sid] = 0
            self.edge[sid] = self.count[sid] >= self.CONFIRM
        return self.edge


class Buzzer:
    """Tones on pin 2 that play without blocking: call tick() from the main loop."""

    TASK = ((2000, 70), (0, 60), (2000, 70))
    EDGE = ((3200, 80), (0, 40), (3200, 80), (0, 40), (3200, 80))
    SUCCESS = ((1500, 70), (2500, 110))
    FAILURE = ((700, 250),)

    def __init__(self, duty=12000):
        self.pwm = PWM(Pin(2), freq=2000, duty_u16=0)
        self.duty = duty
        self.queue = []
        self.until = 0
        self.muted = False

    def play(self, tones):
        if not self.muted:
            self.queue = list(tones)
            self.until = 0

    def tick(self):
        if time.ticks_diff(time.ticks_ms(), self.until) < 0:
            return
        if not self.queue:
            self.pwm.duty_u16(0)
            return
        freq, ms = self.queue.pop(0)
        if freq:
            self.pwm.freq(freq)
            self.pwm.duty_u16(self.duty)
        else:
            self.pwm.duty_u16(0)
        self.until = time.ticks_add(time.ticks_ms(), ms)
