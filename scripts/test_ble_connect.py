#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "bleak>=3.0",
#     "dbus-fast>=3.1,<4",
# ]
# ///
"""Standalone BLE connection test for ChameleonUltra.

Tests the full connection flow outside HA:
  1. Scan for the device
  2. Register a BlueZ pairing agent with the configured PIN
  3. Connect via bleak with pair=True (bleak calls Pair() internally)
  4. Send GET_APP_VERSION over NUS
  5. Parse the protocol response
  6. Disconnect cleanly

Usage:
    sudo ./scripts/test_ble_connect.py
    sudo ./scripts/test_ble_connect.py --address FE:6A:52:FA:98:62 --pin 123456
"""

import argparse
import asyncio
import logging
import struct
import sys
import time

from bleak import BleakClient, BleakScanner
from dbus_fast.aio import MessageBus
from dbus_fast.constants import BusType
from dbus_fast.service import ServiceInterface, method

# Crank logging to maximum on everything
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s.%(msecs)03d %(name)-40s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
)
for mod in [
    "dbus_fast", "dbus_fast.message_bus", "dbus_fast.aio",
    "dbus_fast.aio.message_bus", "dbus_fast.aio.message_reader",
    "dbus_fast.service", "dbus_fast.proxy_object",
    "bleak", "bleak.backends.bluezdbus",
    "bleak.backends.bluezdbus.manager",
    "bleak.backends.bluezdbus.client",
    "bleak.backends.bluezdbus.scanner",
]:
    logging.getLogger(mod).setLevel(logging.DEBUG)

# -- Protocol constants (mirrored from const.py) --

NUS_RX_CHAR_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # host → device
NUS_TX_CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # device → host
SOF = 0x11
CMD_GET_APP_VERSION = 1000
CMD_GET_DEVICE_MODEL = 1033
CMD_GET_BATTERY_INFO = 1025
CMD_GET_GIT_VERSION = 1017

AGENT_PATH = "/org/test/chameleon_agent"


async def find_adapter() -> str:
    """Auto-detect the BlueZ adapter name (hci0, hci1, etc.)."""
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        intro = await bus.introspect("org.bluez", "/org/bluez")
        for node in intro.nodes:
            if node.name and node.name.startswith("hci"):
                return node.name
    finally:
        bus.disconnect()
    raise RuntimeError("No BlueZ adapter found")


def lrc(data: bytes | bytearray) -> int:
    return (~sum(data) + 1) & 0xFF


def build_frame(cmd: int, data: bytes = b"", status: int = 0x0000) -> bytes:
    header = struct.pack("!HHH", cmd, status, len(data))
    preamble = bytes([SOF, 0xEF]) + header
    lrc2 = lrc(preamble)
    lrc3 = lrc(data) if data else 0x00
    return preamble + bytes([lrc2]) + data + bytes([lrc3])


def parse_frame(raw: bytes) -> tuple[int, int, bytes] | None:
    """Parse a raw frame, return (cmd, status, data) or None."""
    if len(raw) < 10 or raw[0] != SOF:
        return None
    cmd = struct.unpack("!H", raw[2:4])[0]
    status = struct.unpack("!H", raw[4:6])[0]
    data_len = struct.unpack("!H", raw[6:8])[0]
    data = raw[9 : 9 + data_len]
    return cmd, status, bytes(data)


# -- BlueZ pairing agent --


class PinAgent(ServiceInterface):
    """BlueZ Agent1 that auto-responds with a static passkey."""

    def __init__(self, pin: int) -> None:
        super().__init__("org.bluez.Agent1")
        self._pin = pin
        self._t0 = time.monotonic()

    def _ts(self) -> str:
        return f"t+{time.monotonic() - self._t0:.3f}s"

    @method()
    def Release(self) -> None:
        print(f"  [agent {self._ts()}] Release called")

    @method()
    def RequestPasskey(self, device: "o") -> "u":
        print(f"  [agent {self._ts()}] >>> RequestPasskey ENTER for {device}")
        print(f"  [agent {self._ts()}] >>> Returning passkey {self._pin} (type={type(self._pin).__name__})")
        return self._pin

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q") -> None:
        print(f"  [agent {self._ts()}] DisplayPasskey: passkey={passkey} entered={entered} device={device}")

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u") -> None:
        print(f"  [agent {self._ts()}] RequestConfirmation: passkey={passkey} device={device}")

    @method()
    def AuthorizeService(self, device: "o", uuid: "s") -> None:
        print(f"  [agent {self._ts()}] AuthorizeService: uuid={uuid} device={device}")

    @method()
    def Cancel(self) -> None:
        print(f"  [agent {self._ts()}] !!! Cancel called — pairing was canceled by BlueZ")


async def register_agent(bus: MessageBus, pin: int) -> PinAgent:
    """Register a BlueZ pairing agent on the system bus."""
    agent = PinAgent(pin)
    print(f"  [register] Exporting agent at {AGENT_PATH}")
    bus.export(AGENT_PATH, agent)

    print(f"  [register] Introspecting org.bluez /org/bluez")
    introspection = await bus.introspect("org.bluez", "/org/bluez")
    obj = bus.get_proxy_object("org.bluez", "/org/bluez", introspection)
    mgr = obj.get_interface("org.bluez.AgentManager1")
    print(f"  [register] Calling RegisterAgent({AGENT_PATH}, KeyboardDisplay)")
    await mgr.call_register_agent(AGENT_PATH, "KeyboardDisplay")
    print(f"  [register] Calling RequestDefaultAgent({AGENT_PATH})")
    await mgr.call_request_default_agent(AGENT_PATH)
    print(f"  [register] Agent registered and set as default (PIN: {pin})")
    return agent


