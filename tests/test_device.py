"""Tests for the ChameleonUltra BLE device client.

Uses mocked BleakClient to test command serialization, MTU chunking,
response correlation, and high-level command parsing.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from custom_components.chameleon_ultra.const import (
    NUS_RX_CHAR_UUID,
    Command,
    DeviceMode,
    DeviceModel,
    SenseType,
)
from custom_components.chameleon_ultra.device import (
    ChameleonTimeoutError,
    ChameleonUltraDevice,
)
from custom_components.chameleon_ultra.protocol import (
    ProtocolError,
    StatusError,
    build_frame,
)


def _make_mock_client(mtu: int = 247) -> MagicMock:
    """Create a mock BleakClient with configurable MTU."""
    client = MagicMock()
    type(client).mtu_size = PropertyMock(return_value=mtu)
    client.write_gatt_char = AsyncMock()
    return client


def _make_response(cmd: Command, data: bytes = b"", status: int = 0) -> bytes:
    """Build a response frame as the device would send it."""
    return build_frame(cmd, data, status=status)


def _simulate_response(
    device: ChameleonUltraDevice, cmd: Command, data: bytes = b"", status: int = 0
) -> None:
    """Simulate a BLE notification containing a response frame."""
    raw = _make_response(cmd, data, status)
    device.on_notification(None, bytearray(raw))


# ---------------------------------------------------------------------------
# Basic command flow
# ---------------------------------------------------------------------------


class TestSendCommand:
    @pytest.mark.asyncio
    async def test_basic_command_response(self) -> None:
        client = _make_mock_client()
        device = ChameleonUltraDevice(client)

        async def respond() -> None:
            await asyncio.sleep(0.01)
            _simulate_response(device, Command.GET_APP_VERSION, b"\x02\x01")

        asyncio.get_event_loop().create_task(respond())
        frame = await device.send_command(Command.GET_APP_VERSION)
        assert frame.cmd == Command.GET_APP_VERSION
        assert frame.data == b"\x02\x01"

    @pytest.mark.asyncio
    async def test_write_called_with_correct_uuid(self) -> None:
        client = _make_mock_client()
        device = ChameleonUltraDevice(client)

        async def respond() -> None:
            await asyncio.sleep(0.01)
            _simulate_response(device, Command.GET_APP_VERSION, b"\x02\x01")

        asyncio.get_event_loop().create_task(respond())
        await device.send_command(Command.GET_APP_VERSION)
        call_args = client.write_gatt_char.call_args_list[0]
        assert call_args[0][0] == NUS_RX_CHAR_UUID
        assert call_args[1]["response"] is False

    @pytest.mark.asyncio
    async def test_timeout_raises(self) -> None:
        client = _make_mock_client()
        device = ChameleonUltraDevice(client)
        with pytest.raises(ChameleonTimeoutError, match="GET_APP_VERSION"):
            await device.send_command(Command.GET_APP_VERSION, timeout=0.05)

    @pytest.mark.asyncio
    async def test_invalid_cmd_status_raises(self) -> None:
        client = _make_mock_client()
        device = ChameleonUltraDevice(client)

        async def respond() -> None:
            await asyncio.sleep(0.01)
            _simulate_response(device, Command.SET_SLOT_ENABLE, status=0xFFFF)

        asyncio.get_event_loop().create_task(respond())
        with pytest.raises(StatusError) as exc_info:
            await device.send_command(Command.SET_SLOT_ENABLE, b"\x00\x01\x01")
        assert exc_info.value.status == 0xFFFF

    @pytest.mark.asyncio
    async def test_nonzero_status_accepted(self) -> None:
        """Device returns non-zero status (e.g. 0x0068) for success."""
        client = _make_mock_client()
        device = ChameleonUltraDevice(client)

        async def respond() -> None:
            await asyncio.sleep(0.01)
            _simulate_response(device, Command.GET_APP_VERSION, b"\x02\x01", status=0x0068)

        asyncio.get_event_loop().create_task(respond())
        frame = await device.send_command(Command.GET_APP_VERSION)
        assert frame.data == b"\x02\x01"
        assert frame.status == 0x0068

    @pytest.mark.asyncio
    async def test_cmd_mismatch_raises(self) -> None:
        client = _make_mock_client()
        device = ChameleonUltraDevice(client)

        async def respond_wrong_cmd() -> None:
            await asyncio.sleep(0.01)
            _simulate_response(device, Command.GET_BATTERY_INFO, b"\x10\x00\x50")

        asyncio.get_event_loop().create_task(respond_wrong_cmd())
        with pytest.raises(ProtocolError, match="Response cmd"):
            await device.send_command(Command.GET_APP_VERSION)


# ---------------------------------------------------------------------------
# MTU chunking
# ---------------------------------------------------------------------------


class TestMtuChunking:
    @pytest.mark.asyncio
    async def test_small_frame_single_write(self) -> None:
        """A frame smaller than MTU should be sent in one write."""
        client = _make_mock_client(mtu=247)
        device = ChameleonUltraDevice(client)

        async def respond() -> None:
            await asyncio.sleep(0.01)
            _simulate_response(device, Command.GET_APP_VERSION, b"\x02\x01")

        asyncio.get_event_loop().create_task(respond())
        await device.send_command(Command.GET_APP_VERSION)
        assert client.write_gatt_char.call_count == 1

    @pytest.mark.asyncio
    async def test_large_frame_chunked(self) -> None:
        """A frame larger than MTU payload should be chunked."""
        # MTU 23 -> payload = 20 bytes
        client = _make_mock_client(mtu=23)
        device = ChameleonUltraDevice(client)

        # 100 bytes of data -> frame = 110 bytes -> ceil(110/20) = 6 chunks
        data = bytes(100)

        async def respond() -> None:
            await asyncio.sleep(0.01)
            _simulate_response(
                device, Command.MF1_WRITE_EMU_BLOCK_DATA, b""
            )

        asyncio.get_event_loop().create_task(respond())
        await device.send_command(Command.MF1_WRITE_EMU_BLOCK_DATA, data)
        assert client.write_gatt_char.call_count == 6

    @pytest.mark.asyncio
    async def test_chunks_reconstruct_original_frame(self) -> None:
        """All chunks concatenated should equal the original frame bytes."""
        client = _make_mock_client(mtu=23)
        device = ChameleonUltraDevice(client)
        data = bytes(range(50))

        async def respond() -> None:
            await asyncio.sleep(0.01)
            _simulate_response(device, Command.MF1_WRITE_EMU_BLOCK_DATA)

        asyncio.get_event_loop().create_task(respond())
        await device.send_command(Command.MF1_WRITE_EMU_BLOCK_DATA, data)

        sent = b""
        for call in client.write_gatt_char.call_args_list:
            sent += bytes(call[0][1])
        expected = build_frame(Command.MF1_WRITE_EMU_BLOCK_DATA, data)
        assert sent == expected


# ---------------------------------------------------------------------------
# Command serialization
# ---------------------------------------------------------------------------


class TestCommandLock:
    @pytest.mark.asyncio
    async def test_concurrent_commands_serialized(self) -> None:
        """Two concurrent send_command calls should execute sequentially."""
        client = _make_mock_client()
        device = ChameleonUltraDevice(client)
        order: list[str] = []

        async def delayed_respond(cmd: Command, delay: float) -> None:
            await asyncio.sleep(delay)
            _simulate_response(device, cmd, b"\x00")

        async def cmd1() -> None:
            # Schedule response for cmd1
            asyncio.get_event_loop().create_task(
                delayed_respond(Command.GET_APP_VERSION, 0.02)
            )
            await device.send_command(Command.GET_APP_VERSION)
            order.append("cmd1_done")

        async def cmd2() -> None:
            # Small delay so cmd1 grabs the lock first
            await asyncio.sleep(0.005)
            asyncio.get_event_loop().create_task(
                delayed_respond(Command.GET_BATTERY_INFO, 0.02)
            )
            await device.send_command(Command.GET_BATTERY_INFO)
            order.append("cmd2_done")

        await asyncio.gather(cmd1(), cmd2())
        assert order == ["cmd1_done", "cmd2_done"]

    @pytest.mark.asyncio
    async def test_send_command_sequence_atomic(self) -> None:
        """send_command_sequence should hold the lock for all commands."""
        client = _make_mock_client()
        device = ChameleonUltraDevice(client)
        responses_sent = 0

        async def respond_to_each() -> None:
            nonlocal responses_sent
            for cmd in [Command.SET_SLOT_ENABLE, Command.SET_SLOT_ENABLE]:
                await asyncio.sleep(0.01)
                _simulate_response(device, cmd)
                responses_sent += 1

        asyncio.get_event_loop().create_task(respond_to_each())
        results = await device.send_command_sequence([
            (Command.SET_SLOT_ENABLE, b"\x00\x01\x01"),
            (Command.SET_SLOT_ENABLE, b"\x00\x01\x00"),
        ])
        assert len(results) == 2
        assert responses_sent == 2


# ---------------------------------------------------------------------------
# Notification handling
# ---------------------------------------------------------------------------


class TestNotificationHandling:
    @pytest.mark.asyncio
    async def test_fragmented_response(self) -> None:
        """Response arriving in multiple BLE notifications should reassemble."""
        client = _make_mock_client()
        device = ChameleonUltraDevice(client)
        raw = _make_response(Command.GET_BATTERY_INFO, b"\x0F\xA0\x64")

        async def respond_fragmented() -> None:
            await asyncio.sleep(0.01)
            # Send in two fragments
            device.on_notification(None, bytearray(raw[:5]))
            device.on_notification(None, bytearray(raw[5:]))

        asyncio.get_event_loop().create_task(respond_fragmented())
        frame = await device.send_command(Command.GET_BATTERY_INFO)
        assert frame.data == b"\x0F\xA0\x64"

    def test_unsolicited_frame_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """Frames arriving without a pending future should be logged."""
        client = _make_mock_client()
        device = ChameleonUltraDevice(client)
        with caplog.at_level("DEBUG"):
            _simulate_response(device, Command.GET_APP_VERSION, b"\x02\x01")
        assert "Unsolicited frame" in caplog.text


# ---------------------------------------------------------------------------
# High-level command API
# ---------------------------------------------------------------------------


class TestHighLevelCommands:
    @pytest.mark.asyncio
    async def test_get_app_version(self) -> None:
        client = _make_mock_client()
        device = ChameleonUltraDevice(client)

        async def respond() -> None:
            await asyncio.sleep(0.01)
            _simulate_response(device, Command.GET_APP_VERSION, b"\x02\x05")

        asyncio.get_event_loop().create_task(respond())
        major, minor = await device.get_app_version()
        assert (major, minor) == (2, 5)

    @pytest.mark.asyncio
    async def test_get_git_version(self) -> None:
        client = _make_mock_client()
        device = ChameleonUltraDevice(client)

        async def respond() -> None:
            await asyncio.sleep(0.01)
            _simulate_response(
                device, Command.GET_GIT_VERSION, b"v2.1.0"
            )

        asyncio.get_event_loop().create_task(respond())
        version = await device.get_git_version()
        assert version == "v2.1.0"

    @pytest.mark.asyncio
    async def test_get_device_model(self) -> None:
        client = _make_mock_client()
        device = ChameleonUltraDevice(client)

        async def respond() -> None:
            await asyncio.sleep(0.01)
            _simulate_response(device, Command.GET_DEVICE_MODEL, b"\x00")

        asyncio.get_event_loop().create_task(respond())
        model = await device.get_device_model()
        assert model == DeviceModel.ULTRA

    @pytest.mark.asyncio
    async def test_get_battery_info(self) -> None:
        client = _make_mock_client()
        device = ChameleonUltraDevice(client)

        async def respond() -> None:
            await asyncio.sleep(0.01)
            # voltage=4000 (0x0FA0), percentage=100 (0x64)
            _simulate_response(device, Command.GET_BATTERY_INFO, b"\x0F\xA0\x64")

        asyncio.get_event_loop().create_task(respond())
        info = await device.get_battery_info()
        assert info == {"voltage": 4000, "percentage": 100}

    @pytest.mark.asyncio
    async def test_get_device_mode(self) -> None:
        client = _make_mock_client()
        device = ChameleonUltraDevice(client)

        async def respond() -> None:
            await asyncio.sleep(0.01)
            _simulate_response(device, Command.GET_DEVICE_MODE, b"\x00")

        asyncio.get_event_loop().create_task(respond())
        mode = await device.get_device_mode()
        assert mode == DeviceMode.EMULATOR

    @pytest.mark.asyncio
    async def test_get_active_slot(self) -> None:
        client = _make_mock_client()
        device = ChameleonUltraDevice(client)

        async def respond() -> None:
            await asyncio.sleep(0.01)
            _simulate_response(device, Command.GET_ACTIVE_SLOT, b"\x03")

        asyncio.get_event_loop().create_task(respond())
        slot = await device.get_active_slot()
        assert slot == 3

    @pytest.mark.asyncio
    async def test_set_slot_enable(self) -> None:
        client = _make_mock_client()
        device = ChameleonUltraDevice(client)

        async def respond() -> None:
            await asyncio.sleep(0.01)
            _simulate_response(device, Command.SET_SLOT_ENABLE)

        asyncio.get_event_loop().create_task(respond())
        await device.set_slot_enable(0, SenseType.HF, True)

        # Verify the payload sent
        call_args = client.write_gatt_char.call_args_list[0]
        sent_frame = bytes(call_args[0][1])
        # The data payload should be [slot=0, sense=1, enable=1]
        # Data starts at byte 9 in the frame
        assert sent_frame[9:12] == b"\x00\x01\x01"

    @pytest.mark.asyncio
    async def test_get_enabled_slots(self) -> None:
        client = _make_mock_client()
        device = ChameleonUltraDevice(client)

        # 8 pairs: all HF enabled, all LF disabled
        data = bytes([0x01, 0x00] * 8)

        async def respond() -> None:
            await asyncio.sleep(0.01)
            _simulate_response(device, Command.GET_ENABLED_SLOTS, data)

        asyncio.get_event_loop().create_task(respond())
        slots = await device.get_enabled_slots()
        assert len(slots) == 8
        for slot in slots:
            assert slot["hf_enabled"] is True
            assert slot["lf_enabled"] is False

    @pytest.mark.asyncio
    async def test_get_slot_info(self) -> None:
        client = _make_mock_client()
        device = ChameleonUltraDevice(client)

        import struct

        # Slot 0: MF1_1K (HF=2), EM410X (LF=100); rest unknown
        data = struct.pack("!HH", 2, 100) + bytes(28)

        async def respond() -> None:
            await asyncio.sleep(0.01)
            _simulate_response(device, Command.GET_SLOT_INFO, data)

        asyncio.get_event_loop().create_task(respond())
        slots = await device.get_slot_info()
        assert slots[0]["hf_type"] == 2
        assert slots[0]["lf_type"] == 100
        assert slots[1]["hf_type"] == 0

    @pytest.mark.asyncio
    async def test_write_emu_block_data_validates(self) -> None:
        client = _make_mock_client()
        device = ChameleonUltraDevice(client)

        with pytest.raises(ValueError, match="multiple of 16"):
            await device.write_emu_block_data(0, bytes(15))

        with pytest.raises(ValueError, match="Max 31 blocks"):
            await device.write_emu_block_data(0, bytes(32 * 16))
