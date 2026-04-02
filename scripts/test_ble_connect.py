#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "bleak>=3.0",
#     "dbus-fast>=3.1,<4",
# ]
# ///
"""Standalone BLE connection test for ChameleonUltra.

Proves the full connection flow works outside HA:
  1. Scan for the device
  2. Register a BlueZ pairing agent with the configured PIN
  3. Connect via bleak
  4. Send GET_APP_VERSION over NUS
  5. Parse the protocol response
  6. Disconnect cleanly

Usage:
    sudo ./scripts/test_ble_connect.py
    sudo ./scripts/test_ble_connect.py --address FE:6A:52:FA:98:62 --pin 123456
"""

import argparse
import asyncio
import struct
import sys

from bleak import BleakClient, BleakScanner
from dbus_fast import Variant
from dbus_fast.aio import MessageBus
from dbus_fast.constants import BusType
from dbus_fast.service import ServiceInterface, method

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

    @method()
    def Release(self) -> None:
        pass

    @method()
    def RequestPasskey(self, device: "o") -> "u":
        print(f"  [agent] Providing passkey {self._pin} for {device}")
        return self._pin

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q") -> None:
        print(f"  [agent] DisplayPasskey: {passkey} (entered: {entered})")

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u") -> None:
        print(f"  [agent] Confirming passkey {passkey}")

    @method()
    def AuthorizeService(self, device: "o", uuid: "s") -> None:
        pass

    @method()
    def Cancel(self) -> None:
        print("  [agent] Pairing cancelled")


async def register_agent(bus: MessageBus, pin: int) -> PinAgent:
    """Register a BlueZ pairing agent on the system bus."""
    agent = PinAgent(pin)
    bus.export(AGENT_PATH, agent)

    introspection = await bus.introspect("org.bluez", "/org/bluez")
    obj = bus.get_proxy_object("org.bluez", "/org/bluez", introspection)
    mgr = obj.get_interface("org.bluez.AgentManager1")
    await mgr.call_register_agent(AGENT_PATH, "KeyboardDisplay")
    await mgr.call_request_default_agent(AGENT_PATH)
    print(f"  [agent] Registered pairing agent (PIN: {pin})")
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


async def ensure_paired(bus: MessageBus, address: str, adapter: str = "hci1") -> None:
    """Check if paired; if not, initiate pairing (agent must be registered)."""
    dev_path = f"/org/bluez/{adapter}/dev_{address.replace(':', '_')}"
    try:
        intro = await bus.introspect("org.bluez", dev_path)
    except Exception:
        print(f"  [pair] Device {address} not known to BlueZ yet — skipping pair check")
        return

    obj = bus.get_proxy_object("org.bluez", dev_path, intro)
    props = obj.get_interface("org.freedesktop.DBus.Properties")
    paired = await props.call_get("org.bluez.Device1", "Paired")

    if paired.value:
        print(f"  [pair] Already paired with {address}")
        return

    print(f"  [pair] Not paired — initiating pairing with {address}...")
    device = obj.get_interface("org.bluez.Device1")
    await device.call_pair()

    await props.call_set("org.bluez.Device1", "Trusted", Variant("b", True))
    print(f"  [pair] Successfully paired and trusted {address}")


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
    parser.add_argument("--skip-pair", action="store_true", help="Skip pairing step")
    args = parser.parse_args()

    address = args.address
    pin = int(args.pin)

    print(f"=== ChameleonUltra BLE Connection Test ===")
    print(f"Address: {address}")
    print(f"PIN: {pin}")
    print()

    # Step 1: Detect adapter
    print("[1/6] Detecting BlueZ adapter...")
    adapter = await find_adapter()
    print(f"  Using adapter: {adapter}")

    # Step 2: Scan
    print(f"\n[2/6] Scanning for device...")
    device = await BleakScanner.find_device_by_address(address, timeout=10.0)
    if device is None:
        print(f"  FAIL: Device {address} not found")
        sys.exit(1)
    print(f"  Found: {device.name} ({device.address})")

    # Step 3: Register pairing agent (pairing triggered automatically during GATT)
    bus = None
    if not args.skip_pair:
        print(f"\n[3/6] Registering pairing agent...")
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        await register_agent(bus, pin)

        # Pre-pair via D-Bus if device is already known but not paired
        print(f"\n[4/6] Checking pairing (adapter={adapter})...")
        try:
            await ensure_paired(bus, address, adapter=adapter)
        except Exception as e:
            print(f"  Pairing via D-Bus didn't work: {e}")
            print("  Agent is registered — pairing should trigger during GATT connect")
    else:
        print("\n[3/6] Skipping pairing agent (--skip-pair)")
        print("\n[4/6] Skipping pairing check")

    # Step 5: Connect and send commands
    print("\n[5/6] Connecting via GATT...")
    try:
        async with BleakClient(device, timeout=15.0) as client:
            print(f"  Connected! MTU: {client.mtu_size}")

            # GET_APP_VERSION
            cmd, status, data = await send_command(client, CMD_GET_APP_VERSION)
            if status == 0 and len(data) >= 2:
                print(f"  App version: {data[0]}.{data[1]}")
            else:
                print(f"  GET_APP_VERSION: status={status:#06x} data={data.hex()}")

            # GET_DEVICE_MODEL
            cmd, status, data = await send_command(client, CMD_GET_DEVICE_MODEL)
            if status == 0 and data:
                model = "Ultra" if data[0] == 0 else "Lite"
                print(f"  Model: Chameleon{model}")

            # GET_GIT_VERSION
            cmd, status, data = await send_command(client, CMD_GET_GIT_VERSION)
            if status == 0:
                print(f"  Firmware: {data.decode('utf-8', errors='replace')}")

            # GET_BATTERY_INFO
            cmd, status, data = await send_command(client, CMD_GET_BATTERY_INFO)
            if status == 0 and len(data) >= 3:
                voltage = struct.unpack("!H", data[0:2])[0]
                pct = data[2]
                print(f"  Battery: {pct}% ({voltage}mV)")

            print("\n[6/6] All commands succeeded!")

    except Exception as e:
        print(f"  FAIL: {e}")
        sys.exit(1)
    finally:
        if bus:
            await unregister_agent(bus)
            bus.disconnect()

    print("\n=== SUCCESS ===")


if __name__ == "__main__":
    asyncio.run(main())
