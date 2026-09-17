"""
PCA9685 16-channel, 12-bit PWM driver (I2C address 0x40), MicroPython, with a
hobby-servo helper.

All channels share one PWM frequency; servos want 50 Hz. A channel's output is
set by the tick (0..4095) where it turns on and the tick where it turns off
within each period; here pulses always start at tick 0.

On the M5StickC PLUS2 it shares the Grove I2C bus (SDA 32, SCL 33) with the
VL53L0X. Servo power comes from V+ on the board's terminal, never from the stick.
"""

import time

ADDRESS = 0x40
MODE1 = 0x00
MODE2 = 0x01
PRESCALE = 0xFE
LED0_ON_L = 0x06
ALL_LED_ON_L = 0xFA

RESTART = 0x80
SLEEP = 0x10
ALLCALL = 0x01
AUTO_INCREMENT = 0x20
OUTDRV = 0x04
FULL = 0x1000                     # the "fully on/off" bit in a channel's ON/OFF high byte
OSC_HZ = 25000000


class PCA9685:
    def __init__(self, i2c, address=ADDRESS, freq_hz=50):
        self.i2c = i2c
        self.address = address
        self._w(MODE2, OUTDRV)                    # totem-pole outputs
        self._w(MODE1, ALLCALL)
        time.sleep_ms(5)
        self.all_off()
        self.freq(freq_hz)

    def _w(self, reg, value):
        self.i2c.writeto_mem(self.address, reg, bytes((value & 0xFF,)))

    def _r(self, reg):
        return self.i2c.readfrom_mem(self.address, reg, 1)[0]

    def freq(self, hz=None):
        """Set (or read) the PWM frequency for all channels."""
        if hz is None:
            return OSC_HZ / (4096 * (self._r(PRESCALE) + 1))
        prescale = int(OSC_HZ / (4096.0 * hz) + 0.5) - 1
        prescale = max(3, min(255, prescale))
        old = self._r(MODE1)
        self._w(MODE1, (old & ~RESTART & 0xFF) | SLEEP)          # prescale only changes while asleep
        self._w(PRESCALE, prescale)
        self._w(MODE1, old & ~SLEEP & 0xFF)
        time.sleep_ms(5)                                           # oscillator restart
        self._w(MODE1, (old | RESTART | AUTO_INCREMENT) & ~SLEEP & 0xFF)
        self.hz = OSC_HZ / (4096 * (prescale + 1))

    def pwm(self, channel, on, off):
        """Raw on/off ticks (0..4095, or FULL for fully on/off) for one channel."""
        if not 0 <= channel <= 15:
            raise ValueError("channel must be 0..15")
        self.i2c.writeto_mem(self.address, LED0_ON_L + 4 * channel,
                             bytes((on & 0xFF, (on >> 8) & 0xFF, off & 0xFF, (off >> 8) & 0xFF)))

    def duty(self, channel, value):
        """Duty cycle 0..4095; 0 is fully off (no pulse), 4095 fully on."""
        if value <= 0:
            self.pwm(channel, 0, FULL)
        elif value >= 4095:
            self.pwm(channel, FULL, 0)
        else:
            self.pwm(channel, 0, value)

    def pulse_us(self, channel, us):
        """A pulse of this many microseconds each period (servos: about 500..2500)."""
        ticks = int(us * 4096 * self.hz / 1000000 + 0.5)
        self.duty(channel, max(1, min(4094, ticks)))

    def off(self, channel):
        """No pulses: a servo on this channel goes limp."""
        self.pwm(channel, 0, FULL)

    def all_off(self):
        self.i2c.writeto_mem(self.address, ALL_LED_ON_L, bytes((0, 0, 0, FULL >> 8)))


class Servo:
    """
    One hobby servo on a PCA9685 channel. Angles are in degrees, 0..180 by default,
    mapped linearly onto min_us..max_us (MG90S: roughly 500..2500). trim shifts the
    centre; invert mirrors the direction; lo/hi clamp every command.
    """

    def __init__(self, pca, channel, min_us=500, max_us=2500, trim=0.0, invert=False, lo=0.0, hi=180.0):
        self.pca = pca
        self.channel = channel
        self.min_us, self.max_us = min_us, max_us
        self.trim, self.invert = trim, invert
        self.lo, self.hi = lo, hi
        self.angle = None

    def move(self, angle):
        angle = max(self.lo, min(self.hi, angle))
        a = angle + self.trim
        if self.invert:
            a = 180.0 - a
        a = max(0.0, min(180.0, a))
        self.pca.pulse_us(self.channel, self.min_us + (self.max_us - self.min_us) * a / 180.0)
        self.angle = angle
        return angle

    def relax(self):
        self.pca.off(self.channel)
        self.angle = None
