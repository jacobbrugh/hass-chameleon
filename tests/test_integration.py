"""Actor-model integration tests for ChameleonUltra.

Tests exercise real code paths by simulating the ChameleonUltra device as an
async actor that receives protocol frames and generates responses. The BLE
transport (BleakClient) is mocked at the socket boundary — everything above
it (frame assembly, protocol parsing, device commands, coordinator logic)
runs for real.

The mock device maintains internal state (slots, battery, mode, block data)
and responds to commands exactly as the real firmware would, so tests catch
bugs in serialization, response parsing, and state transitions.
"""

from __future__ import annotations

import asyncio
import struct

import pytest

from custom_components.chameleon_ultra.const import (
    NUS_RX_CHAR_UUID,
    NUS_TX_CHAR_UUID,
    SLOT_COUNT,
    Command,
    DeviceMode,
    DeviceModel,
    SenseType,
    Status,
    TagType,
)
from custom_components.chameleon_ultra.device import (
    ChameleonTimeoutError,
    ChameleonUltraDevice,
)
from custom_components.chameleon_ultra.protocol import (
    Frame,
    FrameAssembler,
    StatusError,
    build_frame,
)


# ---------------------------------------------------------------------------
# Mock ChameleonUltra device — the "other actor"
# ---------------------------------------------------------------------------


