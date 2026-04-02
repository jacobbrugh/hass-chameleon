"""ChameleonUltra BLE device client.

Async wrapper around BleakClient that speaks the ChameleonUltra protocol
over the Nordic UART Service (NUS). Handles MTU-aware chunking, command
serialization, and response correlation.

Connection lifecycle is owned by the caller (coordinator.py).
"""

from __future__ import annotations

import asyncio
import logging
import struct
from typing import Any

from bleak import BleakClient

from .const import (
    DEFAULT_COMMAND_TIMEOUT,
    MAX_DATA_LENGTH,
    NUS_RX_CHAR_UUID,
    SLOT_COUNT,
    Command,
    DeviceMode,
    DeviceModel,
    SenseType,
    TagType,
)
from .protocol import Frame, FrameAssembler, ProtocolError, StatusError, build_frame

_LOGGER = logging.getLogger(__name__)

# Minimum usable MTU payload (BLE default minus ATT header)
_MIN_MTU_PAYLOAD = 20


class ChameleonTimeoutError(ProtocolError):
    """Timed out waiting for a device response."""


class ChameleonUltraDevice:
    """High-level async interface to a ChameleonUltra over BLE.

    The caller must provide an already-connected BleakClient and subscribe
    this device's ``on_notification`` method to the NUS TX characteristic
    before sending any commands.
    """

    def __init__(self, client: BleakClient) -> None:
        self._client = client
        self._assembler = FrameAssembler()
        self._command_lock = asyncio.Lock()
        self._response_future: asyncio.Future[Frame] | None = None

    @property
    def client(self) -> BleakClient:
        return self._client

    # ------------------------------------------------------------------
    # BLE notification handler
    # ------------------------------------------------------------------

    def on_notification(self, _sender: Any, data: bytearray) -> None:
        """Feed incoming NUS TX data into the frame assembler.

        Must be registered via ``client.start_notify(NUS_TX_CHAR_UUID, device.on_notification)``.
        """
        frames = self._assembler.feed(data)
        for frame in frames:
            if self._response_future is not None and not self._response_future.done():
                self._response_future.set_result(frame)
            else:
                _LOGGER.debug(
                    "Unsolicited frame: cmd=%#06x status=%#06x len=%d",
                    frame.cmd,
                    frame.status,
                    len(frame.data),
                )

    # ------------------------------------------------------------------
    # Low-level command transport
    # ------------------------------------------------------------------

    async def send_command(
        self,
        cmd: Command,
        data: bytes = b"",
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> Frame:
        """Send a command and await the response.

        Serialized by ``_command_lock`` so only one command is in-flight at a
        time. The caller does NOT need to hold the lock.

        Raises:
            ChameleonTimeoutError: No response within ``timeout`` seconds.
            StatusError: Device returned a non-success status.
        """
        async with self._command_lock:
            return await self._send_locked(cmd, data, timeout)

    async def _send_locked(
        self,
        cmd: Command,
        data: bytes = b"",
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> Frame:
        """Inner send that assumes the caller already holds ``_command_lock``."""
        loop = asyncio.get_running_loop()
        self._response_future = loop.create_future()
        self._assembler.reset()

        frame_bytes = build_frame(cmd, data)
        mtu_payload = max(self._client.mtu_size - 3, _MIN_MTU_PAYLOAD)
        for i in range(0, len(frame_bytes), mtu_payload):
            chunk = frame_bytes[i : i + mtu_payload]
            await self._client.write_gatt_char(
                NUS_RX_CHAR_UUID, chunk, response=False
            )

        try:
            frame = await asyncio.wait_for(self._response_future, timeout=timeout)
        except asyncio.TimeoutError:
            self._response_future = None
            raise ChameleonTimeoutError(
                f"No response to {cmd.name} ({cmd:#06x}) within {timeout}s"
            ) from None
        finally:
            self._response_future = None

        if frame.cmd != cmd:
            raise ProtocolError(
                f"Response cmd {frame.cmd:#06x} != request cmd {cmd:#06x}"
            )
        if not frame.is_success:
            raise StatusError(cmd=frame.cmd, status=frame.status, data=frame.data)
        return frame

    async def send_command_sequence(
        self,
        commands: list[tuple[Command, bytes]],
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> list[Frame]:
        """Send multiple commands atomically under a single lock acquisition.

        Used by the unlock sequence (enable → sleep → disable) to prevent
        polls from interleaving.
        """
        async with self._command_lock:
            results: list[Frame] = []
            for cmd, data in commands:
                frame = await self._send_locked(cmd, data, timeout)
                results.append(frame)
            return results

    # ------------------------------------------------------------------
    # High-level command API
    # ------------------------------------------------------------------

    async def get_app_version(self) -> tuple[int, int]:
        """Return (major, minor) firmware version."""
        frame = await self.send_command(Command.GET_APP_VERSION)
        return frame.data[0], frame.data[1]

    async def get_git_version(self) -> str:
        """Return the firmware git version string."""
        frame = await self.send_command(Command.GET_GIT_VERSION)
        return frame.data.decode("utf-8", errors="replace")

    async def get_device_model(self) -> DeviceModel:
        """Return the device model (Ultra or Lite)."""
        frame = await self.send_command(Command.GET_DEVICE_MODEL)
        return DeviceModel(frame.data[0])

    async def get_device_chip_id(self) -> bytes:
        """Return the 8-byte nRF DEVICEID."""
        frame = await self.send_command(Command.GET_DEVICE_CHIP_ID)
        return bytes(frame.data[:8])

    async def get_battery_info(self) -> dict[str, int]:
        """Return battery voltage (mV) and percentage."""
        frame = await self.send_command(Command.GET_BATTERY_INFO)
        voltage = struct.unpack("!H", frame.data[0:2])[0]
        percentage = frame.data[2]
        return {"voltage": voltage, "percentage": percentage}

    async def get_device_mode(self) -> DeviceMode:
        """Return the current device mode."""
        frame = await self.send_command(Command.GET_DEVICE_MODE)
        return DeviceMode(frame.data[0])

    async def change_device_mode(self, mode: DeviceMode) -> None:
        """Switch between emulator and reader mode."""
        await self.send_command(Command.CHANGE_DEVICE_MODE, bytes([mode]))

    async def get_active_slot(self) -> int:
        """Return the active slot index (0-7)."""
        frame = await self.send_command(Command.GET_ACTIVE_SLOT)
        return frame.data[0]

    async def set_active_slot(self, slot: int) -> None:
        """Set the active slot (0-7)."""
        await self.send_command(Command.SET_ACTIVE_SLOT, bytes([slot]))

    async def get_slot_info(self) -> list[dict[str, int]]:
        """Return tag type info for all 8 slots.

        Returns a list of 8 dicts with ``hf_type`` and ``lf_type`` keys.
        """
        frame = await self.send_command(Command.GET_SLOT_INFO)
        slots: list[dict[str, int]] = []
        for i in range(SLOT_COUNT):
            offset = i * 4
            hf_type = struct.unpack("!H", frame.data[offset : offset + 2])[0]
            lf_type = struct.unpack("!H", frame.data[offset + 2 : offset + 4])[0]
            slots.append({"hf_type": hf_type, "lf_type": lf_type})
        return slots

    async def get_enabled_slots(self) -> list[dict[str, bool]]:
        """Return enable state for all 8 slots.

        Returns a list of 8 dicts with ``hf_enabled`` and ``lf_enabled`` keys.
        """
        frame = await self.send_command(Command.GET_ENABLED_SLOTS)
        slots: list[dict[str, bool]] = []
        for i in range(SLOT_COUNT):
            offset = i * 2
            hf_enabled = frame.data[offset] == 0x01
            lf_enabled = frame.data[offset + 1] == 0x01
            slots.append({"hf_enabled": hf_enabled, "lf_enabled": lf_enabled})
        return slots

    async def set_slot_enable(
        self, slot: int, sense_type: SenseType, enable: bool
    ) -> None:
        """Enable or disable a slot's HF or LF emulation."""
        await self.send_command(
            Command.SET_SLOT_ENABLE,
            bytes([slot, sense_type, int(enable)]),
        )

    async def set_slot_tag_type(self, slot: int, tag_type: TagType) -> None:
        """Set the tag type for a slot."""
        await self.send_command(
            Command.SET_SLOT_TAG_TYPE,
            bytes([slot]) + struct.pack("!H", tag_type),
        )

    async def get_slot_tag_nick(self, slot: int, sense_type: SenseType) -> str:
        """Get a slot's nickname (up to 32 bytes UTF-8)."""
        frame = await self.send_command(
            Command.GET_SLOT_TAG_NICK, bytes([slot, sense_type])
        )
        return frame.data.decode("utf-8", errors="replace")

    async def set_slot_tag_nick(
        self, slot: int, sense_type: SenseType, name: str
    ) -> None:
        """Set a slot's nickname (max 32 bytes UTF-8)."""
        name_bytes = name.encode("utf-8")[:32]
        await self.send_command(
            Command.SET_SLOT_TAG_NICK,
            bytes([slot, sense_type]) + name_bytes,
        )

    async def save_slot_config(self) -> None:
        """Persist current slot configuration to flash."""
        await self.send_command(Command.SLOT_DATA_CONFIG_SAVE)

    async def save_settings(self) -> None:
        """Persist device settings to flash."""
        await self.send_command(Command.SAVE_SETTINGS)

    # ------------------------------------------------------------------
    # Emulation data commands
    # ------------------------------------------------------------------

    async def get_anti_coll_data(self) -> dict[str, bytes | int]:
        """Read the current anti-collision data for the active slot.

        Returns dict with ``uid``, ``atqa``, ``sak``, ``ats`` keys.
        """
        frame = await self.send_command(Command.HF14A_GET_ANTI_COLL_DATA)
        if not frame.data:
            return {"uid": b"", "atqa": b"\x00\x00", "sak": 0, "ats": b""}
        uid_len = frame.data[0]
        uid = bytes(frame.data[1 : 1 + uid_len])
        pos = 1 + uid_len
        atqa = bytes(frame.data[pos : pos + 2])
        sak = frame.data[pos + 2]
        ats_len = frame.data[pos + 3] if pos + 3 < len(frame.data) else 0
        ats = bytes(frame.data[pos + 4 : pos + 4 + ats_len]) if ats_len else b""
        return {"uid": uid, "atqa": atqa, "sak": sak, "ats": ats}

    async def set_anti_coll_data(
        self,
        uid: bytes,
        atqa: bytes,
        sak: int,
        ats: bytes = b"",
    ) -> None:
        """Set anti-collision data for the active slot."""
        data = bytes([len(uid)]) + uid + atqa + bytes([sak, len(ats)]) + ats
        await self.send_command(Command.HF14A_SET_ANTI_COLL_DATA, data)

    async def read_emu_block_data(self, start_block: int, count: int) -> bytes:
        """Read emulation block data from the active slot.

        Args:
            start_block: First block number.
            count: Number of 16-byte blocks to read (1-32).

        Returns:
            Raw block data (count * 16 bytes).
        """
        frame = await self.send_command(
            Command.MF1_READ_EMU_BLOCK_DATA, bytes([start_block, count])
        )
        return bytes(frame.data)

    async def write_emu_block_data(self, start_block: int, data: bytes) -> None:
        """Write emulation block data to the active slot.

        Args:
            start_block: First block number.
            data: Raw block data (must be a multiple of 16 bytes, max 31 blocks).
        """
        if len(data) % 16 != 0:
            raise ValueError("Data must be a multiple of 16 bytes")
        if len(data) > 31 * 16:
            raise ValueError("Max 31 blocks (496 bytes) per write")
        await self.send_command(
            Command.MF1_WRITE_EMU_BLOCK_DATA,
            bytes([start_block]) + data,
        )
