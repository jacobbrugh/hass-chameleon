"""Switch platform for ChameleonUltra — per-slot HF emulation toggles."""

from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SLOT_COUNT, SenseType
from .coordinator import ChameleonUltraCoordinator
from .entity import ChameleonUltraEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ChameleonUltraCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        ChameleonUltraSlotSwitch(coordinator, slot_idx)
        for slot_idx in range(SLOT_COUNT)
    ])


class ChameleonUltraSlotSwitch(ChameleonUltraEntity, SwitchEntity):
    """Switch to enable/disable HF emulation for a specific slot."""

    _attr_icon = "mdi:contactless-payment"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, coordinator: ChameleonUltraCoordinator, slot_idx: int
    ) -> None:
        super().__init__(coordinator)
        self._slot_idx = slot_idx
        # Display as 1-indexed to match official GUI
        self._attr_name = f"Slot {slot_idx + 1} HF Enabled"
        self._attr_unique_id = f"{coordinator.address}_slot_{slot_idx}_hf"

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        enabled_slots = self.coordinator.data.get("enabled_slots", [])
        if self._slot_idx < len(enabled_slots):
            return enabled_slots[self._slot_idx].get("hf_enabled", False)
        return None

    async def async_turn_on(self, **kwargs) -> None:
        device = await self.coordinator._ensure_connected()
        await device.set_slot_enable(self._slot_idx, SenseType.HF, True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        device = await self.coordinator._ensure_connected()
        await device.set_slot_enable(self._slot_idx, SenseType.HF, False)
        await self.coordinator.async_request_refresh()
