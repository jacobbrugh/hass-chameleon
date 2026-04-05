"""DataUpdateCoordinator for ChameleonUltra.

The ChameleonUltra sleeps 4 seconds after BLE disconnects and won't
re-advertise until a button press or USB connection. To prevent this,
we keep the BLE connection open permanently — the device stays awake
as long as a BLE client is connected. If the connection drops (device
reset, out of range, etc.), we return cached state and reconnect on
the next poll or user action.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from datetime import timedelta
from typing import Any

from bleak import BleakClient
from dbus_fast.aio import MessageBus
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_PIN,
    DEFAULT_PIN,
    DOMAIN,
    NUS_TX_CHAR_UUID,
    SLOT_COUNT,
)
from .device import ChameleonTimeoutError, ChameleonUltraDevice
from .pairing import (
    async_is_paired,
    async_register_agent,
    async_unregister_agent,
)
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
        self._expected_disconnect = False
        self._agent_bus: MessageBus | None = None

    @property
    def device(self) -> ChameleonUltraDevice | None:
        """Return the active device client, or None if disconnected."""
        return self._device

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    # ------------------------------------------------------------------
    # Agent lifecycle
    # ------------------------------------------------------------------

    async def _ensure_agent_registered(self) -> None:
        """Register the BLE pairing agent if not already registered."""
        if self._agent_bus is not None:
            return
        pin = int(self.config_entry.data.get(CONF_PIN, DEFAULT_PIN))
        try:
            self._agent_bus, _ = await async_register_agent(pin)
            _LOGGER.debug("Pairing agent registered (PIN configured)")
        except Exception:
            _LOGGER.exception("Failed to register pairing agent")
            raise

    async def _teardown_agent(self) -> None:
        """Unregister the pairing agent."""
        if self._agent_bus is not None:
            await async_unregister_agent(self._agent_bus)
            self._agent_bus = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def _ensure_connected(self) -> ChameleonUltraDevice:
        """Connect to the device if not already connected.

        Pairing is handled by bleak's pair=True parameter, which calls
        Device1.Pair() as part of the connect flow. Our registered agent
        provides the passkey when BlueZ requests it during SMP.
        """
        if self._client is not None and self._client.is_connected and self._device is not None:
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

        # Ensure the pairing agent is registered before connecting
        await self._ensure_agent_registered()

        # Check if we need to pair — let bleak handle it during connect
        try:
            needs_pair = not await async_is_paired(self.address)
        except Exception:
            _LOGGER.exception("Failed to check pairing state, assuming needs pair")
            needs_pair = True

        if needs_pair:
            _LOGGER.info(
                "ChameleonUltra %s not paired — bleak will pair during connect",
                self.address,
            )
        else:
            _LOGGER.debug("ChameleonUltra %s already paired", self.address)

        self._expected_disconnect = False
        self._client = BleakClient(
            ble_device,
            disconnected_callback=self._on_disconnect,
            timeout=30.0,
            pair=needs_pair,
        )

        try:
            await self._client.connect()
        except Exception:
            _LOGGER.exception(
                "Failed to connect to %s (pair=%s)",
                self.address,
                needs_pair,
            )
            self._client = None
            raise

        # ChameleonUltra negotiates 247-byte MTU at the link layer.
        # Must set before any access to mtu_size to suppress bleak warning.
        self._client._backend._mtu_size = 247

        _LOGGER.info("Connected to %s, MTU=%s", self.address, self._client.mtu_size)

        self._device = ChameleonUltraDevice(self._client)
        try:
            await self._client.start_notify(
                NUS_TX_CHAR_UUID, self._device.on_notification
            )
        except Exception:
            _LOGGER.exception(
                "Failed to start NUS notifications on %s", self.address
            )
            raise

        _LOGGER.info("ChameleonUltra %s ready (MTU=%s)", self.address, self._client.mtu_size)
        return self._device

    def _on_disconnect(self, client: BleakClient) -> None:
        """Handle BLE disconnection."""
        self._client = None
        self._device = None
        if not self._expected_disconnect:
            _LOGGER.warning(
                "ChameleonUltra %s disconnected unexpectedly. Traceback: %s",
                self.address,
                "".join(traceback.format_stack()),
            )

    async def _disconnect(self) -> None:
        """Gracefully disconnect from the device."""
        if self._client is not None and self._client.is_connected:
            self._expected_disconnect = True
            _LOGGER.debug("Disconnecting from ChameleonUltra %s", self.address)
            await self._client.disconnect()
        self._client = None
        self._device = None

    async def async_shutdown(self) -> None:
        """Clean up on integration unload."""
        await self._disconnect()
        await self._teardown_agent()

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Poll device state.

        If the device is asleep (unreachable), return the last known data
        with connected=False instead of raising UpdateFailed. This keeps
        entities available with stale-but-useful state rather than going
        "unavailable" every time the device sleeps.
        """
        try:
            device = await self._ensure_connected()
        except Exception as err:
            raise UpdateFailed(f"Failed to connect: {err}") from err

        try:
            _LOGGER.debug("Polling ChameleonUltra %s...", self.address)
            battery = await device.get_battery_info()
            _LOGGER.debug("  battery: %s", battery)
            active_slot = await device.get_active_slot()
            _LOGGER.debug("  active_slot: %s", active_slot)
            slot_info = await device.get_slot_info()
            _LOGGER.debug("  slot_info: OK")
            enabled_slots = await device.get_enabled_slots()
            _LOGGER.debug("  enabled_slots: OK")
            device_mode = await device.get_device_mode()
            _LOGGER.debug("  device_mode: %s", device_mode)
            firmware = await device.get_git_version()
            _LOGGER.debug("  firmware: %s", firmware)

            slot_nicks: list[str] = []
            for i in range(SLOT_COUNT):
                try:
                    nick = await device.get_slot_tag_nick(i, 0x01)  # HF
                    slot_nicks.append(nick)
                except (ProtocolError, ChameleonTimeoutError):
                    _LOGGER.debug("  slot %d nick failed", i, exc_info=True)
                    slot_nicks.append("")

            _LOGGER.info(
                "Poll OK: battery=%s%%, slot=%d, fw=%s, MTU=%s",
                battery["percentage"],
                active_slot,
                firmware,
                self._client.mtu_size if self._client else "?",
            )

        except (ProtocolError, ChameleonTimeoutError, OSError) as err:
            _LOGGER.error(
                "ChameleonUltra %s command failed",
                self.address,
                exc_info=True,
            )
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
        return data
