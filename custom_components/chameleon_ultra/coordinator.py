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
    CONF_PIN,
    DEFAULT_DISCONNECT_DELAY,
    DEFAULT_PIN,
    DOMAIN,
    NUS_TX_CHAR_UUID,
    SLOT_COUNT,
    DeviceModel,
)
from .device import ChameleonTimeoutError, ChameleonUltraDevice
from .pairing import async_is_paired, async_pair_with_pin
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
        _LOGGER.debug(
            "Got BLE device ref for %s: name=%s", self.address, ble_device.name
        )

        # Ensure the device is paired in BlueZ before attempting GATT connection
        try:
            _LOGGER.debug("Checking pairing status for %s", self.address)
            paired = await async_is_paired(self.address)
            _LOGGER.debug("Pairing check result for %s: %s", self.address, paired)
            if not paired:
                pin = self.config_entry.data.get(CONF_PIN, DEFAULT_PIN)
                _LOGGER.info(
                    "ChameleonUltra %s not paired — initiating BLE pairing", self.address
                )
                await async_pair_with_pin(self.address, pin)
                _LOGGER.info("BLE pairing completed for %s", self.address)
        except Exception as err:
            _LOGGER.warning(
                "BLE pairing attempt failed for %s: %s (will try connecting anyway)",
                self.address,
                err,
            )

        _LOGGER.debug(
            "Calling establish_connection for %s (max_attempts=3)", self.address
        )
        self._expected_disconnect = False
        self._client = await establish_connection(
            BleakClientWithServiceCache,
            ble_device,
            self.address,
            disconnected_callback=self._on_disconnect,
            max_attempts=3,
        )
        _LOGGER.debug(
            "establish_connection succeeded for %s, MTU=%s",
            self.address, self._client.mtu_size,
        )

        self._device = ChameleonUltraDevice(self._client)
        _LOGGER.debug("Starting NUS TX notifications for %s", self.address)
        await self._client.start_notify(
            NUS_TX_CHAR_UUID, self._device.on_notification
        )
        self._reset_disconnect_timer()
        _LOGGER.info("Connected to ChameleonUltra %s", self.address)
        return self._device

    def _on_disconnect(self, client: BleakClient) -> None:
        """Handle unexpected BLE disconnection."""
        import traceback
        self._cancel_disconnect_timer()
        self._client = None
        self._device = None
        if not self._expected_disconnect:
            _LOGGER.warning(
                "ChameleonUltra %s disconnected unexpectedly. Traceback:\n%s",
                self.address,
                "".join(traceback.format_stack()),
            )

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
            _LOGGER.debug("Polling: get_battery_info")
            battery = await device.get_battery_info()
            _LOGGER.debug("Polling: get_active_slot")
            active_slot = await device.get_active_slot()
            _LOGGER.debug("Polling: get_slot_info")
            slot_info = await device.get_slot_info()
            _LOGGER.debug("Polling: get_enabled_slots")
            enabled_slots = await device.get_enabled_slots()
            _LOGGER.debug("Polling: get_device_mode")
            device_mode = await device.get_device_mode()
            _LOGGER.debug("Polling: get_git_version")
            firmware = await device.get_git_version()

            # Fetch slot nicknames (best-effort, some may fail)
            slot_nicks: list[str] = []
            for i in range(SLOT_COUNT):
                try:
                    _LOGGER.debug("Polling: get_slot_tag_nick(%d)", i)
                    nick = await device.get_slot_tag_nick(i, 0x01)  # HF
                    slot_nicks.append(nick)
                except (ProtocolError, ChameleonTimeoutError):
                    slot_nicks.append("")

            _LOGGER.debug("Poll complete: battery=%s%%, slot=%d, fw=%s",
                          battery["percentage"], active_slot, firmware)

        except (ProtocolError, ChameleonTimeoutError, OSError) as err:
            _LOGGER.error("Poll failed at command: %s", err)
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