class MockChameleon:
    """Simulates a ChameleonUltra device at the protocol level.

    Receives raw BLE writes (NUS RX), parses them into protocol frames,
    processes commands against internal state, and delivers response frames
    as BLE notifications (NUS TX).  The test asserts on command_log to
    verify exactly what the integration sent.
    """

    def __init__(self) -> None:
        # Device state
        self.active_slot: int = 0
        self.device_mode: int = DeviceMode.EMULATOR
        self.battery_voltage: int = 4200
        self.battery_pct: int = 85
        self.firmware: str = "v2.0.0-test"
        self.chip_id: bytes = b"\xDE\xAD\xBE\xEF\xCA\xFE\xBA\xBE"
        self.model: int = DeviceModel.ULTRA
        self.app_version: tuple[int, int] = (2, 0)

        self.slot_types: list[dict[str, int]] = [
            {"hf_type": TagType.MF1_1K, "lf_type": 0} for _ in range(SLOT_COUNT)
        ]
        self.slot_enabled: list[dict[str, bool]] = [
            {"hf_enabled": i == 0, "lf_enabled": False} for i in range(SLOT_COUNT)
        ]
        self.slot_nicks: list[str] = [f"Slot {i + 1}" for i in range(SLOT_COUNT)]

        # 64 blocks × 16 bytes for MF Classic 1K
        self.block_data: bytearray = bytearray(64 * 16)
        self.anti_coll: dict[str, bytes | int] = {
            "uid": b"\xDE\xAD\xBE\xEF",
            "atqa": b"\x04\x00",
            "sak": 0x08,
            "ats": b"",
        }

        # Transport
        self._assembler = FrameAssembler()
        self._tx_callback: object = None
        self._tx_mtu: int = 20
        self.responsive: bool = True

        # Captures every command frame received — tests assert on this
        self.command_log: list[Frame] = []

    def set_tx_callback(self, callback: object) -> None:
        self._tx_callback = callback

    def feed_rx(self, data: bytes | bytearray) -> None:
        """Process bytes written to NUS RX (host → device)."""
        frames = self._assembler.feed(data)
        for frame in frames:
            self.command_log.append(frame)
            if not self.responsive:
                continue
            response = self._handle(frame)
            if response is not None:
                self._send_tx(response)

    def _send_tx(self, frame_bytes: bytes) -> None:
        """Deliver response as chunked NUS TX notifications."""
        assert self._tx_callback is not None
        for i in range(0, len(frame_bytes), self._tx_mtu):
            chunk = bytearray(frame_bytes[i : i + self._tx_mtu])
            self._tx_callback(NUS_TX_CHAR_UUID, chunk)

    def _handle(self, frame: Frame) -> bytes | None:
        cmd = frame.cmd
        data = frame.data

        # -- Device info -------------------------------------------------
        if cmd == Command.GET_APP_VERSION:
            return build_frame(cmd, bytes(self.app_version))

        if cmd == Command.GET_GIT_VERSION:
            return build_frame(cmd, self.firmware.encode("utf-8"))

        if cmd == Command.GET_DEVICE_MODEL:
            return build_frame(cmd, bytes([self.model]))

        if cmd == Command.GET_DEVICE_CHIP_ID:
            return build_frame(cmd, self.chip_id)

        if cmd == Command.GET_BATTERY_INFO:
            return build_frame(
                cmd, struct.pack("!HB", self.battery_voltage, self.battery_pct)
            )

        if cmd == Command.GET_DEVICE_MODE:
            return build_frame(cmd, bytes([self.device_mode]))

        if cmd == Command.CHANGE_DEVICE_MODE:
            self.device_mode = data[0]
            return build_frame(cmd)

        # -- Slots -------------------------------------------------------
        if cmd == Command.GET_ACTIVE_SLOT:
            return build_frame(cmd, bytes([self.active_slot]))

        if cmd == Command.SET_ACTIVE_SLOT:
            slot = data[0]
            if slot >= SLOT_COUNT:
                return build_frame(cmd, status=Status.PAR_ERR)
            self.active_slot = slot
            return build_frame(cmd)

        if cmd == Command.GET_SLOT_INFO:
            payload = b""
            for s in self.slot_types:
                payload += struct.pack("!HH", s["hf_type"], s["lf_type"])
            return build_frame(cmd, payload)

        if cmd == Command.GET_ENABLED_SLOTS:
            payload = b""
            for s in self.slot_enabled:
                payload += bytes([int(s["hf_enabled"]), int(s["lf_enabled"])])
            return build_frame(cmd, payload)

        if cmd == Command.SET_SLOT_ENABLE:
            slot, sense, enable = data[0], data[1], bool(data[2])
            if slot >= SLOT_COUNT:
                return build_frame(cmd, status=Status.PAR_ERR)
            key = "hf_enabled" if sense == SenseType.HF else "lf_enabled"
            self.slot_enabled[slot][key] = enable
            return build_frame(cmd)

        if cmd == Command.SET_SLOT_TAG_TYPE:
            slot = data[0]
            tag_type = struct.unpack("!H", data[1:3])[0]
            self.slot_types[slot]["hf_type"] = tag_type
            return build_frame(cmd)

        if cmd == Command.GET_SLOT_TAG_NICK:
            slot = data[0]
            return build_frame(cmd, self.slot_nicks[slot].encode("utf-8"))

        if cmd == Command.SET_SLOT_TAG_NICK:
            slot = data[0]
            # sense_type = data[1]
            self.slot_nicks[slot] = data[2:].decode("utf-8")
            return build_frame(cmd)

        if cmd == Command.SLOT_DATA_CONFIG_SAVE:
            return build_frame(cmd)

        if cmd == Command.SAVE_SETTINGS:
            return build_frame(cmd)

        # -- Emulation data ----------------------------------------------
        if cmd == Command.HF14A_GET_ANTI_COLL_DATA:
            ac = self.anti_coll
            uid = ac["uid"]
            atqa = ac["atqa"]
            sak = ac["sak"]
            ats = ac["ats"]
            payload = (
                bytes([len(uid)]) + uid + atqa + bytes([sak, len(ats)]) + ats
            )
            return build_frame(cmd, payload)

        if cmd == Command.HF14A_SET_ANTI_COLL_DATA:
            uid_len = data[0]
            uid = data[1 : 1 + uid_len]
            pos = 1 + uid_len
            atqa = data[pos : pos + 2]
            sak = data[pos + 2]
            ats_len = data[pos + 3]
            ats = data[pos + 4 : pos + 4 + ats_len]
            self.anti_coll = {
                "uid": bytes(uid),
                "atqa": bytes(atqa),
                "sak": sak,
                "ats": bytes(ats),
            }
            return build_frame(cmd)

        if cmd == Command.MF1_READ_EMU_BLOCK_DATA:
            start, count = data[0], data[1]
            offset = start * 16
            return build_frame(cmd, bytes(self.block_data[offset : offset + count * 16]))

        if cmd == Command.MF1_WRITE_EMU_BLOCK_DATA:
            start = data[0]
            block_bytes = data[1:]
            offset = start * 16
            self.block_data[offset : offset + len(block_bytes)] = block_bytes
            return build_frame(cmd)

        # Unknown command
        return build_frame(cmd, status=Status.INVALID_CMD)


# ---------------------------------------------------------------------------
# Mock BLE transport — thin shim that routes to MockChameleon
# ---------------------------------------------------------------------------


