"""Generate placeholder PNG icons for the OpsMemory PWA shell.

Pure stdlib (struct + zlib + binascii). Solid-color icons in OpsMemory dark slate.
Produces 192x192 (standard), 512x512 (standard), 512x512 (maskable).

Usage:
    python scripts/generate_placeholder_icons.py

Output:
    web/icons/icon-192.png
    web/icons/icon-512.png
    web/icons/icon-512-maskable.png

Replace with real branded icons before launch.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

# OpsMemory dark slate
BG_COLOR = (26, 26, 26, 255)  # RGBA
ACCENT = (200, 200, 200, 255)  # for the small "OM" mark


def make_solid_png(width: int, height: int, color: tuple[int, int, int, int]) -> bytes:
    """Build a valid 8-bit RGBA PNG with a single solid color."""
    return _make_png(width, height, lambda x, y: color)


def make_marked_png(width: int, height: int, bg: tuple[int, int, int, int],
                    mark: tuple[int, int, int, int]) -> bytes:
    """Solid background with a centered mark drawn as four rectangles forming 'OM'."""
    # Simple geometric mark: two vertical bars + one horizontal bar (rough 'O' + 'M' suggestion).
    # Centered, scaled to ~50% width/height.
    cx, cy = width // 2, height // 2
    half_w = width // 4
    half_h = height // 4
    # Vertical bar thickness
    t = max(2, width // 32)

    def pixel(x: int, y: int) -> tuple[int, int, int, int]:
        # Outer rectangle outline
        in_box = (cx - half_w <= x <= cx + half_w) and (cy - half_h <= y <= cy + half_h)
        on_outline = in_box and (
            x < cx - half_w + t or x > cx + half_w - t
            or y < cy - half_h + t or y > cy + half_h - t
        )
        # Diagonal mark across the box
        rel = (x - (cx - half_w)) / max(1, 2 * half_w)
        diag_y = (cy - half_h) + int(rel * 2 * half_h)
        on_diag = in_box and abs(y - diag_y) < t
        if on_outline or on_diag:
            return mark
        return bg

    return _make_png(width, height, pixel)


def _make_png(width: int, height: int, pixel_fn) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        length = struct.pack(">I", len(data))
        crc = zlib.crc32(tag + data)
        return length + tag + data + struct.pack(">I", crc)

    sig = b"\x89PNG\r\n\x1a\n"
    # 8-bit RGBA = bit depth 8, color type 6
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))

    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter byte: None
        for x in range(width):
            r, g, b, a = pixel_fn(x, y)
            raw.append(r)
            raw.append(g)
            raw.append(b)
            raw.append(a)
    idat = chunk(b"IDAT", zlib.compress(bytes(raw), 9))

    iend = chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


def main() -> None:
    out_dir = Path(__file__).resolve().parent.parent / "web" / "icons"
    out_dir.mkdir(parents=True, exist_ok=True)

    targets = [
        ("icon-192.png", 192, make_marked_png(192, 192, BG_COLOR, ACCENT)),
        ("icon-512.png", 512, make_marked_png(512, 512, BG_COLOR, ACCENT)),
        ("icon-512-maskable.png", 512, make_solid_png(512, 512, BG_COLOR)),
    ]

    for filename, _, data in targets:
        path = out_dir / filename
        path.write_bytes(data)
        print(f"wrote {path} ({len(data)} bytes)")


if __name__ == "__main__":
    main()
