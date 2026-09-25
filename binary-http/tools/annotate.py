#!/usr/bin/env python3
"""Capture one real BHTTP/1 exchange and annotate every octet.

    python tools/annotate.py localhost:9000/index.html            print the annotated dump
    python tools/annotate.py localhost:9000/index.html --save cap write cap.request.bin / cap.response.bin
    python tools/annotate.py --from cap                           annotate a saved capture

The request is built by bcurl's own request_frames(), so these are exactly the bytes bcurl sends.
The response bytes are recorded exactly as recv() returned them, with nothing re-encoded.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from bcurl import parse_url, request_frames  # noqa: E402
from bhttp.frames import (DATA, END_STREAM, ERROR_NAMES, GOAWAY, HEADER, HEADERS, PREFACE,  # noqa: E402
                          TYPE_NAMES, FrameReader)
from bhttp.headers import STATIC_TABLE  # noqa: E402

WIDTH = 8   # octets per row keeps the hex column narrow enough to print


class Recorder:
    """Wraps a socket and remembers every octet recv() hands back."""

    def __init__(self, sock):
        self.sock, self.seen = sock, bytearray()

    def recv(self, n):
        data = self.sock.recv(n)
        self.seen += data
        return data

    def settimeout(self, t):
        self.sock.settimeout(t)


def capture(url, method="GET"):
    host, port, authority, path = parse_url(url)
    sent = PREFACE + b"".join(f.encode() for f in request_frames(1, method, path, authority))
    sock = socket.create_connection((host, port), timeout=10)
    try:
        sock.sendall(sent)
        rec = Recorder(sock)
        reader = FrameReader(rec)
        reader.read_preface()
        while True:
            frame = reader.read_frame()
            if frame.type in (DATA, HEADERS) and frame.flags & END_STREAM:
                break
        # Everything recv() returned must have been consumed: nothing past END_STREAM.
        received = bytes(rec.seen[:len(rec.seen) - len(reader.buf)])
    finally:
        sock.close()
    return sent, received


def rows_for(data):
    """Yield (offset, octets, label) for every field in a byte stream that starts with a preface."""
    yield 0, data[:8], f'preface      "BHTTP/1\\n": magic + protocol version \'1\''
    pos, n = 8, 0
    while pos < len(data):
        length, ftype, flags, sid = HEADER.unpack_from(data, pos)
        n += 1
        name = TYPE_NAMES.get(ftype, f"unknown 0x{ftype:02x}")
        yield None, b"", f"── frame {n}: {name}, stream {sid}, {length}-octet payload " + "─" * 12
        yield pos, data[pos:pos + 2], f"length       = {length} (payload octets after this 8-octet header)"
        yield pos + 2, data[pos + 2:pos + 3], f"type         = 0x{ftype:02x} {name}"
        if ftype in (DATA, HEADERS):
            meaning = "END_STREAM: last frame of this message" if flags & END_STREAM else "none: more frames follow"
        else:
            meaning = "(none defined)"
        yield pos + 3, data[pos + 3:pos + 4], f"flags        = 0x{flags:02x} {meaning}"
        yield pos + 4, data[pos + 4:pos + 8], f"stream id    = {sid}" + (" (the connection)" if sid == 0 else "")
        start = pos + 8
        payload = data[start:start + length]
        if ftype == HEADERS:
            yield from header_rows(payload, start)
        elif ftype == DATA:
            yield from chunk_rows(payload, start, lambda c: "body         " + repr(c.decode("latin-1")))
        elif ftype == GOAWAY:
            yield start, payload[:4], f"last stream  = {int.from_bytes(payload[:4], 'big')}"
            code = int.from_bytes(payload[4:8], "big")
            yield start + 4, payload[4:8], f"error code   = {code} {ERROR_NAMES.get(code, '?')}"
            yield from chunk_rows(payload[8:], start + 8, lambda c: "debug        " + repr(c.decode()))
        else:
            yield from chunk_rows(payload, start, lambda c: "(unknown type: skipped unread)")
        pos = start + length


def header_rows(block, base):
    p = 0
    while p < len(block):
        index = block[p]
        if index:
            yield base + p, block[p:p + 1], f"index {index:<6} -> name {STATIC_TABLE[index]!r} from the static table"
            p += 1
        else:
            yield base + p, block[p:p + 1], "index 0      -> literal name follows"
            name_len = int.from_bytes(block[p + 1:p + 3], "big")
            yield base + p + 1, block[p + 1:p + 3], f"name length  = {name_len}"
            name = block[p + 3:p + 3 + name_len]
            yield from chunk_rows(name, base + p + 3, lambda c, n=name: f"name         {n.decode()!r}", first_only=True)
            p += 3 + name_len
        value_len = int.from_bytes(block[p:p + 2], "big")
        yield base + p, block[p:p + 2], f"value length = {value_len}"
        value = block[p + 2:p + 2 + value_len]
        yield from chunk_rows(value, base + p + 2, lambda c, v=value: f"value        {v.decode('latin-1')!r}",
                              first_only=True)
        p += 2 + value_len


def chunk_rows(data, base, label, first_only=False):
    for i in range(0, len(data), WIDTH):
        chunk = data[i:i + WIDTH]
        yield base + i, chunk, (label(chunk) if i == 0 or not first_only else "")


def render(data):
    lines = ["offset  octets                   field", "------  -----------------------  " + "-" * 56]
    for offset, octets, label in rows_for(data):
        if offset is None:
            lines.append(f"        {label}")
        else:
            lines.append(f"{offset:04x}    {octets.hex(' '):<23}  {label}".rstrip())
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", nargs="?", default="localhost:9000/index.html")
    parser.add_argument("-X", dest="method", default="GET", help="method to send (default GET)")
    parser.add_argument("--save", metavar="PREFIX", help="also write PREFIX.request.bin and PREFIX.response.bin")
    parser.add_argument("--from", dest="source", metavar="PREFIX", help="annotate a saved capture instead")
    args = parser.parse_args()
    if args.source:
        with open(args.source + ".request.bin", "rb") as f:
            sent = f.read()
        with open(args.source + ".response.bin", "rb") as f:
            received = f.read()
    else:
        sent, received = capture(args.url, args.method)
    if args.save:
        for suffix, data in ((".request.bin", sent), (".response.bin", received)):
            with open(args.save + suffix, "wb") as f:
                f.write(data)
    sys.stdout.reconfigure(encoding="utf-8")
    print(f"### Client -> server: {len(sent)} octets\n\n```text\n{render(sent)}\n```\n")
    print(f"### Server -> client: {len(received)} octets\n\n```text\n{render(received)}\n```")


if __name__ == "__main__":
    main()