class MockBleakClient:
    """BleakClient replacement that routes NUS writes through MockChameleon."""

    def __init__(self, chameleon: MockChameleon) -> None:
        self._chameleon = chameleon
        self._connected = True
        self.mtu_size: int = 23
        self._notify_cbs: dict[str, object] = {}
        self.write_log: list[tuple[str, bytes]] = []

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def write_gatt_char(
        self, uuid: str, data: bytes | bytearray, response: bool = False
    ) -> None:
        self.write_log.append((uuid, bytes(data)))
        if uuid == NUS_RX_CHAR_UUID:
            self._chameleon.feed_rx(data)

    async def start_notify(self, uuid: str, callback: object) -> None:
        self._notify_cbs[uuid] = callback
        if uuid == NUS_TX_CHAR_UUID:
            self._chameleon.set_tx_callback(callback)

    async def stop_notify(self, uuid: str) -> None:
        self._notify_cbs.pop(uuid, None)

    async def disconnect(self) -> None:
        self._connected = False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def chameleon() -> MockChameleon:
    return MockChameleon()


@pytest.fixture
def ble_client(chameleon: MockChameleon) -> MockBleakClient:
    return MockBleakClient(chameleon)


@pytest.fixture
async def device(ble_client: MockBleakClient) -> ChameleonUltraDevice:
    dev = ChameleonUltraDevice(ble_client)
    await ble_client.start_notify(NUS_TX_CHAR_UUID, dev.on_notification)
    return dev


# ---------------------------------------------------------------------------
# Full poll cycle — exercises every query the coordinator makes
# ---------------------------------------------------------------------------


async def test_poll_cycle_reads_all_device_state(
    chameleon: MockChameleon, device: ChameleonUltraDevice
) -> None:
    """Simulate the coordinator's _async_update_data poll cycle.

    Sends every query the coordinator issues and verifies the parsed
    results match the mock device's state.
    """
    battery = await device.get_battery_info()
    assert battery == {"voltage": 4200, "percentage": 85}

    assert await device.get_active_slot() == 0
    assert await device.get_device_mode() == DeviceMode.EMULATOR
    assert await device.get_git_version() == "v2.0.0-test"

    slot_info = await device.get_slot_info()
    assert len(slot_info) == SLOT_COUNT
    assert slot_info[0]["hf_type"] == TagType.MF1_1K

    enabled = await device.get_enabled_slots()
    assert enabled[0]["hf_enabled"] is True
    assert enabled[1]["hf_enabled"] is False

    for i in range(SLOT_COUNT):
        nick = await device.get_slot_tag_nick(i, SenseType.HF)
        assert nick == f"Slot {i + 1}"

    # Verify the mock saw every command
    cmds = {f.cmd for f in chameleon.command_log}
    assert cmds >= {
        Command.GET_BATTERY_INFO,
        Command.GET_ACTIVE_SLOT,
        Command.GET_DEVICE_MODE,
        Command.GET_GIT_VERSION,
        Command.GET_SLOT_INFO,
        Command.GET_ENABLED_SLOTS,
        Command.GET_SLOT_TAG_NICK,
    }


# ---------------------------------------------------------------------------
# Unlock button — enable HF → hold → disable HF (atomic under lock)
# ---------------------------------------------------------------------------


async def test_unlock_sequence(
    chameleon: MockChameleon, device: ChameleonUltraDevice
) -> None:
    """The unlock button enables HF on the active slot, holds, then disables.

    Verify the exact command sequence and that device state transitions
    correctly.
    """
    assert chameleon.slot_enabled[0]["hf_enabled"] is True  # starts enabled

    # Simulate what button.py does: atomic enable → sleep → disable
    async with device._command_lock:
        await device._send_locked(
            Command.SET_SLOT_ENABLE, bytes([0, SenseType.HF, 1])
        )
        await asyncio.sleep(0.01)  # shortened hold for test
        await device._send_locked(
            Command.SET_SLOT_ENABLE, bytes([0, SenseType.HF, 0])
        )

    # Verify command sequence is exactly enable then disable
    enable_disable = [
        (f.cmd, f.data) for f in chameleon.command_log
        if f.cmd == Command.SET_SLOT_ENABLE
    ]
    assert enable_disable == [
        (Command.SET_SLOT_ENABLE, bytes([0, SenseType.HF, 1])),
        (Command.SET_SLOT_ENABLE, bytes([0, SenseType.HF, 0])),
    ]

    # Device state: HF disabled after sequence
    assert chameleon.slot_enabled[0]["hf_enabled"] is False


# ---------------------------------------------------------------------------
# Slot selection
# ---------------------------------------------------------------------------


async def test_slot_select_persists(
    chameleon: MockChameleon, device: ChameleonUltraDevice
) -> None:
    """Selecting a slot changes active slot and saves config to flash."""
    assert chameleon.active_slot == 0

    await device.set_active_slot(3)
    await device.save_slot_config()

    assert chameleon.active_slot == 3

    cmds = [f.cmd for f in chameleon.command_log]
    assert cmds == [Command.SET_ACTIVE_SLOT, Command.SLOT_DATA_CONFIG_SAVE]


