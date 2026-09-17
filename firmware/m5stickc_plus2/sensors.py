"""
The M5StickC PLUS2's sensors: IMU (MPU6886: accelerometer, gyroscope, die
temperature), real-time clock (BM8563), battery voltage, the ESP32's own
temperature sensor, and the three buttons. Also the buzzer, the one sound output.

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
        """(year, month, day, hour, minute, second), or None if the RTC lost power."""
        r = self.i2c.readfrom_mem(RTC, 0x02, 7)
        if r[0] & 0x80:                                  # voltage-low flag: time not reliable
            return None
        return (2000 + _bcd(r[6]), _bcd(r[5] & 0x1F), _bcd(r[3] & 0x3F),
                _bcd(r[2] & 0x3F), _bcd(r[1] & 0x7F), _bcd(r[0] & 0x7F))

    def set_clock(self, year, month, day, weekday, hour, minute, second):
        self.i2c.writeto_mem(RTC, 0x02, bytes([
            _to_bcd(second), _to_bcd(minute), _to_bcd(hour), _to_bcd(day),
            weekday, _to_bcd(month), _to_bcd(year % 100)]))


class Buzzer:
    """Tones on pin 2 that play without blocking: call tick() from the main loop."""

    TASK = ((2000, 70), (0, 60), (2000, 70))
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
