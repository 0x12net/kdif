"""Regenerates the project icons - PCM/icon.png (PCM package listing) and
plugin/icons/kdif_{24,48}.png (the KiCad toolbar button, see plugin/plugin.json).

Written by hand with zlib/struct rather than drawn in an image editor or
generated with Pillow: the icons are the *only* binary assets in the
repository, and this keeps them reproducible from source on any machine that
can already run kdif itself (stdlib only, no extra dependency, same output on
Linux/macOS/Windows). Run it after changing anything below:

    python3 PCM/make_icons.py

The drawing is the viewer's own diff legend, at icon size: a red square (only
in revision A), a green one (only in B), overlapping additively into the same
yellow the viewer paints unchanged geometry with. Rendered 4x oversampled and
box-filtered down, which is what gives the edges their antialiasing.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Same red/green the viewer uses for "only in A" / "only in B"; their sum
# saturates to the "present in both" yellow, so the overlap needs no colour
# of its own - it falls out of the additive blend below.
COLOR_A = (208, 64, 64)
COLOR_B = (64, 192, 96)

SS = 4  # supersampling factor


def _rounded_rect(px, size, x0, y0, x1, y1, radius, color):
    """Additively blend a rounded rect onto the RGBA float buffer `px`."""
    r, g, b = color
    for y in range(max(0, int(y0)), min(size, int(y1) + 1)):
        for x in range(max(0, int(x0)), min(size, int(x1) + 1)):
            # distance test only matters near the four corner circles
            cx = x0 + radius if x < x0 + radius else (x1 - radius if x > x1 - radius else x)
            cy = y0 + radius if y < y0 + radius else (y1 - radius if y > y1 - radius else y)
            if (x - cx) ** 2 + (y - cy) ** 2 > radius * radius:
                continue
            i = (y * size + x) * 4
            px[i] = min(255, px[i] + r)
            px[i + 1] = min(255, px[i + 1] + g)
            px[i + 2] = min(255, px[i + 2] + b)
            px[i + 3] = 255


def _render(size: int) -> bytes:
    """Draw at size*SS, box-filter down to size, return RGBA rows."""
    big = size * SS
    px = [0] * (big * big * 4)

    # Two squares offset along the diagonal, each ~62% of the canvas, with a
    # margin so the toolbar button doesn't clip them.
    m = big * 0.08
    span = big * 0.62
    radius = big * 0.10
    _rounded_rect(px, big, m, m, m + span, m + span, radius, COLOR_A)
    off = big - span - 2 * m
    _rounded_rect(px, big, m + off, m + off, m + off + span, m + off + span, radius, COLOR_B)

    rows = bytearray()
    n = SS * SS
    for y in range(size):
        rows.append(0)  # PNG filter type 0 (None) for this scanline
        for x in range(size):
            acc = [0, 0, 0, 0]
            for dy in range(SS):
                for dx in range(SS):
                    i = ((y * SS + dy) * big + (x * SS + dx)) * 4
                    for c in range(4):
                        acc[c] += px[i + c]
            # Average colour over the covered subpixels only - averaging over
            # all of them would darken the antialiased edge towards black
            # (the transparent subpixels carry rgb 0,0,0), the classic
            # non-premultiplied halo.
            alpha = acc[3] / n
            covered = acc[3] / 255 or 1
            rows += bytes(min(255, round(acc[c] / covered)) for c in range(3))
            rows.append(round(alpha))
    return bytes(rows)


def _png(size: int, raw: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def main() -> None:
    targets = {
        REPO_ROOT / "PCM" / "icon.png": 64,
        REPO_ROOT / "plugin" / "icons" / "kdif_24.png": 24,
        REPO_ROOT / "plugin" / "icons" / "kdif_48.png": 48,
    }
    for path, size in targets.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_png(size, _render(size)))
        print(f"wrote {path.relative_to(REPO_ROOT)} ({size}x{size})")


if __name__ == "__main__":
    main()
