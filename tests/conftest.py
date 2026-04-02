"""Shared test fixtures for ChameleonUltra integration tests.

Mocks external dependencies (bleak, homeassistant) at the sys.modules level
so protocol and device tests can run without the full HA environment.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock


def _ensure_mock(name: str) -> None:
    """Install a MagicMock as a module if it's not already importable."""
    if name not in sys.modules:
        sys.modules[name] = MagicMock()


# Mock bleak and its submodules
for mod in [
    "bleak",
    "bleak.backends",
    "bleak.backends.device",
    "bleak_retry_connector",
    "dbus_fast",
    "dbus_fast.aio",
    "dbus_fast.constants",
    "dbus_fast.service",
]:
    _ensure_mock(mod)

# dbus_fast.service needs real decorators for class construction
dbus_service = sys.modules["dbus_fast.service"]
dbus_service.ServiceInterface = type("ServiceInterface", (), {"__init__": lambda self, name: None})
dbus_service.method = lambda **kw: (lambda f: f)  # identity decorator

dbus_constants = sys.modules["dbus_fast.constants"]
dbus_constants.BusType = type("BusType", (), {"SYSTEM": 0})

# Mock homeassistant and all submodules used by the integration
_ha_mods = [
    "homeassistant",
    "homeassistant.components",
    "homeassistant.components.bluetooth",
    "homeassistant.components.binary_sensor",
    "homeassistant.components.button",
    "homeassistant.components.event",
    "homeassistant.components.select",
    "homeassistant.components.sensor",
    "homeassistant.components.switch",
    "homeassistant.config_entries",
    "homeassistant.const",
    "homeassistant.core",
    "homeassistant.helpers",
    "homeassistant.helpers.device_registry",
    "homeassistant.helpers.entity",
    "homeassistant.helpers.entity_platform",
    "homeassistant.helpers.update_coordinator",
    "voluptuous",
]
for mod in _ha_mods:
    _ensure_mock(mod)

# Ensure the mocked homeassistant.const provides CONF_ADDRESS and PERCENTAGE
ha_const = sys.modules["homeassistant.const"]
ha_const.CONF_ADDRESS = "address"
ha_const.PERCENTAGE = "%"

# Ensure voluptuous has the Schema and Required attributes the config flow needs
vol = sys.modules["voluptuous"]
vol.Schema = MagicMock
vol.Required = MagicMock
vol.Optional = MagicMock
vol.In = MagicMock
vol.All = MagicMock
vol.Coerce = MagicMock
vol.Range = MagicMock

# Ensure CoordinatorEntity can be subclassed
coordinator_mod = sys.modules["homeassistant.helpers.update_coordinator"]
class _FakeDataUpdateCoordinator:
    def __init__(self, *a, **kw) -> None: ...
    def __class_getitem__(cls, item): return cls  # support Generic[T] subscript

class _FakeCoordinatorEntity:
    def __init__(self, *a, **kw) -> None: ...
    def __init_subclass__(cls, **kw) -> None: ...

coordinator_mod.DataUpdateCoordinator = _FakeDataUpdateCoordinator
coordinator_mod.CoordinatorEntity = _FakeCoordinatorEntity
coordinator_mod.UpdateFailed = Exception

# Ensure entity platform classes can be subclassed
for platform_mod_name, class_names in [
    ("homeassistant.components.button", ["ButtonEntity"]),
    ("homeassistant.components.binary_sensor", ["BinarySensorEntity", "BinarySensorDeviceClass"]),
    ("homeassistant.components.event", ["EventEntity", "EventDeviceClass"]),
    ("homeassistant.components.select", ["SelectEntity"]),
    ("homeassistant.components.sensor", ["SensorEntity", "SensorDeviceClass", "SensorStateClass"]),
    ("homeassistant.components.switch", ["SwitchEntity"]),
]:
    mod = sys.modules[platform_mod_name]
    for name in class_names:
        setattr(mod, name, type(name, (), {"__init__": lambda *a, **kw: None}))

# EntityCategory
entity_mod = sys.modules["homeassistant.helpers.entity"]
entity_mod.EntityCategory = type("EntityCategory", (), {"DIAGNOSTIC": "diagnostic", "CONFIG": "config"})

# DeviceInfo and CONNECTION_BLUETOOTH
dr_mod = sys.modules["homeassistant.helpers.device_registry"]
dr_mod.DeviceInfo = dict
dr_mod.CONNECTION_BLUETOOTH = "bluetooth"

# ConfigFlow and friends
config_entries = sys.modules["homeassistant.config_entries"]
config_entries.ConfigFlow = type("ConfigFlow", (), {"__init_subclass__": classmethod(lambda cls, **kw: None)})
config_entries.ConfigEntry = MagicMock
config_entries.ConfigFlowResult = dict
config_entries.OptionsFlow = type("OptionsFlow", (), {})

# Platform enum
ha_const.Platform = type("Platform", (), {
    "BUTTON": "button",
    "BINARY_SENSOR": "binary_sensor",
    "EVENT": "event",
    "SELECT": "select",
    "SENSOR": "sensor",
    "SWITCH": "switch",
})

# callback decorator (identity)
core = sys.modules["homeassistant.core"]
core.callback = lambda f: f
core.HomeAssistant = MagicMock
core.ServiceCall = MagicMock
