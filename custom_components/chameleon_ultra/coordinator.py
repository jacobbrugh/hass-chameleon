"""DataUpdateCoordinator for ChameleonUltra.

Manages the BLE connection lifecycle using a connect-on-demand model.
The ChameleonUltra is a battery device that sleeps when idle and stops
BLE advertising. Connections are transient — we connect, do work, and
let the device sleep. Poll failures due to the device being asleep are
normal and don't mark entities unavailable; we just reuse the last
known state.
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
        self._last_good_data: dict[str, Any] | None = None

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

        # Clean up stale state from previous connection
        self._client = None
        self._device = None

        # Get a fresh BLE device reference from HA's Bluetooth stack
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if not ble_device:
            raise UpdateFailed(
                f"ChameleonUltra {self.address} not found — device may be asleep"
            )
        _LOGGER.debug(
            "Got BLE device ref for %s: name=%s", self.address, ble_device.name
        )

        # Ensure the device is paired in BlueZ before attempting GATT connection
        try:
            paired = await async_is_paired(self.address)
            _LOGGER.debug("Pairing check for %s: %s", self.address, paired)
            if not paired:
                pin = self.config_entry.data.get(CONF_PIN, DEFAULT_PIN)
                _LOGGER.info(
                    "ChameleonUltra %s not paired — initiating BLE pairing",
                    self.address,
                )
                await async_pair_with_pin(self.address, pin)
                _LOGGER.info("BLE pairing completed for %s", self.address)
        except Exception as err:
            _LOGGER.debug(
                "Pairing check/attempt for %s: %s (continuing anyway)",
                self.address,
                err,
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
            "Connected to %s, MTU=%s", self.address, self._client.mtu_size
        )

        self._device = ChameleonUltraDevice(self._client)
        await self._client.start_notify(
            NUS_TX_CHAR_UUID, self._device.on_notification
        )
        self._reset_disconnect_timer()
        _LOGGER.info("Connected to ChameleonUltra %s", self.address)
        return self._device

    def _on_disconnect(self, client: BleakClient) -> None:
        """Handle BLE disconnection."""
        self._cancel_disconnect_timer()
        self._client = None
        self._device = None
        if not self._expected_disconnect:
            _LOGGER.debug(
                "ChameleonUltra %s disconnected (device likely sleeping)",
                self.address,
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
        """Poll device state.

        If the device is asleep (unreachable), return the last known data
        with connected=False instead of raising UpdateFailed. This keeps
        entities available with stale-but-useful state.
        """
        try:
            device = await self._ensure_connected()
        except Exception as err:
            if self._last_good_data is not None:
                _LOGGER.debug(
                    "ChameleonUltra %s unreachable, using cached state: %s",
                    self.address,
                    err,
                )
                return {**self._last_good_data, "connected": False}
            raise UpdateFailed(f"Failed to connect: {err}") from err

        try:
            battery = await device.get_battery_info()
            active_slot = await device.get_active_slot()
            slot_info = await device.get_slot_info()
            enabled_slots = await device.get_enabled_slots()
            device_mode = await device.get_device_mode()
            firmware = await device.get_git_version()

            slot_nicks: list[str] = []
            for i in range(SLOT_COUNT):
                try:
                    nick = await device.get_slot_tag_nick(i, 0x01)  # HF
                    slot_nicks.append(nick)
                except (ProtocolError, ChameleonTimeoutError):
                    slot_nicks.append("")

            _LOGGER.debug(
                "Poll OK: battery=%s%%, slot=%d, fw=%s",
                battery["percentage"],
                active_slot,
                firmware,
            )

        except (ProtocolError, ChameleonTimeoutError, OSError) as err:
            if self._last_good_data is not None:
                _LOGGER.debug(
                    "Poll command failed (%s), using cached state", err
                )
                return {**self._last_good_data, "connected": False}
            raise UpdateFailed(f"Communication error: {err}") from err

        data = {
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
        self._last_good_data = data
        return data
