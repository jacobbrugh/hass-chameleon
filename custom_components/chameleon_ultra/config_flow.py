"""Config flow for ChameleonUltra integration.

Supports two entry points:
1. Bluetooth auto-discovery (HA finds a device advertising "ChameleonUltra")
2. Manual add (user picks from discovered BLE devices)

Both paths collect an optional BLE pairing PIN and store it in config data.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback

from .const import (
    CONF_EMULATION_HOLD_TIME,
    CONF_PIN,
    DEFAULT_EMULATION_HOLD_TIME,
    DEFAULT_PIN,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class ChameleonUltraConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for ChameleonUltra."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}

    # ------------------------------------------------------------------
    # Bluetooth auto-discovery
    # ------------------------------------------------------------------

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle Bluetooth discovery."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery_info = discovery_info

        name = discovery_info.name or "ChameleonUltra"
        self.context["title_placeholders"] = {"name": name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm Bluetooth discovery and collect PIN."""
        assert self._discovery_info is not None
        if user_input is not None:
            pin = user_input.get(CONF_PIN, "").strip() or DEFAULT_PIN
            return self.async_create_entry(
                title=self._discovery_info.name or "ChameleonUltra",
                data={
                    CONF_ADDRESS: self._discovery_info.address,
                    CONF_PIN: pin,
                },
                options={CONF_EMULATION_HOLD_TIME: DEFAULT_EMULATION_HOLD_TIME},
            )

        name = self._discovery_info.name or "ChameleonUltra"
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": name},
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_PIN, default=DEFAULT_PIN): str,
                }
            ),
        )

    # ------------------------------------------------------------------
    # Manual add
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual add — list discovered ChameleonUltra devices."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            info = self._discovered_devices.get(address)
            name = info.name if info else "ChameleonUltra"
            pin = user_input.get(CONF_PIN, "").strip() or DEFAULT_PIN
            return self.async_create_entry(
                title=name,
                data={
                    CONF_ADDRESS: address,
                    CONF_PIN: pin,
                },
                options={CONF_EMULATION_HOLD_TIME: DEFAULT_EMULATION_HOLD_TIME},
            )

        # Build list of discovered ChameleonUltra devices
        self._discovered_devices = {}
        for info in async_discovered_service_info(self.hass, connectable=True):
            if info.name and (
                info.name.startswith("ChameleonUltra")
                or info.name.startswith("ChameleonLite")
            ):
                self._discovered_devices[info.address] = info

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        options = {
            addr: f"{info.name} ({addr})"
            for addr, info in self._discovered_devices.items()
        }

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(options),
                    vol.Optional(CONF_PIN, default=DEFAULT_PIN): str,
                }
            ),
        )

    # ------------------------------------------------------------------
    # Options flow
    # ------------------------------------------------------------------

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return ChameleonUltraOptionsFlow(config_entry)


class ChameleonUltraOptionsFlow(OptionsFlow):
    """Handle options for ChameleonUltra."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = self._config_entry.options.get(
            CONF_EMULATION_HOLD_TIME, DEFAULT_EMULATION_HOLD_TIME
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_EMULATION_HOLD_TIME,
                        default=current,
                    ): vol.All(
                        vol.Coerce(float),
                        vol.Range(min=0.5, max=30.0),
                    ),
                }
            ),
        )
