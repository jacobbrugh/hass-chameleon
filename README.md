# ChameleonUltra NFC Emulator for Home Assistant

A [HACS](https://hacs.xyz) custom integration that controls a [ChameleonUltra](https://github.com/RfidResearchGroup/ChameleonUltra) NFC emulator device over Bluetooth Low Energy.

## Features

- **Unlock button** — One-shot NFC emulation trigger: enables HF emulation for a configurable duration, then automatically disables it
- **Active slot selector** — Choose which of 8 emulation slots is active (1-8, matching the official GUI)
- **Per-slot HF enable switches** — Manual enable/disable for each slot's HF emulation
- **Battery sensor** — Battery percentage monitoring
- **Firmware sensor** — Firmware version display
- **BLE connectivity sensor** — Connection status tracking
- **Device mode sensor** — Emulator vs reader mode indicator
- **Activity events** — Events fired on unlock, slot change, etc.
- **Load dump service** — Upload .mfd, .bin, or .nfc (Flipper Zero) card dumps to emulation slots

## Requirements

- Home Assistant 2024.1.0+
- ChameleonUltra or ChameleonLite device with firmware v2.0+
- Bluetooth adapter accessible to Home Assistant (e.g., Intel AX210, or ESPHome Bluetooth Proxy)

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click the three dots menu -> Custom repositories
3. Add `https://github.com/jacobbrugh/hass-chameleon` as an Integration
4. Search for "ChameleonUltra" and install
5. Restart Home Assistant

### Manual

Copy `custom_components/chameleon_ultra/` to your Home Assistant `config/custom_components/` directory.

## Setup

1. Power on the ChameleonUltra and ensure it's in Bluetooth range
2. Home Assistant should auto-discover the device, or go to Settings -> Devices & Services -> Add Integration -> ChameleonUltra
3. On first connection, hold the **A button** on the ChameleonUltra to authorize BLE pairing

## Configuration

After setup, go to the integration options to configure:

- **Emulation hold time** — How long the Unlock button keeps emulation active (default: 3 seconds)

## Connection Model

The integration uses a battery-friendly **connect-on-demand** model:

- Connects to the device only when sending commands (polls, unlock, etc.)
- After each interaction, keeps the connection for 30 seconds to handle rapid retries
- Then disconnects, allowing the device to return to low-power sleep mode
- Unlock latency is typically 1-2.5 seconds (BLE connect + command round-trip)

## Services

### `chameleon_ultra.load_dump`

Load an NFC card dump file into an emulation slot.

| Field | Description |
|---|---|
| `device_id` | Target ChameleonUltra device |
| `file_path` | Path to dump file relative to HA config dir |
| `slot` | Slot number (1-8) |

Supported formats: `.mfd`/`.bin` (raw binary), `.nfc` (Flipper Zero text format).

## Slot Numbering

The integration displays slots as **1-8** to match the official ChameleonUltra GUI app. Internally, the protocol uses 0-7.
