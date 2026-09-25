"""Minimal PNG read/write with numpy and zlib, so stills need no Pillow.

Writes 8-bit RGB or RGBA. Reads non-interlaced 8-bit greyscale, RGB and RGBA
(the common case for exported digital art). Pillow is used for anything else
when it is installed.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np

_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def write_png(path: Path, pixels: np.ndarray, *, level: int = 6) -> None:
    array = np.ascontiguousarray(pixels, dtype=np.uint8)
    if array.ndim != 3 or array.shape[2] not in (3, 4):
        raise ValueError("write_png expects an (h, w, 3|4) uint8 array")
    height, width, channels = array.shape
    colour_type = 2 if channels == 3 else 6
    rows = np.empty((height, 1 + width * channels), dtype=np.uint8)
    rows[:, 0] = 0
    rows[:, 1:] = array.reshape(height, width * channels)
    header = struct.pack(">IIBBBBB", width, height, 8, colour_type, 0, 0, 0)
    body = zlib.compress(rows.tobytes(), level)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(_SIGNATURE)
        handle.write(_chunk(b"IHDR", header))
        handle.write(_chunk(b"IDAT", body))
        handle.write(_chunk(b"IEND", b""))


def read_image(path: Path) -> np.ndarray | None:
    """RGB uint8 array, or None when the file cannot be decoded here."""
    try:
        from PIL import Image

        with Image.open(path) as image:
            return np.asarray(image.convert("RGB"), dtype=np.uint8)
    except ImportError:
        pass
    except OSError:
        return None
    if path.suffix.lower() != ".png":
        return None
    return _read_png(path)


def _read_png(path: Path) -> np.ndarray | None:
    data = path.read_bytes()
    if not data.startswith(_SIGNATURE):
        return None
    pos = len(_SIGNATURE)
    width = height = depth = colour_type = interlace = None
    idat = bytearray()
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos : pos + 8])
        payload = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, colour_type, _, _, interlace = struct.unpack(">IIBBBBB", payload)
        elif kind == b"IDAT":
            idat.extend(payload)
        elif kind == b"IEND":
            break
    channels = {0: 1, 2: 3, 6: 4}.get(colour_type or -1)
    if not width or not height or depth != 8 or interlace != 0 or channels is None:
        return None
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    out = np.zeros((height, stride), dtype=np.uint8)
    prior = np.zeros(stride, dtype=np.int32)
    for y in range(height):
        start = y * (stride + 1)
        kind = raw[start]
        line = np.frombuffer(raw, dtype=np.uint8, count=stride, offset=start + 1).astype(np.int32)
        recon = _unfilter(kind, line, prior, channels)
        if recon is None:
            return None
        out[y] = recon.astype(np.uint8)
        prior = recon
    image = out.reshape(height, width, channels)
    if channels == 1:
        image = np.repeat(image, 3, axis=2)
    return np.ascontiguousarray(image[:, :, :3])


def _unfilter(kind: int, line: np.ndarray, prior: np.ndarray, bpp: int) -> np.ndarray | None:
    if kind == 0:
        return line
    if kind == 2:
        return (line + prior) & 0xFF
    recon = np.zeros_like(line)
    for i in range(line.size):
        left = recon[i - bpp] if i >= bpp else 0
        up = prior[i]
        if kind == 1:
            value = line[i] + left
        elif kind == 3:
            value = line[i] + ((left + up) >> 1)
        elif kind == 4:
            upper_left = prior[i - bpp] if i >= bpp else 0
            p = left + up - upper_left
            pa, pb, pc = abs(p - left), abs(p - up), abs(p - upper_left)
            predictor = left if pa <= pb and pa <= pc else (up if pb <= pc else upper_left)
            value = line[i] + predictor
        else:
            return None
        recon[i] = value & 0xFF
    return recon


def _chunk(kind: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)
