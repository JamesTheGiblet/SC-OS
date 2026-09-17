"""
VL53L0X time-of-flight distance sensor (I2C address 0x29), MicroPython.

A port of the initialisation and ranging sequence from Pololu's VL53L0X Arduino
library (MIT licence), which itself follows ST's API: reference SPAD setup, the
default tuning table, timing budget, and VHV and phase calibration. Ranging is
continuous and read without blocking: read() returns a distance in mm when a new
measurement is ready, None otherwise.

On the M5StickC PLUS2 the sensor goes on the Grove port: SDA 32, SCL 33.
"""

import time

ADDRESS = 0x29
MODEL_ID = 0xEE
OUT_OF_RANGE_MM = 8190          # the sensor reports 8190/8191 when nothing is in range

SYSRANGE_START = 0x00
SYSTEM_SEQUENCE_CONFIG = 0x01
SYSTEM_INTERRUPT_CONFIG_GPIO = 0x0A
SYSTEM_INTERRUPT_CLEAR = 0x0B
RESULT_INTERRUPT_STATUS = 0x13
RESULT_RANGE_STATUS = 0x14
FINAL_RANGE_CONFIG_MIN_COUNT_RATE_RTN_LIMIT = 0x44
MSRC_CONFIG_TIMEOUT_MACROP = 0x46
DYNAMIC_SPAD_NUM_REQUESTED_REF_SPAD = 0x4E
DYNAMIC_SPAD_REF_EN_START_OFFSET = 0x4F
PRE_RANGE_CONFIG_VCSEL_PERIOD = 0x50
PRE_RANGE_CONFIG_TIMEOUT_MACROP_HI = 0x51
MSRC_CONFIG_CONTROL = 0x60
FINAL_RANGE_CONFIG_VCSEL_PERIOD = 0x70
FINAL_RANGE_CONFIG_TIMEOUT_MACROP_HI = 0x71
GPIO_HV_MUX_ACTIVE_HIGH = 0x84
VHV_CONFIG_PAD_SCL_SDA__EXTSUP_HV = 0x89
GLOBAL_CONFIG_SPAD_ENABLES_REF_0 = 0xB0
GLOBAL_CONFIG_REF_EN_START_SELECT = 0xB6
IDENTIFICATION_MODEL_ID = 0xC0

TUNING = (
    (0xFF, 0x01), (0x00, 0x00), (0xFF, 0x00), (0x09, 0x00), (0x10, 0x00), (0x11, 0x00),
    (0x24, 0x01), (0x25, 0xFF), (0x75, 0x00), (0xFF, 0x01), (0x4E, 0x2C), (0x48, 0x00),
    (0x30, 0x20), (0xFF, 0x00), (0x30, 0x09), (0x54, 0x00), (0x31, 0x04), (0x32, 0x03),
    (0x40, 0x83), (0x46, 0x25), (0x60, 0x00), (0x27, 0x00), (0x50, 0x06), (0x51, 0x00),
    (0x52, 0x96), (0x56, 0x08), (0x57, 0x30), (0x61, 0x00), (0x62, 0x00), (0x64, 0x00),
    (0x65, 0x00), (0x66, 0xA0), (0xFF, 0x01), (0x22, 0x32), (0x47, 0x14), (0x49, 0xFF),
    (0x4A, 0x00), (0xFF, 0x00), (0x7A, 0x0A), (0x7B, 0x00), (0x78, 0x21), (0xFF, 0x01),
    (0x23, 0x34), (0x42, 0x00), (0x44, 0xFF), (0x45, 0x26), (0x46, 0x05), (0x40, 0x40),
    (0x0E, 0x06), (0x20, 0x1A), (0x43, 0x40), (0xFF, 0x00), (0x34, 0x03), (0x35, 0x44),
    (0xFF, 0x01), (0x31, 0x04), (0x4B, 0x09), (0x4C, 0x05), (0x4D, 0x04), (0xFF, 0x00),
    (0x44, 0x00), (0x45, 0x20), (0x47, 0x08), (0x48, 0x28), (0x67, 0x00), (0x70, 0x04),
    (0x71, 0x01), (0x72, 0xFE), (0x76, 0x00), (0x77, 0x00), (0xFF, 0x01), (0x0D, 0x01),
    (0xFF, 0x00), (0x80, 0x01), (0x01, 0xF8), (0xFF, 0x01), (0x8E, 0x01), (0x00, 0x01),
    (0xFF, 0x00), (0x80, 0x00),
)


class VL53L0XError(Exception):
    pass


