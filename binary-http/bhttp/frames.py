"""BHTTP/1 frames (SPEC.md §1-3).

    +-------------------------------+---------------+---------------+
    |          Length (16)          |   Type (8)    |   Flags (8)   |
    +-------------------------------+---------------+---------------+
    |                        Stream ID (32)                         |
    +---------------------------------------------------------------+
    |                 Payload (Length octets) ...                   |
"""

from __future__ import annotations

import socket
import struct
import time
from typing import NamedTuple

PREFACE = b"BHTTP/1\n"
HEADER = struct.Struct("!HBBI")      # every field whole octets: one unpack, no masking
HEADER_SIZE = HEADER.size            # 8
MAX_PAYLOAD = 0xFFFF
DATA_CHUNK = 16384                   # what we put in one DATA frame; receivers accept up to 65535

DATA, HEADERS, GOAWAY = 0x00, 0x01, 0x02
TYPE_NAMES = {DATA: "DATA", HEADERS: "HEADERS", GOAWAY: "GOAWAY"}
GREASE_TYPES = range(0xF0, 0x100)    # reserved, never assigned: peers MUST skip them

END_STREAM = 0x01

NO_ERROR, PROTOCOL_ERROR, INTERNAL_ERROR, TIMEOUT = 0, 1, 2, 3
ERROR_NAMES = {NO_ERROR: "NO_ERROR", PROTOCOL_ERROR: "PROTOCOL_ERROR",
               INTERNAL_ERROR: "INTERNAL_ERROR", TIMEOUT: "TIMEOUT"}
GOAWAY_FIXED = struct.Struct("!II")


class ProtocolError(Exception):
    """A connection error (SPEC §6): the peer broke the rules and the connection must end."""

    def __init__(self, message, code=PROTOCOL_ERROR):
        super().__init__(message)
        self.code = code


class ConnectionClosed(Exception):
    """EOF exactly at a frame boundary: the peer closed cleanly."""


class FrameTimeout(Exception):
    """A frame (or message) started but did not finish in time."""


class Frame(NamedTuple):
    type: int
    flags: int
    stream_id: int
    payload: bytes = b""

    @property
    def end_stream(self):
        return bool(self.flags & END_STREAM) and self.type in (DATA, HEADERS)

    @property
    def known(self):
        return self.type in TYPE_NAMES

    def encode(self):
        return encode_frame(self.type, self.flags, self.stream_id, self.payload)

    def describe(self):
        name = TYPE_NAMES.get(self.type, f"UNKNOWN(0x{self.type:02x})")
        flags = f"0x{self.flags:02x}"
        if self.end_stream:
            flags += " END_STREAM"
        return f"{name} stream={self.stream_id} flags={flags} length={len(self.payload)}"


def encode_frame(ftype, flags, stream_id, payload=b""):
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload of {len(payload)} octets does not fit a 16-bit length")
    return HEADER.pack(len(payload), ftype, flags, stream_id) + payload


def goaway(last_stream_id, code, debug=""):
    return Frame(GOAWAY, 0, 0, GOAWAY_FIXED.pack(last_stream_id, code) + debug.encode("utf-8"))


def parse_goaway(frame):
    if frame.stream_id != 0:
        raise ProtocolError(f"GOAWAY on stream {frame.stream_id} (must be 0)")
    if len(frame.payload) < GOAWAY_FIXED.size:
        raise ProtocolError(f"GOAWAY payload is {len(frame.payload)} octets (minimum 8)")
    last, code = GOAWAY_FIXED.unpack_from(frame.payload)
    return last, code, frame.payload[GOAWAY_FIXED.size:].decode("utf-8", "replace")


class FrameReader:
    """Cuts frames out of a TCP byte stream. Leftover bytes stay buffered for the next frame."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = bytearray()

    def _fill(self, deadline):
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FrameTimeout()
            self.sock.settimeout(remaining)
        try:
            data = self.sock.recv(65536)
        except socket.timeout:
            raise FrameTimeout() from None
        if not data:
            return False
        self.buf += data
        return True

    def wait_for_data(self, timeout):
        """Block until at least one byte is buffered. False on timeout; ConnectionClosed on EOF."""
        if self.buf:
            return True
        self.sock.settimeout(timeout)
        try:
            data = self.sock.recv(65536)
        except socket.timeout:
            return False
        if not data:
            raise ConnectionClosed()
        self.buf += data
        return True

    def read_exact(self, n, deadline=None, what="frame"):
        while len(self.buf) < n:
            had = len(self.buf)
            if not self._fill(deadline):
                if had == 0 and what == "frame":
                    raise ConnectionClosed()
                raise ProtocolError(f"connection closed after {had} of {n} octets of a {what}")
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def read_preface(self, deadline=None):
        got = self.read_exact(len(PREFACE), deadline, what="preface")
        if got != PREFACE:
            hint = " (that looks like HTTP/1.x text)" if got[:4] in (b"HTTP", b"GET ", b"HEAD", b"POST") else ""
            raise ProtocolError(f"bad preface {got!r}, expected {PREFACE!r}{hint}")
        return got

    def read_frame(self, deadline=None):
        """One whole frame. The 8-octet header says exactly how many payload octets follow."""
        length, ftype, flags, stream_id = HEADER.unpack(self.read_exact(HEADER_SIZE, deadline))
        payload = self.read_exact(length, deadline, what="frame payload") if length else b""
        return Frame(ftype, flags, stream_id, payload)
