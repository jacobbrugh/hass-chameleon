"""Button platform for ChameleonUltra — unlock trigger."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_EMULATION_HOLD_TIME, Command, DEFAULT_EMULATION_HOLD_TIME, DOMAIN
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

    Sequence: start sensing → wait hold_time → stop sensing.
    Uses SET_EMULATION_SENSE which toggles the NFCT/LF peripherals without
    touching slot config or active slot. The entire cycle is atomic (holds
    the command lock).
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
            "Unlock: slot=%d hold=%.1fs — sending SENSE ON", active_slot, hold_time
        )

        try:
            # Atomic sequence under the command lock: sense on → wait → sense off
            async with device._command_lock:
                resp = await device._send_locked(
                    cmd=Command.SET_EMULATION_SENSE,
                    data=bytes([0x01]),
                )
                _LOGGER.info(
                    "Unlock: SENSE ON response: cmd=%s status=%s data=%s",
                    resp.cmd, resp.status, resp.data.hex() if resp.data else "empty",
                )
                _LOGGER.info("Unlock: emulation active, waiting %.1fs", hold_time)
                await asyncio.sleep(hold_time)
                resp = await device._send_locked(
                    cmd=Command.SET_EMULATION_SENSE,
                    data=bytes([0x00]),
                )
                _LOGGER.info(
                    "Unlock: SENSE OFF response: cmd=%s status=%s data=%s",
                    resp.cmd, resp.status, resp.data.hex() if resp.data else "empty",
                )
        except Exception:
            _LOGGER.error(
                "Unlock FAILED — exception during emulation cycle",
                exc_info=True,
            )
            raise

        _LOGGER.info("Unlock: cycle complete")

        # Refresh coordinator state to reflect the enable/disable
        await self.coordinator.async_request_refresh()

        self.hass.bus.async_fire(
            f"{DOMAIN}_event",
            {"type": "unlock_triggered", "slot": active_slot},
        )
