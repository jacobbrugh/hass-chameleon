"""Button platform for ChameleonUltra — unlock trigger."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_EMULATION_HOLD_TIME, DEFAULT_EMULATION_HOLD_TIME, DOMAIN, SenseType
from .coordinator import ChameleonUltraCoordinator
from .entity import ChameleonUltraEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ChameleonUltraCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ChameleonUltraUnlockButton(coordinator)])


class ChameleonUltraUnlockButton(ChameleonUltraEntity, ButtonEntity):
    """Button that triggers a timed NFC emulation cycle to unlock a door.

    Sequence: enable HF emulation on the active slot → wait hold_time →
    disable HF emulation. The entire cycle is atomic (holds the command lock).
    """

    _attr_name = "Unlock"
    _attr_icon = "mdi:door-open"

    def __init__(self, coordinator: ChameleonUltraCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_unlock"

    async def async_press(self) -> None:
        """Execute the unlock cycle."""
        device = await self.coordinator._ensure_connected()

        hold_time = self.coordinator.config_entry.options.get(
            CONF_EMULATION_HOLD_TIME, DEFAULT_EMULATION_HOLD_TIME
        )
        active_slot = self.coordinator.data.get("active_slot", 0)

        _LOGGER.info(
            "Unlock triggered: slot=%d, hold_time=%.1fs", active_slot, hold_time
        )

        try:
            # Atomic sequence under the command lock: enable → wait → disable
            async with device._command_lock:
                await device._send_locked(
                    cmd=1006,  # SET_SLOT_ENABLE
                    data=bytes([active_slot, SenseType.HF, 0x01]),
                )
                await asyncio.sleep(hold_time)
                await device._send_locked(
                    cmd=1006,  # SET_SLOT_ENABLE
                    data=bytes([active_slot, SenseType.HF, 0x00]),
                )
        except Exception:
            _LOGGER.warning(
                "Unlock sequence interrupted — device will auto-disable "
                "emulation when BLE connection drops (~5s)",
                exc_info=True,
            )
            raise

        # Refresh coordinator state to reflect the enable/disable
        await self.coordinator.async_request_refresh()

        self.hass.bus.async_fire(
            f"{DOMAIN}_event",
            {"type": "unlock_triggered", "slot": active_slot},
        )
