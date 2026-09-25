"""xxd-style hexdump and per-frame trace lines, shared by bcurl -v and bserve -v."""

from __future__ import annotations

from .frames import HEADERS, Frame
from .headers import MalformedHeaders, decode_block


def hexdump(data, prefix="", limit=None):
    lines = []
    shown = data if limit is None else data[:limit]
    for off in range(0, len(shown), 16):
        row = shown[off:off + 16]
        hexes = " ".join(f"{b:02x}" for b in row[:8])
        if len(row) > 8:
            hexes += "  " + " ".join(f"{b:02x}" for b in row[8:])
        text = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in row)
        lines.append(f"{prefix}{off:04x}  {hexes:<49} |{text}|")
    if len(shown) < len(data):
        lines.append(f"{prefix}....  ({len(data) - len(shown)} more octets not shown; -vv shows all)")
    return lines


def trace_frame(direction, frame: Frame, limit=None):
    """'>' for sent, '<' for received. Summary line, hexdump of the whole frame, decoded headers."""
    lines = [f"{direction} {frame.describe()}"]
    if not frame.known:
        lines[0] += "   (unknown type: skipped)" if direction == "<" else "   (grease)"
    lines += hexdump(frame.encode(), f"{direction}   ", limit)
    if frame.type == HEADERS:
        try:
            for field in decode_block(frame.payload):
                how = f"static {field.index}" if field.index else "literal name"
                shown = f"{field.name}: {field.text}"
                lines.append(f"{direction}     {shown:<52} [{how}]")
        except MalformedHeaders as exc:
            lines.append(f"{direction}     (malformed header block: {exc})")
    return lines


def trace_preface(direction, data):
    return [f"{direction} preface ({len(data)} octets)"] + hexdump(data, f"{direction}   ")
