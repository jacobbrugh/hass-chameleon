#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "bleak>=3.0",
#     "dbus-fast>=3.1,<4",
# ]
# ///
"""Compare bluetooth signal strength for adapter tuning"""

import argparse
import asyncio

from bleak import BleakScanner


async def scan(address: str, adapter: str, duration: float = 10) -> list[int]:
    rssi_samples: list[int] = []

    def callback(device, adv_data):
        if device.address.upper() == address.upper():
            rssi_samples.append(adv_data.rssi)

    scanner = BleakScanner(detection_callback=callback, adapter=adapter)
    await scanner.start()
    await asyncio.sleep(duration)
    await scanner.stop()
    return rssi_samples


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("address", help="BLE device MAC address to scan for (e.g. FE:6A:52:FA:98:62)")
    parser.add_argument("--adapter", default="hci0", help="HCI adapter to use (default: hci0)")
    parser.add_argument("--duration", type=float, default=10, help="Scan duration in seconds (default: 10)")
    args = parser.parse_args()

    samples = asyncio.run(scan(args.address, args.adapter, args.duration))
    if not samples:
        print("No advertisements received from device.")
        raise SystemExit(1)
    print(f"n={len(samples)} mean={sum(samples)/len(samples):.1f} min={min(samples)} max={max(samples)}")


main()