async def test_slot_select_out_of_range(
    chameleon: MockChameleon, device: ChameleonUltraDevice
) -> None:
    """Selecting slot >= 8 returns PAR_ERR status."""
    with pytest.raises(StatusError) as exc_info:
        await device.set_active_slot(99)
    assert exc_info.value.status == Status.PAR_ERR
    assert chameleon.active_slot == 0  # unchanged


# ---------------------------------------------------------------------------
# Slot toggle (switch entity)
# ---------------------------------------------------------------------------


async def test_slot_toggle_on_off(
    chameleon: MockChameleon, device: ChameleonUltraDevice
) -> None:
    """Toggle HF emulation for a slot that starts disabled."""
    assert chameleon.slot_enabled[3]["hf_enabled"] is False

    await device.set_slot_enable(3, SenseType.HF, True)
    assert chameleon.slot_enabled[3]["hf_enabled"] is True

    await device.set_slot_enable(3, SenseType.HF, False)
    assert chameleon.slot_enabled[3]["hf_enabled"] is False


# ---------------------------------------------------------------------------
# Block data upload — simulates load_dump service
# ---------------------------------------------------------------------------


async def test_load_dump_writes_blocks_and_anti_coll(
    chameleon: MockChameleon, device: ChameleonUltraDevice
) -> None:
    """Simulate the load_dump service: set slot, tag type, anti-coll, blocks."""
    # Fake 1K dump: 64 blocks × 16 bytes
    dump = bytes(range(256)) * 4  # 1024 bytes, recognizable pattern

    # Steps mirroring __init__.py's load_dump service
    await device.set_active_slot(2)
    await device.set_slot_tag_type(2, TagType.MF1_1K)

    uid = dump[0:4]
    sak = dump[5]
    atqa = dump[6:8]
    await device.set_anti_coll_data(uid, atqa, sak)

    # Upload in 31-block chunks (max 496 bytes per write)
    block_size = 16
    chunk_blocks = 31
    for start in range(0, 64, chunk_blocks):
        end = min(start + chunk_blocks, 64)
        chunk = dump[start * block_size : end * block_size]
        await device.write_emu_block_data(start, chunk)

    await device.set_slot_enable(2, SenseType.HF, True)
    await device.save_slot_config()

    # Verify device state
    assert chameleon.active_slot == 2
    assert chameleon.slot_types[2]["hf_type"] == TagType.MF1_1K
    assert chameleon.anti_coll["uid"] == dump[0:4]
    assert chameleon.anti_coll["sak"] == dump[5]
    assert chameleon.slot_enabled[2]["hf_enabled"] is True

    # Verify block data was written correctly
    assert bytes(chameleon.block_data[:1024]) == dump


# ---------------------------------------------------------------------------
# Anti-collision data round-trip
# ---------------------------------------------------------------------------


async def test_anti_coll_round_trip(
    chameleon: MockChameleon, device: ChameleonUltraDevice
) -> None:
    """Write and read-back anti-collision data including ATS."""
    uid = b"\x01\x02\x03\x04\x05\x06\x07"
    atqa = b"\x44\x00"
    sak = 0x20
    ats = b"\x75\x77\x80\x02"

    await device.set_anti_coll_data(uid, atqa, sak, ats)
    result = await device.get_anti_coll_data()

    assert result["uid"] == uid
    assert result["atqa"] == atqa
    assert result["sak"] == sak
    assert result["ats"] == ats


# ---------------------------------------------------------------------------
# Timeout — device goes silent
# ---------------------------------------------------------------------------


async def test_command_timeout_when_device_unresponsive(
    chameleon: MockChameleon, device: ChameleonUltraDevice
) -> None:
    """When the device stops responding, commands raise ChameleonTimeoutError."""
    chameleon.responsive = False

    with pytest.raises(ChameleonTimeoutError):
        await device.send_command(Command.GET_BATTERY_INFO, timeout=0.05)

    # The command was still sent — mock saw it
    assert chameleon.command_log[-1].cmd == Command.GET_BATTERY_INFO


# ---------------------------------------------------------------------------
# Concurrent command serialization
# ---------------------------------------------------------------------------


