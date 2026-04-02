"""Constants for the ChameleonUltra integration."""

from enum import IntEnum

DOMAIN = "chameleon_ultra"

# Nordic UART Service UUIDs
NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_CHAR_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # host -> device
NUS_TX_CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # device -> host

# Protocol constants
SOF = 0x11
LRC1 = 0xEF
FRAME_PREAMBLE_SIZE = 9  # SOF + LRC1 + CMD(2) + STATUS(2) + LEN(2) + LRC2
FRAME_MIN_SIZE = 10  # preamble + LRC3
MAX_DATA_LENGTH = 512  # firmware limit per the protocol wiki
MAX_FRAME_SIZE = FRAME_MIN_SIZE + MAX_DATA_LENGTH

# Default timings
DEFAULT_EMULATION_HOLD_TIME = 3.0  # seconds
DEFAULT_DISCONNECT_DELAY = 30.0  # seconds before disconnecting idle BLE
DEFAULT_COMMAND_TIMEOUT = 5.0  # seconds to wait for a response

SLOT_COUNT = 8

# Config entry keys
CONF_EMULATION_HOLD_TIME = "emulation_hold_time"


class Command(IntEnum):
    """ChameleonUltra protocol command codes."""

    GET_APP_VERSION = 1000
    CHANGE_DEVICE_MODE = 1001
    GET_DEVICE_MODE = 1002
    SET_ACTIVE_SLOT = 1003
    SET_SLOT_TAG_TYPE = 1004
    SET_SLOT_DATA_DEFAULT = 1005
    SET_SLOT_ENABLE = 1006
    SET_SLOT_TAG_NICK = 1007
    GET_SLOT_TAG_NICK = 1008
    SLOT_DATA_CONFIG_SAVE = 1009
    ENTER_BOOTLOADER = 1010
    GET_DEVICE_CHIP_ID = 1011
    SAVE_SETTINGS = 1013
    GET_GIT_VERSION = 1017
    GET_ACTIVE_SLOT = 1018
    GET_SLOT_INFO = 1019
    GET_ENABLED_SLOTS = 1023
    GET_BATTERY_INFO = 1025
    SET_BLE_PAIRING_KEY = 1030
    GET_BLE_PAIRING_KEY = 1031
    GET_DEVICE_MODEL = 1033
    GET_DEVICE_SETTINGS = 1034
    GET_DEVICE_CAPABILITIES = 1035
    GET_BLE_PAIRING_ENABLE = 1036
    SET_BLE_PAIRING_ENABLE = 1037

    MF1_WRITE_EMU_BLOCK_DATA = 4000
    HF14A_SET_ANTI_COLL_DATA = 4001
    MF1_READ_EMU_BLOCK_DATA = 4008
    HF14A_GET_ANTI_COLL_DATA = 4018


class Status(IntEnum):
    """Protocol response status codes."""

    SUCCESS = 0x0000
    PAR_ERR = 0x0001  # parameter error
    DEV_MODE_ERR = 0x0002  # wrong device mode
    FLASH_WRITE_FAIL = 0x0003
    NOT_IMPLEMENTED = 0x0004
    INVALID_CMD = 0xFFFF


class SenseType(IntEnum):
    """NFC sense type (frequency band)."""

    HF = 0x01  # 13.56 MHz
    LF = 0x02  # 125 kHz


class DeviceMode(IntEnum):
    """Device operating mode."""

    EMULATOR = 0x00
    READER = 0x01


class DeviceModel(IntEnum):
    """Device hardware model."""

    ULTRA = 0x00
    LITE = 0x01


class TagType(IntEnum):
    """Tag-specific type identifiers (from tag_specific_type_t)."""

    UNKNOWN = 0
    # HF types
    MF1_MINI = 1
    MF1_1K = 2
    MF1_2K = 3
    MF1_4K = 4
    MF0_MINI = 5
    NTAG_213 = 6
    NTAG_215 = 7
    NTAG_216 = 8
    MF0_UL11 = 9
    MF0_UL21 = 10
    # LF types
    EM410X = 100
    T5577 = 101