class VL53L0X:
    def __init__(self, i2c, address=ADDRESS, timeout_ms=500):
        self.i2c = i2c
        self.address = address
        self.timeout_ms = timeout_ms
        self.stop_variable = 0
        self._init()

    # --- register access ---

    def _w(self, reg, value):
        self.i2c.writeto_mem(self.address, reg, bytes((value & 0xFF,)))

    def _w16(self, reg, value):
        self.i2c.writeto_mem(self.address, reg, bytes(((value >> 8) & 0xFF, value & 0xFF)))

    def _r(self, reg):
        return self.i2c.readfrom_mem(self.address, reg, 1)[0]

    def _r16(self, reg):
        b = self.i2c.readfrom_mem(self.address, reg, 2)
        return (b[0] << 8) | b[1]

    def _wait(self, done, what):
        start = time.ticks_ms()
        while not done():
            if time.ticks_diff(time.ticks_ms(), start) > self.timeout_ms:
                raise VL53L0XError("timeout waiting for " + what)
            time.sleep_ms(1)

    # --- initialisation ---

    def _init(self):
        if self._r(IDENTIFICATION_MODEL_ID) != MODEL_ID:
            raise VL53L0XError("not a VL53L0X (model id)")
        self._w(VHV_CONFIG_PAD_SCL_SDA__EXTSUP_HV, self._r(VHV_CONFIG_PAD_SCL_SDA__EXTSUP_HV) | 0x01)  # 2V8 I/O
        self._w(0x88, 0x00)                                   # I2C standard mode
        self._w(0x80, 0x01)
        self._w(0xFF, 0x01)
        self._w(0x00, 0x00)
        self.stop_variable = self._r(0x91)
        self._w(0x00, 0x01)
        self._w(0xFF, 0x00)
        self._w(0x80, 0x00)
        # disable the MSRC and pre-range signal rate limit checks
        self._w(MSRC_CONFIG_CONTROL, self._r(MSRC_CONFIG_CONTROL) | 0x12)
        self._w16(FINAL_RANGE_CONFIG_MIN_COUNT_RATE_RTN_LIMIT, int(0.25 * (1 << 7)))
        self._w(SYSTEM_SEQUENCE_CONFIG, 0xFF)

        spad_count, is_aperture = self._spad_info()
        spad_map = bytearray(self.i2c.readfrom_mem(self.address, GLOBAL_CONFIG_SPAD_ENABLES_REF_0, 6))
        self._w(0xFF, 0x01)
        self._w(DYNAMIC_SPAD_REF_EN_START_OFFSET, 0x00)
        self._w(DYNAMIC_SPAD_NUM_REQUESTED_REF_SPAD, 0x2C)
        self._w(0xFF, 0x00)
        self._w(GLOBAL_CONFIG_REF_EN_START_SELECT, 0xB4)
        first = 12 if is_aperture else 0
        enabled = 0
        for i in range(48):
            if i < first or enabled == spad_count:
                spad_map[i // 8] &= ~(1 << (i % 8)) & 0xFF
            elif (spad_map[i // 8] >> (i % 8)) & 0x01:
                enabled += 1
        self.i2c.writeto_mem(self.address, GLOBAL_CONFIG_SPAD_ENABLES_REF_0, spad_map)

        for reg, value in TUNING:
            self._w(reg, value)

        self._w(SYSTEM_INTERRUPT_CONFIG_GPIO, 0x04)           # interrupt on new sample ready
        self._w(GPIO_HV_MUX_ACTIVE_HIGH, self._r(GPIO_HV_MUX_ACTIVE_HIGH) & ~0x10 & 0xFF)
        self._w(SYSTEM_INTERRUPT_CLEAR, 0x01)

        budget = self._timing_budget()
        self._w(SYSTEM_SEQUENCE_CONFIG, 0xE8)
        self._set_timing_budget(budget)

        self._w(SYSTEM_SEQUENCE_CONFIG, 0x01)
        self._single_ref_calibration(0x40)                    # VHV
        self._w(SYSTEM_SEQUENCE_CONFIG, 0x02)
        self._single_ref_calibration(0x00)                    # phase
        self._w(SYSTEM_SEQUENCE_CONFIG, 0xE8)

    def _spad_info(self):
        self._w(0x80, 0x01)
        self._w(0xFF, 0x01)
        self._w(0x00, 0x00)
        self._w(0xFF, 0x06)
        self._w(0x83, self._r(0x83) | 0x04)
        self._w(0xFF, 0x07)
        self._w(0x81, 0x01)
        self._w(0x80, 0x01)
        self._w(0x94, 0x6B)
        self._w(0x83, 0x00)
        self._wait(lambda: self._r(0x83) != 0x00, "SPAD info")
        self._w(0x83, 0x01)
        tmp = self._r(0x92)
        self._w(0x81, 0x00)
        self._w(0xFF, 0x06)
        self._w(0x83, self._r(0x83) & ~0x04 & 0xFF)
        self._w(0xFF, 0x01)
        self._w(0x00, 0x01)
        self._w(0xFF, 0x00)
        self._w(0x80, 0x00)
        return tmp & 0x7F, (tmp >> 7) & 0x01

    def _single_ref_calibration(self, vhv_init_byte):
        self._w(SYSRANGE_START, 0x01 | vhv_init_byte)
        self._wait(lambda: self._r(RESULT_INTERRUPT_STATUS) & 0x07, "reference calibration")
        self._w(SYSTEM_INTERRUPT_CLEAR, 0x01)
        self._w(SYSRANGE_START, 0x00)

    # --- timing budget ---

    @staticmethod
    def _decode_timeout(value):
        return ((value & 0x00FF) << ((value & 0xFF00) >> 8)) + 1

    @staticmethod
    def _encode_timeout(mclks):
        if mclks <= 0:
            return 0
        mclks -= 1
        ms_byte = 0
        while mclks > 0xFF:
            mclks >>= 1
            ms_byte += 1
        return (ms_byte << 8) | (mclks & 0xFF)

    @staticmethod
    def _mclks_to_us(mclks, vcsel_period_pclks):
        macro_period_ns = ((2304 * vcsel_period_pclks * 1655) + 500) // 1000
        return ((mclks * macro_period_ns) + 500) // 1000

    @staticmethod
    def _us_to_mclks(us, vcsel_period_pclks):
        macro_period_ns = ((2304 * vcsel_period_pclks * 1655) + 500) // 1000
        return ((us * 1000) + (macro_period_ns // 2)) // macro_period_ns

    def _steps(self):
        config = self._r(SYSTEM_SEQUENCE_CONFIG)
        enables = {"tcc": (config >> 4) & 1, "dss": (config >> 3) & 1, "msrc": (config >> 2) & 1,
                   "pre_range": (config >> 6) & 1, "final_range": (config >> 7) & 1}
        pre_vcsel = (self._r(PRE_RANGE_CONFIG_VCSEL_PERIOD) + 1) * 2
        final_vcsel = (self._r(FINAL_RANGE_CONFIG_VCSEL_PERIOD) + 1) * 2
        msrc_dss_tcc_us = self._mclks_to_us(self._r(MSRC_CONFIG_TIMEOUT_MACROP) + 1, pre_vcsel)
        pre_range_mclks = self._decode_timeout(self._r16(PRE_RANGE_CONFIG_TIMEOUT_MACROP_HI))
        pre_range_us = self._mclks_to_us(pre_range_mclks, pre_vcsel)
        final_range_mclks = self._decode_timeout(self._r16(FINAL_RANGE_CONFIG_TIMEOUT_MACROP_HI))
        if enables["pre_range"]:
            final_range_mclks -= pre_range_mclks
        final_range_us = self._mclks_to_us(final_range_mclks, final_vcsel)
        timeouts = {"msrc_dss_tcc_us": msrc_dss_tcc_us, "pre_range_us": pre_range_us,
                    "pre_range_mclks": pre_range_mclks, "final_range_us": final_range_us,
                    "final_vcsel": final_vcsel}
        return enables, timeouts

    def _used_budget(self, enables, t):
        used = 1910 + 960                                     # start and end overhead
        if enables["tcc"]:
            used += t["msrc_dss_tcc_us"] + 590
        if enables["dss"]:
            used += 2 * (t["msrc_dss_tcc_us"] + 690)
        elif enables["msrc"]:
            used += t["msrc_dss_tcc_us"] + 660
        if enables["pre_range"]:
            used += t["pre_range_us"] + 660
        return used

    def _timing_budget(self):
        enables, t = self._steps()
        budget = self._used_budget(enables, t)
        if enables["final_range"]:
            budget += t["final_range_us"] + 550
        return budget

    def _set_timing_budget(self, budget_us):
        if budget_us < 20000:
            raise VL53L0XError("timing budget under 20 ms")
        enables, t = self._steps()
        used = self._used_budget(enables, t)
        if enables["final_range"]:
            used += 550
            if used > budget_us:
                raise VL53L0XError("timing budget too small")
            final_mclks = self._us_to_mclks(budget_us - used, t["final_vcsel"])
            if enables["pre_range"]:
                final_mclks += t["pre_range_mclks"]
            self._w16(FINAL_RANGE_CONFIG_TIMEOUT_MACROP_HI, self._encode_timeout(final_mclks))

    # --- ranging ---

    def start(self):
        """Start continuous ranging (as fast as the timing budget allows)."""
        self._w(0x80, 0x01)
        self._w(0xFF, 0x01)
        self._w(0x00, 0x00)
        self._w(0x91, self.stop_variable)
        self._w(0x00, 0x01)
        self._w(0xFF, 0x00)
        self._w(0x80, 0x00)
        self._w(SYSRANGE_START, 0x02)

    def read(self):
        """A new distance in mm if one is ready, else None. OUT_OF_RANGE_MM or more: no target."""
        if not self._r(RESULT_INTERRUPT_STATUS) & 0x07:
            return None
        mm = self._r16(RESULT_RANGE_STATUS + 10)
        self._w(SYSTEM_INTERRUPT_CLEAR, 0x01)
        return mm