async def test_concurrent_commands_serialized(
    chameleon: MockChameleon, device: ChameleonUltraDevice
) -> None:
    """Multiple concurrent commands are serialized — never interleaved."""
    results = await asyncio.gather(
        device.get_battery_info(),
        device.get_active_slot(),
        device.get_device_mode(),
    )

    assert results[0] == {"voltage": 4200, "percentage": 85}
    assert results[1] == 0
    assert results[2] == DeviceMode.EMULATOR

    # Commands arrived one at a time (assembler resets between commands)
    cmds = [f.cmd for f in chameleon.command_log]
    assert len(cmds) == 3
    assert set(cmds) == {
        Command.GET_BATTERY_INFO,
        Command.GET_ACTIVE_SLOT,
        Command.GET_DEVICE_MODE,
    }


# ---------------------------------------------------------------------------
# Chunked response — response split across multiple BLE notifications
# ---------------------------------------------------------------------------


async def test_chunked_response_reassembly(
    chameleon: MockChameleon, ble_client: MockBleakClient
) -> None:
    """Response split into tiny BLE notifications is reassembled correctly."""
    # Force tiny MTU so responses are heavily chunked
    chameleon._tx_mtu = 5
    ble_client.mtu_size = 8  # host side also small

    dev = ChameleonUltraDevice(ble_client)
    await ble_client.start_notify(NUS_TX_CHAR_UUID, dev.on_notification)

    version = await dev.get_git_version()
    assert version == "v2.0.0-test"


# ---------------------------------------------------------------------------
# D-Bus pairing agent — would have caught the from __future__ annotations bug
# ---------------------------------------------------------------------------


async def test_pin_agent_instantiation_and_introspection() -> None:
    """Instantiate _PinAgent with real dbus_fast and verify D-Bus signatures.

    Regression: adding `from __future__ import annotations` to pairing.py
    broke dbus_fast 3.x's parse_annotation() on `-> None` return types,
    crashing the entire integration on import.  This test catches that by
    constructing the agent (which triggers @method() annotation parsing)
    and verifying the resulting D-Bus introspection.
    """
    from custom_components.chameleon_ultra.pairing import PinAgent as _PinAgent

    agent = _PinAgent(123456)
    introspection = agent.introspect()
    methods = {m.name: m for m in introspection.methods}

    # All Agent1 methods must be present
    assert set(methods) >= {
        "Release",
        "RequestPasskey",
        "DisplayPasskey",
        "RequestConfirmation",
        "AuthorizeService",
        "Cancel",
    }

    # RequestPasskey: takes object path, returns uint32
    rp = methods["RequestPasskey"]
    assert len(rp.in_args) == 1
    assert rp.in_args[0].signature == "o"
    assert len(rp.out_args) == 1
    assert rp.out_args[0].signature == "u"

    # Release: no args, void return
    rel = methods["Release"]
    assert len(rel.in_args) == 0
    assert len(rel.out_args) == 0

    # DisplayPasskey: (object_path, uint32, uint16) → void
    dp = methods["DisplayPasskey"]
    assert len(dp.in_args) == 3
    assert [a.signature for a in dp.in_args] == ["o", "u", "q"]
    assert len(dp.out_args) == 0


async def test_pin_agent_returns_configured_passkey() -> None:
    """RequestPasskey method returns the PIN the agent was constructed with."""
    from custom_components.chameleon_ultra.pairing import PinAgent as _PinAgent

    agent = _PinAgent(654321)

    # Call the underlying function directly (unwrap @method decorator)
    fn = agent.RequestPasskey
    if hasattr(fn, "__wrapped__"):
        result = fn.__wrapped__(agent, "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF")
    else:
        result = fn("/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF")

    assert result == 654321


# ---------------------------------------------------------------------------
# Device mode switch
# ---------------------------------------------------------------------------


async def test_change_device_mode(
    chameleon: MockChameleon, device: ChameleonUltraDevice
) -> None:
    """Switch between emulator and reader mode."""
    assert await device.get_device_mode() == DeviceMode.EMULATOR

    await device.change_device_mode(DeviceMode.READER)
    assert chameleon.device_mode == DeviceMode.READER
    assert await device.get_device_mode() == DeviceMode.READER


# ---------------------------------------------------------------------------
# Slot nickname persistence
# ---------------------------------------------------------------------------


async def test_set_and_get_slot_nick(
    chameleon: MockChameleon, device: ChameleonUltraDevice
) -> None:
    """Write a slot nickname and read it back."""
    await device.set_slot_tag_nick(5, SenseType.HF, "Front Door")
    assert chameleon.slot_nicks[5] == "Front Door"

    nick = await device.get_slot_tag_nick(5, SenseType.HF)
    assert nick == "Front Door"
