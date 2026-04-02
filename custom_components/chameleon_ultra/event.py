"""Event platform for ChameleonUltra — activity events."""

from __future__ import annotations

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ChameleonUltraCoordinator
from .entity import ChameleonUltraEntity


EVENT_TYPES = [
    "unlock_triggered",
    "slot_changed",
    "dump_loaded",
    "connection_lost",
    "connection_restored",
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ChameleonUltraCoordinator = hass.data[DOMAIN][entry.entry_id]
    entity = ChameleonUltraActivityEvent(coordinator)
    async_add_entities([entity])

    # Listen for integration-fired events and forward to the event entity
    @callback
    def _on_event(event) -> None:
        event_type = event.data.get("type")
        if event_type in EVENT_TYPES:
            entity._trigger_event(
                event_type,
                {k: v for k, v in event.data.items() if k != "type"},
            )
            entity.async_write_ha_state()

    entry.async_on_unload(
        hass.bus.async_listen(f"{DOMAIN}_event", _on_event)
    )


class ChameleonUltraActivityEvent(ChameleonUltraEntity, EventEntity):
    """Event entity for ChameleonUltra activity."""

    _attr_name = "Activity"
    _attr_icon = "mdi:bell-ring"
    _attr_event_types = EVENT_TYPES

    def __init__(self, coordinator: ChameleonUltraCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_activity"
