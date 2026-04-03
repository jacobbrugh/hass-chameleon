"""ChameleonUltra binary protocol: frame encoding, decoding, and reassembly.

This module is pure Python with no async or BLE dependencies, making it
fully unit-testable without mocks.

Frame format (all multi-byte fields are big-endian):

    SOF(1) | LRC1(1) | CMD(2) | STATUS(2) | LEN(2) | LRC2(1) | DATA(0..512) | LRC3(1)

- SOF: always 0x11
- LRC1: always 0xEF (LRC of SOF alone)
- LRC2: LRC over bytes [0..8] (SOF through LEN)
- LRC3: LRC over DATA bytes; 0x00 if LEN == 0
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from enum import IntEnum, auto

from .const import MAX_DATA_LENGTH, SOF, LRC1 as EXPECTED_LRC1


class ProtocolError(Exception):
    """Base exception for protocol-level errors."""


class FrameError(ProtocolError):
    """Malformed or corrupt frame."""


class StatusError(ProtocolError):
    """Device returned a non-success status code."""

    def __init__(self, cmd: int, status: int, data: bytes = b"") -> None:
        self.cmd = cmd
        self.status = status
        self.data = data
        super().__init__(f"Command {cmd:#06x} failed with status {status:#06x}")


def lrc(data: bytes | bytearray) -> int:
    """Compute 8-bit LRC (two's complement of sum mod 256)."""
    return (~sum(data) + 1) & 0xFF


def build_frame(cmd: int, data: bytes = b"", status: int = 0x0000) -> bytes:
    """Build a complete protocol frame.

    Args:
        cmd: Command code (2 bytes, big-endian).
        data: Payload bytes (0 to MAX_DATA_LENGTH).
        status: Status field (0x0000 for requests).

    Returns:
        Complete frame bytes ready to send.

    Raises:
        ValueError: If data exceeds MAX_DATA_LENGTH.
    """
    if len(data) > MAX_DATA_LENGTH:
        raise ValueError(f"Data length {len(data)} exceeds max {MAX_DATA_LENGTH}")

    header = struct.pack("!HHH", cmd, status, len(data))  # 6 bytes
    preamble = bytes([SOF, EXPECTED_LRC1]) + header
    lrc2 = lrc(preamble)
    lrc3 = lrc(data) if data else 0x00
    return preamble + bytes([lrc2]) + data + bytes([lrc3])


@dataclass(frozen=True, slots=True)
class Frame:
    """A parsed protocol frame."""

    cmd: int
    status: int
    data: bytes

    # Known error status codes from the protocol spec
    _ERROR_STATUSES = frozenset({0x0001, 0x0002, 0x0003, 0x0004, 0xFFFF})

    @property
    def is_success(self) -> bool:
        return self.status not in self._ERROR_STATUSES

    @property
    def is_error(self) -> bool:
        """Check for known error statuses. The device returns non-zero status
        codes (e.g. 0x0068) for successful responses, so we only reject
        status codes that are explicitly defined as errors in the protocol."""
        return self.status in self._ERROR_STATUSES


class _State(IntEnum):
    """FrameAssembler internal states."""

    WAIT_SOF = auto()
    WAIT_LRC1 = auto()
    ACCUMULATE_HEADER = auto()
    VALIDATE_LRC2 = auto()
    ACCUMULATE_DATA = auto()
    VALIDATE_LRC3 = auto()


class FrameAssembler:
    """State machine that accumulates BLE notification bytes into complete Frames.

    Usage:
        assembler = FrameAssembler()
        for ble_chunk in notifications:
            for frame in assembler.feed(ble_chunk):
                handle(frame)

    Handles:
    - Partial frames split across multiple BLE notifications
    - Multiple frames concatenated in a single notification
    - SOF byte (0x11) appearing in data payloads (validated by LRC1)
    - Corrupt frames (resync to next SOF)
    - Stale partial frames (timeout-based discard)
    """

    FRAME_TIMEOUT = 3.0  # seconds to wait for a complete frame

    def __init__(self) -> None:
        self._state = _State.WAIT_SOF
        self._buf = bytearray()
        self._data_len = 0
        self._frame_start: float = 0.0

    def reset(self) -> None:
        """Reset the assembler to its initial state."""
        self._state = _State.WAIT_SOF
        self._buf.clear()
        self._data_len = 0
        self._frame_start = 0.0

    def feed(self, data: bytes | bytearray) -> list[Frame]:
        """Process incoming bytes and return any complete frames.

        Args:
            data: Raw bytes from a BLE notification.

        Returns:
            List of complete Frame objects (may be empty).
        """
        frames: list[Frame] = []
        now = time.monotonic()

        # Check for stale partial frame
        if (
            self._state != _State.WAIT_SOF
            and self._frame_start > 0
            and (now - self._frame_start) > self.FRAME_TIMEOUT
        ):
            self.reset()

        for byte in data:
            frame = self._process_byte(byte, now)
            if frame is not None:
                frames.append(frame)

        return frames

    def _process_byte(self, byte: int, now: float) -> Frame | None:
        """Process a single byte through the state machine.

        Returns a Frame if one was completed, else None.
        """
        if self._state == _State.WAIT_SOF:
            if byte == SOF:
                self._buf.clear()
                self._buf.append(byte)
                self._frame_start = now
                self._state = _State.WAIT_LRC1
            return None

        if self._state == _State.WAIT_LRC1:
            if byte == EXPECTED_LRC1:
                self._buf.append(byte)
                self._state = _State.ACCUMULATE_HEADER
            elif byte == SOF:
                # This byte might be the start of a new frame
                self._buf.clear()
                self._buf.append(byte)
                self._frame_start = now
                # Stay in WAIT_LRC1
            else:
                self._state = _State.WAIT_SOF
            return None

        if self._state == _State.ACCUMULATE_HEADER:
            self._buf.append(byte)
            # Need 6 more bytes after SOF+LRC1: CMD(2)+STATUS(2)+LEN(2)
            if len(self._buf) == 8:  # SOF + LRC1 + 6 header bytes
                self._state = _State.VALIDATE_LRC2
            return None

        if self._state == _State.VALIDATE_LRC2:
            expected_lrc2 = lrc(self._buf)
            if byte != expected_lrc2:
                # Corrupt header — try to resync
                self._state = _State.WAIT_SOF
                # Check if this byte is SOF to start over
                if byte == SOF:
                    self._buf.clear()
                    self._buf.append(byte)
                    self._frame_start = now
                    self._state = _State.WAIT_LRC1
                return None
            self._buf.append(byte)  # store LRC2
            # Parse LEN from buf[6:8]
            self._data_len = struct.unpack("!H", self._buf[6:8])[0]
            if self._data_len > MAX_DATA_LENGTH:
                self._state = _State.WAIT_SOF
                return None
            if self._data_len == 0:
                self._state = _State.VALIDATE_LRC3
            else:
                self._state = _State.ACCUMULATE_DATA
            return None

        if self._state == _State.ACCUMULATE_DATA:
            self._buf.append(byte)
            # buf is: preamble(9) + data bytes so far
            data_received = len(self._buf) - 9
            if data_received == self._data_len:
                self._state = _State.VALIDATE_LRC3
            return None

        if self._state == _State.VALIDATE_LRC3:
            data_bytes = bytes(self._buf[9:]) if self._data_len > 0 else b""
            expected_lrc3 = lrc(data_bytes) if data_bytes else 0x00
            self._state = _State.WAIT_SOF
            if byte != expected_lrc3:
                return None
            # Successfully parsed a complete frame
            cmd = struct.unpack("!H", self._buf[2:4])[0]
            status = struct.unpack("!H", self._buf[4:6])[0]
            return Frame(cmd=cmd, status=status, data=data_bytes)

        return None  # pragma: no cover
