"""ChameleonUltra NFC Emulator integration for Home Assistant."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, SenseType, TagType
from .coordinator import ChameleonUltraCoordinator
from .protocol import ProtocolError

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.BINARY_SENSOR,
    Platform.EVENT,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up ChameleonUltra from a config entry."""
    address: str = entry.data[CONF_ADDRESS]

    coordinator = ChameleonUltraCoordinator(hass, address, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _register_services(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a ChameleonUltra config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator: ChameleonUltraCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
    return unload_ok


# ---------------------------------------------------------------------------
# Service: load_dump
# ---------------------------------------------------------------------------

_SERVICES_REGISTERED = False


def _register_services(hass: HomeAssistant) -> None:
    """Register integration services (idempotent)."""
    global _SERVICES_REGISTERED  # noqa: PLW0603
    if _SERVICES_REGISTERED:
        return
    _SERVICES_REGISTERED = True

    async def handle_load_dump(call: ServiceCall) -> None:
        """Load an NFC dump file into a ChameleonUltra emulation slot."""
        device_id: str = call.data["device_id"]
        file_path: str = call.data["file_path"]
        slot: int = call.data["slot"] - 1  # service UI is 1-indexed, protocol is 0-indexed

        # Resolve coordinator from device_id
        dev_reg = dr.async_get(hass)
        device_entry = dev_reg.async_get(device_id)
        if device_entry is None:
            raise ValueError(f"Device {device_id} not found")

        coordinator: ChameleonUltraCoordinator | None = None
        for entry_id, coord in hass.data.get(DOMAIN, {}).items():
            if any(
                (DOMAIN, coord.address) == ident
                for ident in device_entry.identifiers
            ):
                coordinator = coord
                break

        if coordinator is None:
            raise ValueError(f"No ChameleonUltra coordinator for device {device_id}")

        # Resolve and validate file path
        resolved = Path(hass.config.path(file_path)).resolve()
        config_dir = Path(hass.config.path()).resolve()
        if not str(resolved).startswith(str(config_dir)):
            raise ValueError("File path must be within the HA config directory")
        if not resolved.is_file():
            raise FileNotFoundError(f"Dump file not found: {resolved}")

        # Parse dump file
        dump_data = _parse_dump_file(resolved)
        if len(dump_data) < 16:
            raise ValueError("Dump file too small — need at least 1 block (16 bytes)")

        device = await coordinator._ensure_connected()

        # Set up the slot
        await device.set_active_slot(slot)
        await device.set_slot_tag_type(slot, TagType.MF1_1K)

        # Extract anti-collision data from block 0
        block0 = dump_data[:16]
        uid = block0[:4]  # 4-byte UID for MF Classic 1K
        sak = block0[5]
        atqa = bytes([block0[6], block0[7]])
        await device.set_anti_coll_data(uid, atqa, sak)

        # Upload block data in chunks (max 31 blocks = 496 bytes per write)
        total_blocks = len(dump_data) // 16
        chunk_size = 31
        for start in range(0, total_blocks, chunk_size):
            end = min(start + chunk_size, total_blocks)
            chunk = dump_data[start * 16 : end * 16]
            await device.write_emu_block_data(start, chunk)

        # Enable slot and persist
        await device.set_slot_enable(slot, SenseType.HF, True)
        await device.save_slot_config()

        _LOGGER.info(
            "Loaded %d blocks into slot %d from %s",
            total_blocks,
            slot,
            file_path,
        )

    hass.services.async_register(DOMAIN, "load_dump", handle_load_dump)


def _parse_dump_file(path: Path) -> bytes:
    """Parse a dump file and return raw block data.

    Supports:
    - .mfd / .bin: raw binary (expected 1024 bytes for MF Classic 1K)
    - .nfc: Flipper Zero text format
    """
    suffix = path.suffix.lower()

    if suffix in (".mfd", ".bin"):
        return path.read_bytes()

    if suffix == ".nfc":
        return _parse_flipper_nfc(path)

    raise ValueError(f"Unsupported dump file format: {suffix}")


def _parse_flipper_nfc(path: Path) -> bytes:
    """Parse a Flipper Zero .nfc dump file."""
    blocks: dict[int, bytes] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("Block ") and ":" in line:
            parts = line.split(":", 1)
            block_num = int(parts[0].replace("Block ", "").strip())
            hex_data = parts[1].strip().replace(" ", "")
            blocks[block_num] = bytes.fromhex(hex_data)

    if not blocks:
        raise ValueError("No block data found in .nfc file")

    max_block = max(blocks.keys())
    result = bytearray((max_block + 1) * 16)
    for num, data in blocks.items():
        result[num * 16 : num * 16 + len(data)] = data
    return bytes(result)
