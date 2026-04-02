"""Base entity for ChameleonUltra integration."""

from __future__ import annotations

from homeassistant.const import CONF_ADDRESS
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ChameleonUltraCoordinator


class ChameleonUltraEntity(CoordinatorEntity[ChameleonUltraCoordinator]):
    """Base class for ChameleonUltra entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ChameleonUltraCoordinator) -> None:
        super().__init__(coordinator)

    @property
    def device_info(self) -> DeviceInfo:
        data = self.coordinator.data or {}
        model = "ChameleonUltra"
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.address)},
            name=f"ChameleonUltra {self.coordinator.address[-5:].replace(':', '')}",
            manufacturer="RfidResearchGroup",
            model=model,
            sw_version=data.get("firmware_version"),
            connections={(CONNECTION_BLUETOOTH, self.coordinator.address)},
        )

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None