async def unregister_agent(bus: MessageBus) -> None:
    try:
        introspection = await bus.introspect("org.bluez", "/org/bluez")
        obj = bus.get_proxy_object("org.bluez", "/org/bluez", introspection)
        mgr = obj.get_interface("org.bluez.AgentManager1")
        await mgr.call_unregister_agent(AGENT_PATH)
        print("  [agent] Unregistered pairing agent")
    except Exception:
        pass


async def send_command(
    client: BleakClient, cmd: int, data: bytes = b"", timeout: float = 5.0
) -> tuple[int, int, bytes]:
    """Send a protocol command and wait for the response frame."""
    response_future: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
    rx_buf = bytearray()

    def on_notify(_sender, notify_data: bytearray) -> None:
        rx_buf.extend(notify_data)
        # Check if we have a complete frame
        if len(rx_buf) >= 10:
            data_len = struct.unpack("!H", rx_buf[6:8])[0]
            expected = 10 + data_len
            if len(rx_buf) >= expected and not response_future.done():
                response_future.set_result(bytes(rx_buf[:expected]))

    await client.start_notify(NUS_TX_CHAR_UUID, on_notify)
    try:
        frame = build_frame(cmd, data)
        mtu = max(client.mtu_size - 3, 20)
        for i in range(0, len(frame), mtu):
            await client.write_gatt_char(NUS_RX_CHAR_UUID, frame[i : i + mtu], response=False)

        raw = await asyncio.wait_for(response_future, timeout=timeout)
        result = parse_frame(raw)
        if result is None:
            raise RuntimeError(f"Failed to parse response: {raw.hex()}")
        return result
    finally:
        await client.stop_notify(NUS_TX_CHAR_UUID)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Test BLE connection to ChameleonUltra")
    parser.add_argument("--address", "-a", default="FE:6A:52:FA:98:62")
    parser.add_argument("--pin", "-p", default="123456")
    parser.add_argument("--skip-pair", action="store_true", help="Skip pairing (use stored bond)")
    args = parser.parse_args()

    address = args.address
    pin = int(args.pin)

    print(f"=== ChameleonUltra BLE Connection Test ===")
    print(f"Address: {address}")
    print(f"PIN: {pin}")
    print()

    # Step 1: Detect adapter
    t0 = time.monotonic()
    def ts() -> str:
        return f"t+{time.monotonic() - t0:.3f}s"

    print(f"[1/5] {ts()} Detecting BlueZ adapter...")
    adapter = await find_adapter()
    print(f"  {ts()} Using adapter: {adapter}")

    # Step 2: Scan
    print(f"\n[2/5] {ts()} Scanning for device...")
    device = await BleakScanner.find_device_by_address(address, timeout=10.0)
    if device is None:
        print(f"  {ts()} FAIL: Device {address} not found (is it awake?)")
        sys.exit(1)
    print(f"  {ts()} Found: {device.name} ({device.address})")

    # Step 3: Register pairing agent FIRST — must be ready before Pair() triggers
    print(f"\n[3/5] {ts()} Connecting to system D-Bus for agent...")
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    print(f"  {ts()} D-Bus connected, unique name: {bus.unique_name}")
    print(f"  {ts()} Registering pairing agent...")
    await register_agent(bus, pin)
    print(f"  {ts()} Agent ready, waiting 0.5s for BlueZ to register it...")
    await asyncio.sleep(0.5)  # let BlueZ fully process the agent registration

    # Step 4: Connect — bleak calls Pair() instead of Connect() when pair=True
    needs_pair = not args.skip_pair
    print(f"\n[4/5] {ts()} Connecting via GATT (pair={needs_pair})...")
    print(f"  {ts()} BleakClient.__init__ with timeout=30, pair={needs_pair}")
    try:
        async with BleakClient(device, timeout=30.0, pair=needs_pair) as client:

            print(f"  {ts()} Connected! MTU: {client.mtu_size}")
            print(f"  {ts()} is_connected={client.is_connected}")

            # GET_APP_VERSION
            cmd, status, data = await send_command(client, CMD_GET_APP_VERSION)
            if len(data) >= 2:
                print(f"  App version: {data[0]}.{data[1]}")
            else:
                print(f"  GET_APP_VERSION: status={status:#06x} data={data.hex()}")

            # GET_DEVICE_MODEL
            cmd, status, data = await send_command(client, CMD_GET_DEVICE_MODEL)
            if data:
                model = "Ultra" if data[0] == 0 else "Lite"
                print(f"  Model: Chameleon{model}")

            # GET_GIT_VERSION
            cmd, status, data = await send_command(client, CMD_GET_GIT_VERSION)
            print(f"  Firmware: {data.decode('utf-8', errors='replace')}")

            # GET_BATTERY_INFO
            cmd, status, data = await send_command(client, CMD_GET_BATTERY_INFO)
            if len(data) >= 3:
                voltage = struct.unpack("!H", data[0:2])[0]
                pct = data[2]
                print(f"  Battery: {pct}% ({voltage}mV)")

            print(f"\n[5/5] All commands succeeded!")

    except Exception as e:
        print(f"\n  {ts()} FAIL: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        print(f"  {ts()} Cleaning up agent...")
        await unregister_agent(bus)
        bus.disconnect()
        print(f"  {ts()} Done")

    print("\n=== SUCCESS ===")


if __name__ == "__main__":
    asyncio.run(main())
