"""Convert audio to mono WAV, including one verified raw Plaud Opus layout."""

from __future__ import annotations

import logging
import math
import os
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


class ConversionError(RuntimeError):
    pass


_PACKET_BYTES = 80
_PACKETS_PER_PAGE = 50
_TOC = 0xB8  # One mono CELT wideband frame, 20 ms.
_SAMPLES_PER_PACKET = 960  # Opus granule positions always use 48 kHz.


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _decode(src: Path, dst: Path, sample_rate: int, *, strict: bool = False) -> None:
    cmd = ["ffmpeg", "-y"]
    if strict:
        cmd.append("-xerror")
    cmd.extend(["-i", str(src), "-ac", "1", "-ar", str(sample_rate), "-c:a", "pcm_s16le", str(dst)])
    log.debug("Running: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise ConversionError(f"ffmpeg failed ({proc.returncode}): {proc.stderr[-800:]}")


def _raw_packet_count(src: Path) -> int:
    """Accept only complete, fixed-size packets with the observed Opus TOC."""
    size = src.stat().st_size
    if size % _PACKET_BYTES or size < 50 * _PACKET_BYTES:
        raise ConversionError("unsupported headerless Opus packet layout")
    with src.open("rb") as stream:
        for _ in range(size // _PACKET_BYTES):
            packet = stream.read(_PACKET_BYTES)
            if len(packet) != _PACKET_BYTES or packet[0] != _TOC:
                raise ConversionError("unsupported headerless Opus packet layout")
        if stream.read(1):
            raise ConversionError("headerless Opus changed during validation")
    return size // _PACKET_BYTES


def _crc_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        value = byte << 24
        for _ in range(8):
            value = ((value << 1) ^ (0x04C11DB7 if value & 0x80000000 else 0)) & 0xFFFFFFFF
        table.append(value)
    return tuple(table)


_CRC_TABLE = _crc_table()


def _ogg_crc(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC_TABLE[((crc >> 24) ^ byte) & 0xFF]
    return crc


def _write_page(stream, packets: list[bytes], sequence: int, granule: int, flags: int) -> None:
    lacing = bytes(len(packet) for packet in packets)
    header = (
        b"OggS"
        + bytes((0, flags))
        + struct.pack("<QII", granule, 1, sequence)
        + b"\x00\x00\x00\x00"
        + bytes((len(lacing),))
        + lacing
    )
    page = header + b"".join(packets)
    page = page[:22] + struct.pack("<I", _ogg_crc(page)) + page[26:]
    stream.write(page)


def _wrap_raw_opus(src: Path, dst: Path, packet_count: int) -> None:
    # OpusHead's input rate is informational; decoded Opus granules use 48 kHz.
    head = b"OpusHead" + struct.pack("<BBHIhB", 1, 1, 0, 16000, 0, 0)
    vendor = b"localplaud"
    tags = b"OpusTags" + struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", 0)
    with src.open("rb") as raw, dst.open("wb") as ogg:
        _write_page(ogg, [head], 0, 0, 0x02)  # BOS
        _write_page(ogg, [tags], 1, 0, 0)
        sequence = 2
        for offset in range(0, packet_count, _PACKETS_PER_PAGE):
            count = min(_PACKETS_PER_PAGE, packet_count - offset)
            packets = [raw.read(_PACKET_BYTES) for _ in range(count)]
            if any(len(packet) != _PACKET_BYTES or packet[0] != _TOC for packet in packets):
                raise ConversionError("headerless Opus changed during wrapping")
            final = offset + count == packet_count
            _write_page(
                ogg, packets, sequence, (offset + count) * _SAMPLES_PER_PACKET, 0x04 if final else 0
            )  # EOS on final audio page
            sequence += 1
        if raw.read(1):
            raise ConversionError("headerless Opus changed during wrapping")


def _temporary_path(parent: Path, suffix: str) -> Path:
    fd, name = tempfile.mkstemp(prefix=".localplaud-convert-", suffix=suffix, dir=parent)
    os.close(fd)
    return Path(name)


def to_wav(
    src: Path, dst: Path, sample_rate: int = 16000, expected_duration_seconds: float | None = None
) -> Path:
    """Convert media to mono WAV; recover only validated 80-byte raw Opus packets."""
    if not ffmpeg_available():
        raise ConversionError("ffmpeg not found on PATH — required for audio conversion")
    if src.resolve() == dst.resolve():
        raise ConversionError("source and destination must differ")
    dst.parent.mkdir(parents=True, exist_ok=True)
    output = _temporary_path(dst.parent, ".wav")
    container: Path | None = None
    try:
        try:
            _decode(src, output, sample_rate)
        except ConversionError as ffmpeg_error:
            if src.suffix.lower() != ".opus" or not src.is_file():
                raise
            try:
                packet_count = _raw_packet_count(src)
            except ConversionError:
                raise ffmpeg_error from None
            duration = packet_count * 0.02
            if expected_duration_seconds is not None:
                expected = float(expected_duration_seconds)
                if (
                    not math.isfinite(expected)
                    or expected < 0
                    or abs(duration - expected) > max(1.0, 0.02 * expected)
                ):
                    raise ConversionError(
                        "headerless Opus duration differs from recording metadata"
                    ) from ffmpeg_error
            container = _temporary_path(dst.parent, ".ogg")
            _wrap_raw_opus(src, container, packet_count)
            _decode(container, output, sample_rate, strict=True)
        os.replace(output, dst)
        return dst
    finally:
        output.unlink(missing_ok=True)
        if container is not None:
            container.unlink(missing_ok=True)
