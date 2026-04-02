"""Binary sensor platform for ChameleonUltra — connectivity and mode."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, DeviceMode
from .coordinator import ChameleonUltraCoordinator
from .entity import ChameleonUltraEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ChameleonUltraCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        ChameleonUltraConnectivitySensor(coordinator),
        ChameleonUltraModeSensor(coordinator),
    ])


class ChameleonUltraConnectivitySensor(ChameleonUltraEntity, BinarySensorEntity):
    """BLE connectivity sensor."""

    _attr_name = "Connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ChameleonUltraCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_connected"

    @property
    def is_on(self) -> bool:
        return self.coordinator.is_connected


class ChameleonUltraModeSensor(ChameleonUltraEntity, BinarySensorEntity):
    """Device mode sensor — on when in emulator mode."""

    _attr_name = "Emulator Mode"
    _attr_icon = "mdi:contactless-payment"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ChameleonUltraCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_emulator_mode"

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("device_mode") == DeviceMode.EMULATOR
