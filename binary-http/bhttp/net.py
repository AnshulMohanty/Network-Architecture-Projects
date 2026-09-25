"""Listening on every address of a name, and closing without an RST."""

from __future__ import annotations

import socket
import time


def listen_all(host, port, log=lambda msg: None):
    """Listen on every address `host` resolves to: 'localhost' is ::1 *and* 127.0.0.1.

    Returns (sockets, port). With port 0 the first bind picks the port and the rest reuse it.
    """
    infos = socket.getaddrinfo(host or None, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
    listeners, seen, last_error = [], set(), None
    for family, kind, proto, _, address in infos:
        if address[0] in seen:
            continue
        seen.add(address[0])
        sock = None
        try:
            sock = socket.socket(family, kind, proto)
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)   # Windows
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock.bind(address[:1] + (port,) + address[2:])
            sock.listen(128)
        except OSError as exc:
            if sock is not None:
                sock.close()
            last_error = exc
            log(f"warning: cannot listen on {address[0]} port {port}: {exc}")
            continue
        port = sock.getsockname()[1]
        listeners.append(sock)
    if not listeners:
        raise last_error or OSError(f"could not listen on {host}:{port}")
    return listeners, port


def close_gracefully(sock, linger=1.0):
    """FIN first, drain what the peer is still sending, then close, so unread input can't trigger an RST."""
    try:
        sock.shutdown(socket.SHUT_WR)
        sock.settimeout(0.5)
        until = time.monotonic() + linger
        while time.monotonic() < until and sock.recv(65536):
            pass
    except OSError:
        pass
    finally:
        sock.close()
