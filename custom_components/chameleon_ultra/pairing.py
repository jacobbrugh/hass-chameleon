"""BLE pairing agent for ChameleonUltra.

Registers a persistent BlueZ Agent1 that provides the ChameleonUltra's
6-digit BLE passkey during SMP pairing. The agent stays registered for
the lifetime of the coordinator so bleak's pair=True can trigger pairing
at any time.

Bleak handles calling Device1.Pair() itself — this module only provides
the agent that answers the passkey prompt.
"""

import logging

from dbus_fast.aio import MessageBus
from dbus_fast.constants import BusType
from dbus_fast.service import ServiceInterface, method

_LOGGER = logging.getLogger(__name__)

AGENT_PATH = "/org/homeassistant/chameleon_ultra_agent"


async def _find_adapter() -> str:
    """Auto-detect the BlueZ adapter (hci0, hci1, etc.) via D-Bus."""
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        intro = await bus.introspect("org.bluez", "/org/bluez")
        for node in intro.nodes:
            if node.name and node.name.startswith("hci"):
                return node.name
    finally:
        bus.disconnect()
    return "hci0"


class PinAgent(ServiceInterface):
    """BlueZ Agent1 that auto-responds with a static passkey."""

    def __init__(self, pin: int) -> None:
        super().__init__("org.bluez.Agent1")
        self._pin = pin

    @method()
    def Release(self) -> None:
        pass

    @method()
    def RequestPasskey(self, device: "o") -> "u":
        _LOGGER.debug("Providing passkey for %s", device)
        return self._pin

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q") -> None:
        pass

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u") -> None:
        pass

    @method()
    def AuthorizeService(self, device: "o", uuid: "s") -> None:
        pass

    @method()
    def Cancel(self) -> None:
        pass


async def async_is_paired(address: str, adapter: str | None = None) -> bool:
    """Check if a BLE device is already paired/bonded in BlueZ."""
    if adapter is None:
        adapter = await _find_adapter()
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        dev_path = f"/org/bluez/{adapter}/dev_{address.replace(':', '_')}"
        try:
            introspection = await bus.introspect("org.bluez", dev_path)
        except Exception:
            return False
        obj = bus.get_proxy_object("org.bluez", dev_path, introspection)
        props = obj.get_interface("org.freedesktop.DBus.Properties")
        paired = await props.call_get("org.bluez.Device1", "Paired")
        return bool(paired.value)
    finally:
        bus.disconnect()


async def async_register_agent(pin: int) -> tuple[MessageBus, PinAgent]:
    """Register a persistent BlueZ pairing agent.

    Returns the D-Bus connection and agent — caller must keep both alive
    and call async_unregister_agent() on shutdown.
    """
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    agent = PinAgent(pin)
    bus.export(AGENT_PATH, agent)

    bluez_intro = await bus.introspect("org.bluez", "/org/bluez")
    bluez_obj = bus.get_proxy_object("org.bluez", "/org/bluez", bluez_intro)
    agent_mgr = bluez_obj.get_interface("org.bluez.AgentManager1")
    await agent_mgr.call_register_agent(AGENT_PATH, "KeyboardDisplay")
    await agent_mgr.call_request_default_agent(AGENT_PATH)
    _LOGGER.debug("Registered BLE pairing agent at %s", AGENT_PATH)

    return bus, agent


async def async_unregister_agent(bus: MessageBus) -> None:
    """Unregister the pairing agent and disconnect the D-Bus connection."""
    try:
        bluez_intro = await bus.introspect("org.bluez", "/org/bluez")
        bluez_obj = bus.get_proxy_object("org.bluez", "/org/bluez", bluez_intro)
        agent_mgr = bluez_obj.get_interface("org.bluez.AgentManager1")
        await agent_mgr.call_unregister_agent(AGENT_PATH)
        _LOGGER.debug("Unregistered BLE pairing agent")
    except Exception:
        _LOGGER.debug("Failed to unregister agent (may already be gone)")
    bus.disconnect()
