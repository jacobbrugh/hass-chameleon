# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Home Assistant custom integration that controls a ChameleonUltra/Lite NFC emulator over BLE. Installed via HACS or by copying `custom_components/chameleon_ultra/` into HA's config directory.

## Commands

```bash
# Run all tests
uv run pytest

# Run a single test file
uv run pytest tests/test_protocol.py

# Run a specific test
uv run pytest tests/test_protocol.py::TestBuildFrame
```

No linting or type-checking tooling is configured in the repo. Pre-commit hooks are managed externally via Nix.

## Architecture

The integration is layered bottom-to-top:

1. **Protocol** (`protocol.py`) — Pure Python binary framing: `SOF | LRC1 | CMD | STATUS | LEN | LRC2 | DATA | LRC3`. `FrameAssembler` is a state machine that handles partial/concatenated BLE notifications with a 3-second stale frame timeout. `build_frame()` constructs request frames. No external dependencies.

2. **Device** (`device.py`) — `ChameleonUltraDevice` wraps `BleakClient` with an asyncio lock for command serialization (one in-flight command at a time). Handles MTU-aware chunking (MTU hardcoded to 247, minus 3-byte ATT header). Response correlation uses `asyncio.Future` resolved by BLE notification callbacks.

3. **Coordinator** (`coordinator.py`) — `ChameleonUltraCoordinator` extends HA's `DataUpdateCoordinator`. Manages BLE connection lifecycle: connect, pair via BlueZ D-Bus agent, poll device state, auto-reconnect. Keeps connection open permanently to prevent device sleep.

4. **Pairing** (`pairing.py`) — `PinAgent` implements BlueZ's Agent1 D-Bus interface using `dbus-fast`. Auto-responds with a static passkey during BLE SMP pairing. Registered once per coordinator lifetime.

5. **Platform entities** (`sensor.py`, `button.py`, `select.py`, `switch.py`, `binary_sensor.py`, `event.py`) — All extend `ChameleonUltraEntity(CoordinatorEntity)`. The unlock button triggers timed HF emulation. Select controls active slot (1-8). Switches toggle per-slot HF enable/disable.

6. **Services** (`__init__.py`) — `load_dump` service uploads `.mfd`/`.bin`/`.nfc` (Flipper Zero) card dumps to emulation slots with chunked block writes (max 31 blocks per write).

## Key Conventions

- **Slot numbering**: UI displays 1-8 (matches official ChameleonUltra app), protocol uses 0-7. Conversion happens in entity and service code.
- **Async-first**: All I/O is async/await. Tests use `pytest-asyncio` with `asyncio_mode = "auto"`.
- **Test mocking**: `tests/conftest.py` injects mock modules for `bleak`, `homeassistant`, etc. at `sys.modules` level before importing integration code. Protocol tests are pure Python with no mocks.
- **Dependencies**: Runtime requires `dbus-fast` (D-Bus pairing) and `bleak-retry-connector` (HA-managed BLE connections). `bleak` itself is provided by HA at runtime.
