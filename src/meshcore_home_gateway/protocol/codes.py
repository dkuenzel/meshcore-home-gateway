"""Codes of the companion protocol (MeshCore companion-v1.18.0, examples/companion_radio/MyMesh.cpp
lines 9-140). Only the codes the bridge uses or must recognise."""

from __future__ import annotations

from enum import IntEnum, IntFlag

PROTOCOL_VERSION = 3  # DEVICE_QUERY app version: >= 3 selects the *_MSG_RECV_V3 frames


class Command(IntEnum):
    """The commands the gateway sends. Never added (ADR 0020, tests/test_codec.py): radio
    parameters, the private key, raw data and packets, signing, the BLE PIN, factory reset and the
    radio's command line."""

    APP_START = 1
    SEND_TXT_MSG = 2
    SEND_CHANNEL_TXT_MSG = 3
    GET_CONTACTS = 4
    GET_DEVICE_TIME = 5
    SET_DEVICE_TIME = 6
    SET_ADVERT_NAME = 8
    ADD_UPDATE_CONTACT = 9
    SYNC_NEXT_MESSAGE = 10
    RESET_PATH = 13
    GET_BATT_AND_STORAGE = 20
    SET_TUNING_PARAMS = 21
    DEVICE_QUERY = 22
    GET_CHANNEL = 31
    SET_OTHER_PARAMS = 38
    GET_TUNING_PARAMS = 43
    GET_STATS = 56
    SET_AUTOADD_CONFIG = 58
    GET_AUTOADD_CONFIG = 59


class Response(IntEnum):
    OK = 0
    ERR = 1
    CONTACTS_START = 2
    CONTACT = 3
    END_OF_CONTACTS = 4
    SELF_INFO = 5
    SENT = 6
    CONTACT_MSG_RECV = 7
    CHANNEL_MSG_RECV = 8
    CURR_TIME = 9
    NO_MORE_MESSAGES = 10
    BATT_AND_STORAGE = 12
    DEVICE_INFO = 13
    DISABLED = 15
    CONTACT_MSG_RECV_V3 = 16
    CHANNEL_MSG_RECV_V3 = 17
    CHANNEL_INFO = 18
    TUNING_PARAMS = 23
    STATS = 24
    AUTOADD_CONFIG = 25
    CHANNEL_DATA_RECV = 27


class Push(IntEnum):
    ADVERT = 0x80
    PATH_UPDATED = 0x81
    SEND_CONFIRMED = 0x82
    MSG_WAITING = 0x83
    RAW_DATA = 0x84
    LOGIN_SUCCESS = 0x85
    LOGIN_FAIL = 0x86
    STATUS_RESPONSE = 0x87
    LOG_RX_DATA = 0x88
    TRACE_DATA = 0x89
    NEW_ADVERT = 0x8A
    TELEMETRY_RESPONSE = 0x8B
    BINARY_RESPONSE = 0x8C
    PATH_DISCOVERY_RESPONSE = 0x8D
    CONTROL_DATA = 0x8E
    CONTACT_DELETED = 0x8F
    CONTACTS_FULL = 0x90


PUSH_MIN = 0x80


class AutoAdd(IntFlag):
    """Auto-add settings (MyMesh.cpp:147-151). They apply when manual adding is on; without it
    the radio adds every type."""

    OVERWRITE_OLDEST = 0x01  # a full table overwrites the oldest non-favourite contact
    CHAT = 0x02
    REPEATER = 0x04
    ROOM_SERVER = 0x08
    SENSOR = 0x10


class StatsType(IntEnum):
    """Second byte of `GET_STATS` and of its answer (MyMesh.cpp:70-73)."""

    CORE = 0
    RADIO = 1
    PACKETS = 2


class NodeType(IntEnum):
    """Advert types (src/helpers/AdvertDataHelpers.h:7-11)."""

    NONE = 0
    CHAT = 1  # a companion: a person's radio
    REPEATER = 2
    ROOM_SERVER = 3
    SENSOR = 4


class ContactFlag(IntFlag):
    """Flags of a contact record (src/helpers/ContactInfo.h:29-33)."""

    FAVOURITE = 0x01  # kept when a full table overwrites the oldest contact
    TELEMETRY_BASE = 0x02  # may request battery telemetry, if the mode says "by contact"
    TELEMETRY_LOCATION = 0x04
    TELEMETRY_ENVIRONMENT = 0x08
    REMOTE_CLI = 0x10  # may run the radio's CLI over the air: never set by the gateway


class TelemetryMode(IntEnum):
    """Who may request a kind of telemetry (examples/companion_radio/NodePrefs.h:7-9)."""

    DENY = 0
    BY_CONTACT = 1  # contacts whose flags allow it
    ALLOW_ALL = 2


class ErrorCode(IntEnum):
    UNSUPPORTED_CMD = 1
    NOT_FOUND = 2
    TABLE_FULL = 3  # direct message: text too long or no free packet buffer
    BAD_STATE = 4
    FILE_IO_ERROR = 5
    ILLEGAL_ARG = 6
