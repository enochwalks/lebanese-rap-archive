"""Generate the desktop shortcut icons.

Hand-rolled because the project has no image dependency and should not grow one
for two 32x32 glyphs. Writes classic 32bpp BMP/DIB icons (supported by every
Windows shell) at 16 and 32 pixels.

Run from the signals/ folder:  python tools/make_icons.py
"""

import struct
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "icons"

BG = (0x23, 0x23, 0x22)        # panel grey, matches the dashboard
BLUE = (0x39, 0x87, 0xE5)      # series-1 dark
GREEN = (0x0C, 0xA3, 0x0C)     # status good
GREY = (0x54, 0x54, 0x50)


def blank(size):
    return [[(0, 0, 0, 0) for _ in range(size)] for _ in range(size)]


def rounded_bg(px, size, colour, radius_ratio=0.22):
    r = max(1, int(size * radius_ratio))
    for y in range(size):
        for x in range(size):
            # Distance test only inside the four corner boxes.
            cx = r - 1 if x < r else (size - r if x >= size - r else x)
            cy = r - 1 if y < r else (size - r if y >= size - r else y)
            if (x - cx) ** 2 + (y - cy) ** 2 <= r * r or (r <= x < size - r) or (r <= y < size - r):
                px[y][x] = colour + (255,)


def bars(px, size):
    """Three ascending bars: the dashboard."""
    pad = max(2, size // 6)
    inner = size - 2 * pad
    gap = max(1, inner // 10)
    width = (inner - 2 * gap) // 3
    heights = [inner // 3, int(inner * 0.66), inner]
    for i, height in enumerate(heights):
        x0 = pad + i * (width + gap)
        colour = BLUE if i == 2 else GREY
        for y in range(size - pad - height, size - pad):
            for x in range(x0, min(x0 + width, size - pad)):
                px[y][x] = colour + (255,)


def pulse(px, size):
    """A filled dot inside a ring: the agent, listening."""
    cx = cy = (size - 1) / 2.0
    outer = size * 0.36
    ring = size * 0.26
    dot = size * 0.15
    for y in range(size):
        for x in range(size):
            d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            if d <= dot:
                px[y][x] = GREEN + (255,)
            elif ring <= d <= outer:
                px[y][x] = GREEN + (170,)


def dib(px, size):
    """One BITMAPINFOHEADER image: doubled height, bottom-up BGRA, then AND mask."""
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, size * size * 4, 0, 0, 0, 0)
    body = bytearray()
    for y in range(size - 1, -1, -1):
        for x in range(size):
            r, g, b, a = px[y][x]
            body += bytes((b, g, r, a))
    mask = bytes((size * size) // 8)      # zeroed: alpha already carries transparency
    return header + bytes(body) + mask


def write_ico(path, draw):
    images = []
    for size in (16, 32, 48):
        px = blank(size)
        rounded_bg(px, size, BG)
        draw(px, size)
        images.append((size, dib(px, size)))

    offset = 6 + 16 * len(images)
    out = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    for size, data in images:
        out += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for _size, data in images:
        out += data
    path.write_bytes(bytes(out))
    return path


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for name, draw in (("dashboard", bars), ("agent", pulse)):
        p = write_ico(OUT / f"{name}.ico", draw)
        print(f"{p.name}: {p.stat().st_size} bytes")
