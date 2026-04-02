"""DataUpdateCoordinator for ChameleonUltra.

Manages the BLE connection lifecycle using a connect-on-demand model with a
disconnect timer. Polls device state (battery, slots, mode) every 60 seconds.
After each interaction, a 30-second disconnect timer starts — if no new
commands arrive, the connection is dropped and the device returns to sleep.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from bleak import BleakClient
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
)
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DEFAULT_DISCONNECT_DELAY,
    DOMAIN,
    NUS_TX_CHAR_UUID,
    SLOT_COUNT,
    DeviceModel,
)
from .device import ChameleonTimeoutError, ChameleonUltraDevice
from .protocol import ProtocolError

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(seconds=60)


class ChameleonUltraCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that manages the ChameleonUltra BLE connection and state."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=POLL_INTERVAL,
        )
        self.address = address
        self.config_entry = entry
        self._client: BleakClient | None = None
        self._device: ChameleonUltraDevice | None = None
        self._disconnect_timer: asyncio.TimerHandle | None = None
        self._unavailable_callback: callback | None = None
        self._expected_disconnect = False

    @property
    def device(self) -> ChameleonUltraDevice | None:
        """Return the active device client, or None if disconnected."""
        return self._device

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def _ensure_connected(self) -> ChameleonUltraDevice:
        """Connect to the device if not already connected.

        Returns the ChameleonUltraDevice instance.
        """
        if self._client is not None and self._client.is_connected and self._device is not None:
            self._reset_disconnect_timer()
            return self._device

        # Get a fresh BLE device reference from HA's Bluetooth stack
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if not ble_device:
            raise UpdateFailed(
                f"ChameleonUltra {self.address} not found in Bluetooth advertisements"
            )

        _LOGGER.debug("Connecting to ChameleonUltra %s", self.address)
        self._expected_disconnect = False
        self._client = await establish_connection(
            BleakClientWithServiceCache,
            ble_device,
            self.address,
            disconnected_callback=self._on_disconnect,
            max_attempts=3,
        )

        self._device = ChameleonUltraDevice(self._client)
        await self._client.start_notify(
            NUS_TX_CHAR_UUID, self._device.on_notification
        )
        self._reset_disconnect_timer()
        _LOGGER.info("Connected to ChameleonUltra %s", self.address)
        return self._device

    def _on_disconnect(self, client: BleakClient) -> None:
        """Handle unexpected BLE disconnection."""
        self._cancel_disconnect_timer()
        self._client = None
        self._device = None
        if not self._expected_disconnect:
            _LOGGER.warning("ChameleonUltra %s disconnected unexpectedly", self.address)

    def _reset_disconnect_timer(self) -> None:
        """Start or reset the idle disconnect timer."""
        self._cancel_disconnect_timer()
        self._disconnect_timer = self.hass.loop.call_later(
            DEFAULT_DISCONNECT_DELAY,
            lambda: self.hass.async_create_task(self._disconnect()),
        )

    def _cancel_disconnect_timer(self) -> None:
        if self._disconnect_timer is not None:
            self._disconnect_timer.cancel()
            self._disconnect_timer = None

    async def _disconnect(self) -> None:
        """Gracefully disconnect from the device."""
        self._cancel_disconnect_timer()
        if self._client is not None and self._client.is_connected:
            self._expected_disconnect = True
            _LOGGER.debug("Idle disconnect from ChameleonUltra %s", self.address)
            await self._client.disconnect()
        self._client = None
        self._device = None

    async def async_shutdown(self) -> None:
        """Clean up on integration unload."""
        self._cancel_disconnect_timer()
        if self._client is not None and self._client.is_connected:
            self._expected_disconnect = True
            await self._client.disconnect()
        self._client = None
        self._device = None

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Poll device state."""
        try:
            device = await self._ensure_connected()
        except Exception as err:
            raise UpdateFailed(f"Failed to connect: {err}") from err

        try:
            battery = await device.get_battery_info()
            active_slot = await device.get_active_slot()
            slot_info = await device.get_slot_info()
            enabled_slots = await device.get_enabled_slots()
            device_mode = await device.get_device_mode()
            firmware = await device.get_git_version()

            # Fetch slot nicknames (best-effort, some may fail)
            slot_nicks: list[str] = []
            for i in range(SLOT_COUNT):
                try:
                    nick = await device.get_slot_tag_nick(i, 0x01)  # HF
                    slot_nicks.append(nick)
                except (ProtocolError, ChameleonTimeoutError):
                    slot_nicks.append("")

        except (ProtocolError, ChameleonTimeoutError, OSError) as err:
            raise UpdateFailed(f"Communication error: {err}") from err

        return {
            "battery_percentage": battery["percentage"],
            "battery_voltage": battery["voltage"],
            "active_slot": active_slot,
            "slot_info": slot_info,
            "enabled_slots": enabled_slots,
            "device_mode": device_mode,
            "firmware_version": firmware,
            "slot_nicks": slot_nicks,
            "connected": True,
        }
