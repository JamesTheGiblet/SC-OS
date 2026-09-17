"""
Minimal ST7789 driver for the M5StickC PLUS2's 1.14" 135x240 screen, landscape.

Draws into a framebuf and pushes only the rows that changed, so a redraw takes
a few milliseconds and the device keeps reading its small serial input buffer.
Colours are byte-swapped RGB565: framebuf stores little-endian, the panel reads
big-endian.

Pins: MOSI 15, SCK 13, CS 5, DC 14, RST 12, backlight 27.
"""

import framebuf
import time
from machine import SPI, Pin

WIDTH = 240
HEIGHT = 135
X_OFFSET = 40        # the 135x240 panel sits inside the controller's 240x320 memory
Y_OFFSET = 53
MADCTL_LANDSCAPE = 0x60


def rgb(r, g, b):
    c = (r & 0xF8) << 8 | (g & 0xFC) << 3 | b >> 3
    return (c & 0xFF) << 8 | c >> 8


BLACK = rgb(0, 0, 0)
WHITE = rgb(255, 255, 255)
GREY = rgb(130, 130, 130)
RED = rgb(255, 60, 60)
GREEN = rgb(60, 220, 90)
YELLOW = rgb(255, 210, 0)
BLUE = rgb(40, 90, 200)
CYAN = rgb(80, 200, 230)


class Display:
    def __init__(self):
        self.spi = SPI(1, baudrate=20000000, polarity=0, phase=0, sck=Pin(13), mosi=Pin(15))
        self.cs = Pin(5, Pin.OUT, value=1)
        self.dc = Pin(14, Pin.OUT, value=0)
        self.rst = Pin(12, Pin.OUT, value=1)
        self.bl = Pin(27, Pin.OUT, value=0)
        self.buf = bytearray(WIDTH * HEIGHT * 2)
        self.fb = framebuf.FrameBuffer(self.buf, WIDTH, HEIGHT, framebuf.RGB565)
        self._init()

    def _cmd(self, c, data=None):
        self.cs.value(0)
        self.dc.value(0)
        self.spi.write(bytes([c]))
        if data:
            self.dc.value(1)
            self.spi.write(data)
        self.cs.value(1)

    def _init(self):
        self.rst.value(0)
        time.sleep_ms(20)
        self.rst.value(1)
        time.sleep_ms(120)
        self._cmd(0x01)                       # software reset
        time.sleep_ms(150)
        self._cmd(0x11)                       # sleep out
        time.sleep_ms(120)
        self._cmd(0x3A, b"\x55")              # 16-bit colour
        self._cmd(0x36, bytes([MADCTL_LANDSCAPE]))
        self._cmd(0x21)                       # inversion on: this IPS panel needs it
        self._cmd(0x13)                       # normal display
        self._cmd(0x29)                       # display on
        self.fb.fill(BLACK)
        self.show()
        self.backlight(True)

    def backlight(self, on):
        self.bl.value(1 if on else 0)

    def _window(self, x0, y0, x1, y1):
        self._cmd(0x2A, bytes([(x0 + X_OFFSET) >> 8, (x0 + X_OFFSET) & 0xFF,
                               (x1 + X_OFFSET) >> 8, (x1 + X_OFFSET) & 0xFF]))
        self._cmd(0x2B, bytes([(y0 + Y_OFFSET) >> 8, (y0 + Y_OFFSET) & 0xFF,
                               (y1 + Y_OFFSET) >> 8, (y1 + Y_OFFSET) & 0xFF]))

    def show(self, y0=0, y1=HEIGHT - 1):
        """Push rows y0..y1 of the framebuffer to the panel."""
        y0 = max(0, y0)
        y1 = min(HEIGHT - 1, y1)
        self._window(0, y0, WIDTH - 1, y1)
        self.cs.value(0)
        self.dc.value(0)
        self.spi.write(b"\x2C")               # memory write
        self.dc.value(1)
        self.spi.write(memoryview(self.buf)[y0 * WIDTH * 2:(y1 + 1) * WIDTH * 2])
        self.cs.value(1)

    _glyphs = {}

    def _glyph(self, ch):
        """Horizontal runs (x, y, length) of the 8x8 font glyph, computed once per character."""
        runs = self._glyphs.get(ch)
        if runs is None:
            g = framebuf.FrameBuffer(bytearray(8 * 8 * 2), 8, 8, framebuf.RGB565)
            g.text(ch, 0, 0, 1)
            runs = []
            for py in range(8):
                px = 0
                while px < 8:
                    if g.pixel(px, py):
                        start = px
                        while px < 8 and g.pixel(px, py):
                            px += 1
                        runs.append((start, py, px - start))
                    else:
                        px += 1
            self._glyphs[ch] = runs
        return runs

    def big_text(self, text, x, y, color, scale=2):
        """8x8 font scaled up, one rectangle per horizontal run of pixels."""
        fill = self.fb.fill_rect
        for i, ch in enumerate(text):
            gx = x + i * 8 * scale
            for px, py, n in self._glyph(ch):
                fill(gx + px * scale, y + py * scale, n * scale, scale, color)


class Lines:
    """Text rows that redraw, and push to the panel, only when their content changes."""

    def __init__(self, display):
        self.d = display
        self.cache = {}

    def put(self, key, y, height, text, fg, bg=BLACK, big=False):
        state = (text, fg, bg)
        if self.cache.get(key) == state:
            return False
        self.cache[key] = state
        self.d.fb.fill_rect(0, y, WIDTH, height, bg)
        if big:
            self.d.big_text(text, 4, y + (height - 16) // 2, fg)
        else:
            self.d.fb.text(text[:WIDTH // 8], 2, y + (height - 8) // 2, fg)
        self.d.show(y, y + height - 1)
        return True
