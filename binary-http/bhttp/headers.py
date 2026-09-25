"""BHTTP/1 header blocks (SPEC.md §4): static name table + length-prefixed literals.

    field = index:u8  [ name_len:u16  name ]  value_len:u16  value
              index 0     -> literal name follows
              index 1..10 -> name from STATIC_TABLE
"""

from __future__ import annotations

import re
import struct
from typing import NamedTuple

STATIC_TABLE = (None, ":method", ":path", ":authority", ":status", "content-type",
                "content-length", "date", "server", "user-agent", "accept")
STATIC_INDEX = {name: i for i, name in enumerate(STATIC_TABLE) if name}
U16 = struct.Struct("!H")

LITERAL_NAME = re.compile(rb"[a-z0-9!#$%&'*+\-.^_`|~]+")
FORBIDDEN_IN_VALUE = (0x00, 0x0A, 0x0D)
REQUEST_PSEUDO = (":method", ":path", ":authority")
RESPONSE_PSEUDO = (":status",)
METHOD = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")


class MalformedHeaders(ValueError):
    """The header block breaks a §4 validity rule. For a request this means 400, not a dead connection."""


class Field(NamedTuple):
    name: str
    value: bytes
    index: int     # static-table index, or 0 if the name was sent as a literal

    @property
    def text(self):
        return self.value.decode("utf-8", "replace")


def encode_block(fields):
    """[(name, value)] -> payload. Names in the table are sent as their index, others as literals."""
    out = bytearray()
    for name, value in fields:
        value = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        if any(b in value for b in FORBIDDEN_IN_VALUE):
            raise ValueError(f"value of {name} contains NUL, CR or LF")
        index = STATIC_INDEX.get(name, 0)
        out.append(index)
        if index == 0:
            raw = name.encode("ascii")
            if not LITERAL_NAME.fullmatch(raw):
                raise ValueError(f"{name!r} is not a lowercase token (pseudo-headers must be indexed)")
            out += U16.pack(len(raw)) + raw
        out += U16.pack(len(value)) + value
    return bytes(out)


def decode_block(payload):
    """payload -> [Field]. Raises MalformedHeaders on any §4 violation of the encoding itself."""
    fields = []
    pos = 0

    def take(n, what):
        nonlocal pos
        if pos + n > len(payload):
            raise MalformedHeaders(f"{what} runs past the end of the header block "
                                   f"(needs {n} octets at offset {pos}, block is {len(payload)})")
        chunk = payload[pos:pos + n]
        pos += n
        return chunk

    while pos < len(payload):
        index = take(1, "index")[0]
        if index == 0:
            (name_len,) = U16.unpack(take(2, "name length"))
            raw = take(name_len, "name")
            if not raw:
                raise MalformedHeaders("empty literal name")
            if raw.startswith(b":"):
                raise MalformedHeaders(f"literal name {raw!r} starts with ':' (pseudo-headers are indexed only)")
            if not LITERAL_NAME.fullmatch(raw):
                raise MalformedHeaders(f"literal name {raw!r} is not a lowercase token")
            name = raw.decode("ascii")
        elif index < len(STATIC_TABLE):
            name = STATIC_TABLE[index]
        else:
            raise MalformedHeaders(f"header index {index} is not in the static table (1-10)")
        (value_len,) = U16.unpack(take(2, "value length"))
        value = take(value_len, f"value of {name}")
        if any(b in value for b in FORBIDDEN_IN_VALUE):
            raise MalformedHeaders(f"value of {name} contains NUL, CR or LF")
        fields.append(Field(name, bytes(value), index))
    return fields


def _pseudo(fields, allowed, kind):
    pseudo = {}
    seen_regular = False
    for f in fields:
        if f.name.startswith(":"):
            if seen_regular:
                raise MalformedHeaders(f"pseudo-header {f.name} after a regular header")
            if f.name not in allowed:
                raise MalformedHeaders(f"{f.name} is not allowed in a {kind}")
            if f.name in pseudo:
                raise MalformedHeaders(f"duplicate {f.name}")
            pseudo[f.name] = f.value.decode("latin-1")
        else:
            seen_regular = True
    for name in allowed:
        if name not in pseudo:
            raise MalformedHeaders(f"{kind} has no {name}")
    return pseudo


class Request(NamedTuple):
    method: str
    path: str
    authority: str
    fields: list


def parse_request(fields):
    p = _pseudo(fields, REQUEST_PSEUDO, "request")
    if not METHOD.fullmatch(p[":method"]):
        raise MalformedHeaders(f":method {p[':method']!r} is not a token")
    if not p[":path"].startswith("/"):
        raise MalformedHeaders(f":path {p[':path']!r} does not start with '/'")
    return Request(p[":method"], p[":path"], p[":authority"], fields)


def parse_response_status(fields):
    status = _pseudo(fields, RESPONSE_PSEUDO, "response")[":status"]
    if not re.fullmatch(r"[0-9]{3}", status):
        raise MalformedHeaders(f":status {status!r} is not three digits")
    return int(status)


def get(fields, name):
    for f in fields:
        if f.name == name:
            return f.value.decode("latin-1")
    return None
