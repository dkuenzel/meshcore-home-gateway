"""Codes of the companion protocol (MeshCore companion-v1.18.0, examples/companion_radio/MyMesh.cpp
lines 9-140). Only the codes the bridge uses or must recognise."""

from __future__ import annotations

from enum import IntEnum

PROTOCOL_VERSION = 3  # DEVICE_QUERY app version: >= 3 selects the *_MSG_RECV_V3 frames


class Command(IntEnum):
    APP_START = 1
    SEND_TXT_MSG = 2
    SEND_CHANNEL_TXT_MSG = 3
    GET_CONTACTS = 4
    GET_DEVICE_TIME = 5
    SET_DEVICE_TIME = 6
    SYNC_NEXT_MESSAGE = 10
    RESET_PATH = 13
    GET_BATT_AND_STORAGE = 20
    DEVICE_QUERY = 22
    GET_CHANNEL = 31


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


class ErrorCode(IntEnum):
    UNSUPPORTED_CMD = 1
    NOT_FOUND = 2
    TABLE_FULL = 3  # direct message: text too long or no free packet buffer
    BAD_STATE = 4
    FILE_IO_ERROR = 5
    ILLEGAL_ARG = 6
