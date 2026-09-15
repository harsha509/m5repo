"""Reads a built firmware's identity straight out of its .bin.

The ESP app descriptor sits at 0x20 with magic 0xABCD5432, and its
app_elf_sha256 field is byte-identical to sha256(firmware.elf), so it changes
on every rebuild. That makes it the build id for OTA with no version file and
nothing to track in NVS on the device.
"""

import struct
from pathlib import Path
from typing import NamedTuple, Optional

APP_DESC_OFFSET = 0x20
APP_DESC_MAGIC = 0xABCD5432
APP_DESC_SIZE = 256
ELF_SHA256_OFFSET = APP_DESC_OFFSET + 144
IMAGE_MAGIC = 0xE9


class FirmwareImage(NamedTuple):
    path: Path
    build: str
    size: int
    mtime: float


def read_image(path: Path) -> Optional[FirmwareImage]:
    """Returns the image's build id and size, or None if it isn't a valid app."""
    try:
        blob = path.read_bytes()
    except OSError:
        return None
    if len(blob) < APP_DESC_OFFSET + APP_DESC_SIZE or blob[0] != IMAGE_MAGIC:
        return None
    (magic,) = struct.unpack_from("<I", blob, APP_DESC_OFFSET)
    if magic != APP_DESC_MAGIC:
        return None
    build = blob[ELF_SHA256_OFFSET:ELF_SHA256_OFFSET + 32].hex()
    return FirmwareImage(path, build, len(blob), path.stat().st_mtime)
