"""BLE pairing helper for ChameleonUltra using BlueZ D-Bus.

Registers a temporary BlueZ Agent1 that provides the ChameleonUltra's
6-digit BLE passkey during SMP pairing. The agent is registered only
for the duration of the pairing attempt and then removed.
"""

import logging

from dbus_fast.aio import MessageBus
from dbus_fast.constants import BusType
from dbus_fast.service import ServiceInterface, method
from dbus_fast import Variant

_LOGGER = logging.getLogger(__name__)

AGENT_PATH = "/org/homeassistant/chameleon_ultra_agent"


class _PinAgent(ServiceInterface):
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


async def async_is_paired(address: str, adapter: str = "hci0") -> bool:
    """Check if a BLE device is already paired/bonded in BlueZ."""
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


async def async_pair_with_pin(
    address: str, pin: str, adapter: str = "hci0"
) -> None:
    """Pair with a BLE device using a 6-digit passkey via BlueZ D-Bus.

    Registers a temporary Agent1 that responds to passkey requests,
    initiates pairing, trusts the device, then cleans up.

    The ChameleonUltra's A button must be held during this call.

    Args:
        address: BLE MAC address (e.g. "FE:6A:52:FA:98:62").
        pin: 6-digit passkey string (default "123456").
        adapter: BlueZ adapter name (default "hci0").
    """
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    agent = _PinAgent(int(pin))
    agent_registered = False

    try:
        # Export the agent object on our D-Bus connection
        bus.export(AGENT_PATH, agent)

        # Register it with BlueZ as the default agent
        bluez_intro = await bus.introspect("org.bluez", "/org/bluez")
        bluez_obj = bus.get_proxy_object("org.bluez", "/org/bluez", bluez_intro)
        agent_mgr = bluez_obj.get_interface("org.bluez.AgentManager1")
        await agent_mgr.call_register_agent(AGENT_PATH, "KeyboardDisplay")
        await agent_mgr.call_request_default_agent(AGENT_PATH)
        agent_registered = True
        _LOGGER.debug("Registered BLE pairing agent at %s", AGENT_PATH)

        # Get the BlueZ Device1 object
        dev_path = f"/org/bluez/{adapter}/dev_{address.replace(':', '_')}"
        dev_intro = await bus.introspect("org.bluez", dev_path)
        dev_obj = bus.get_proxy_object("org.bluez", dev_path, dev_intro)
        device = dev_obj.get_interface("org.bluez.Device1")
        props = dev_obj.get_interface("org.freedesktop.DBus.Properties")

        # Check if already paired
        paired = await props.call_get("org.bluez.Device1", "Paired")
        if paired.value:
            _LOGGER.info("ChameleonUltra %s is already paired", address)
            return

        # Initiate pairing — BlueZ will call our agent's RequestPasskey
        _LOGGER.info("Pairing with ChameleonUltra %s (hold A button!)", address)
        await device.call_pair()

        # Trust the device so BlueZ auto-connects in the future
        await props.call_set(
            "org.bluez.Device1", "Trusted", Variant("b", True)
        )
        _LOGGER.info("Successfully paired and trusted ChameleonUltra %s", address)

    finally:
        if agent_registered:
            try:
                bluez_intro = await bus.introspect("org.bluez", "/org/bluez")
                bluez_obj = bus.get_proxy_object(
                    "org.bluez", "/org/bluez", bluez_intro
                )
                agent_mgr = bluez_obj.get_interface("org.bluez.AgentManager1")
                await agent_mgr.call_unregister_agent(AGENT_PATH)
                _LOGGER.debug("Unregistered BLE pairing agent")
            except Exception:
                _LOGGER.debug("Failed to unregister agent (may already be gone)")
        bus.disconnect()
