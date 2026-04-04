#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "dbus-fast>=3.1,<4",
# ]
# ///
"""Minimal pairing test using ONLY dbus_fast — no bleak at all.

Isolates whether the pairing failure is in dbus_fast itself
or in bleak's D-Bus activity interfering with the SMP exchange.

Usage:
    sudo ./scripts/test_pair_dbus_only.py
    sudo ./scripts/test_pair_dbus_only.py --address FE:6A:52:FA:98:62 --pin 123456
"""

import argparse
import asyncio
import logging
import time

from dbus_fast import Variant
from dbus_fast.aio import MessageBus
from dbus_fast.constants import BusType
from dbus_fast.service import ServiceInterface, method

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s.%(msecs)03d %(name)-40s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("dbus_fast").setLevel(logging.DEBUG)

AGENT_PATH = "/org/test/chameleon_agent"
t0 = time.monotonic()

def ts() -> str:
    return f"t+{time.monotonic() - t0:.3f}s"


class PinAgent(ServiceInterface):
    def __init__(self, pin: int) -> None:
        super().__init__("org.bluez.Agent1")
        self._pin = pin

    @method()
    def Release(self) -> None:
        print(f"  [{ts()}] Agent.Release called")

    @method()
    def RequestPasskey(self, device: "o") -> "u":
        print(f"  [{ts()}] >>> Agent.RequestPasskey for {device}")
        print(f"  [{ts()}] >>> Returning {self._pin}")
        return self._pin

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q") -> None:
        print(f"  [{ts()}] Agent.DisplayPasskey: {passkey} entered={entered}")

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u") -> None:
        print(f"  [{ts()}] Agent.RequestConfirmation: {passkey}")

    @method()
    def AuthorizeService(self, device: "o", uuid: "s") -> None:
        print(f"  [{ts()}] Agent.AuthorizeService: {uuid}")

    @method()
    def Cancel(self) -> None:
        print(f"  [{ts()}] !!! Agent.Cancel called")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--address", "-a", default="FE:6A:52:FA:98:62")
    parser.add_argument("--pin", "-p", default="123456")
    args = parser.parse_args()

    address = args.address
    pin = int(args.pin)

    print(f"=== Minimal D-Bus-only pairing test ===")
    print(f"Address: {address}, PIN: {pin}")
    print(f"NO bleak, NO scanning, NO BlueZManager")
    print()

    # Single D-Bus connection for everything
    print(f"[{ts()}] Connecting to system D-Bus...")
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    print(f"[{ts()}] Connected: {bus.unique_name}")

    try:
        # Step 1: Find adapter
        print(f"\n[{ts()}] Finding adapter...")
        intro = await bus.introspect("org.bluez", "/org/bluez")
        adapter = None
        for node in intro.nodes:
            if node.name and node.name.startswith("hci"):
                adapter = node.name
                break
        if not adapter:
            print("No adapter found")
            return
        print(f"[{ts()}] Using adapter: {adapter}")

        # Step 2: Register agent
        print(f"\n[{ts()}] Registering agent...")
        agent = PinAgent(pin)
        bus.export(AGENT_PATH, agent)
        bluez_obj = bus.get_proxy_object("org.bluez", "/org/bluez", intro)
        agent_mgr = bluez_obj.get_interface("org.bluez.AgentManager1")
        await agent_mgr.call_register_agent(AGENT_PATH, "KeyboardDisplay")
        await agent_mgr.call_request_default_agent(AGENT_PATH)
        print(f"[{ts()}] Agent registered and set as default")

        # Step 3: Wait a bit for BlueZ to fully process
        print(f"\n[{ts()}] Waiting 1s for agent to settle...")
        await asyncio.sleep(1.0)

        # Step 4: Check if device is known to BlueZ
        dev_path = f"/org/bluez/{adapter}/dev_{address.replace(':', '_')}"
        print(f"\n[{ts()}] Checking device at {dev_path}...")
        try:
            dev_intro = await bus.introspect("org.bluez", dev_path)
            dev_obj = bus.get_proxy_object("org.bluez", dev_path, dev_intro)
            props = dev_obj.get_interface("org.freedesktop.DBus.Properties")
            paired = await props.call_get("org.bluez.Device1", "Paired")
            connected = await props.call_get("org.bluez.Device1", "Connected")
            print(f"[{ts()}] Device known: Paired={paired.value}, Connected={connected.value}")
        except Exception as e:
            print(f"[{ts()}] Device not known to BlueZ: {e}")
            print(f"[{ts()}] Need to scan first. Run: sudo bluetoothctl scan on")
            return

        # Step 5: Call Pair() directly
        print(f"\n[{ts()}] Calling Device1.Pair()...")
        device_iface = dev_obj.get_interface("org.bluez.Device1")
        try:
            await device_iface.call_pair()
            print(f"[{ts()}] Pair() returned successfully!")
        except Exception as e:
            print(f"[{ts()}] Pair() FAILED: {type(e).__name__}: {e}")
            return

        # Step 6: Check result
        paired = await props.call_get("org.bluez.Device1", "Paired")
        connected = await props.call_get("org.bluez.Device1", "Connected")
        print(f"\n[{ts()}] Result: Paired={paired.value}, Connected={connected.value}")

        if paired.value:
            print(f"\n[{ts()}] Setting Trusted=True...")
            await props.call_set("org.bluez.Device1", "Trusted", Variant("b", True))
            print(f"[{ts()}] PAIRING SUCCEEDED!")
        else:
            print(f"[{ts()}] Pairing did not complete")

    finally:
        try:
            await agent_mgr.call_unregister_agent(AGENT_PATH)
        except Exception:
            pass
        bus.disconnect()
        print(f"\n[{ts()}] Done")


if __name__ == "__main__":
    asyncio.run(main())
