"""Select platform for ChameleonUltra — active slot selector."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SLOT_COUNT, TagType
from .coordinator import ChameleonUltraCoordinator
from .entity import ChameleonUltraEntity

_LOGGER = logging.getLogger(__name__)

# Display names match the official ChameleonUltra GUI (1-indexed)
_SLOT_LABELS = [f"Slot {i + 1}" for i in range(SLOT_COUNT)]

_TAG_TYPE_NAMES: dict[int, str] = {t.value: t.name.replace("_", " ") for t in TagType if t != TagType.UNKNOWN}


def _build_option_label(slot_idx: int, data: dict | None) -> str:
    """Build a human-readable label for a slot option."""
    base = _SLOT_LABELS[slot_idx]
    if data is None:
        return base

    parts: list[str] = []

    # Nickname
    nicks = data.get("slot_nicks", [])
    if slot_idx < len(nicks) and nicks[slot_idx]:
        parts.append(nicks[slot_idx])

    # Tag type
    slot_info = data.get("slot_info", [])
    if slot_idx < len(slot_info):
        hf_type = slot_info[slot_idx].get("hf_type", 0)
        if hf_type in _TAG_TYPE_NAMES:
            parts.append(_TAG_TYPE_NAMES[hf_type])

    if parts:
        return f"{base} — {', '.join(parts)}"
    return base


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ChameleonUltraCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ChameleonUltraSlotSelect(coordinator)])


class ChameleonUltraSlotSelect(ChameleonUltraEntity, SelectEntity):
    """Select entity for choosing the active emulation slot (1-8 display, 0-7 internal)."""

    _attr_name = "Active Slot"
    _attr_icon = "mdi:sd"

    def __init__(self, coordinator: ChameleonUltraCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_active_slot"

    @property
    def options(self) -> list[str]:
        return [
            _build_option_label(i, self.coordinator.data) for i in range(SLOT_COUNT)
        ]

    @property
    def current_option(self) -> str | None:
        if self.coordinator.data is None:
            return None
        slot_idx = self.coordinator.data.get("active_slot", 0)
        return _build_option_label(slot_idx, self.coordinator.data)

    async def async_select_option(self, option: str) -> None:
        """Set the active slot based on selected option."""
        # Extract slot index from the option string ("Slot N" -> N-1)
        for i in range(SLOT_COUNT):
            if option.startswith(_SLOT_LABELS[i]):
                device = await self.coordinator._ensure_connected()
                await device.set_active_slot(i)
                await device.save_slot_config()
                await self.coordinator.async_request_refresh()
                return
        _LOGGER.warning("Could not parse slot from option: %s", option)
