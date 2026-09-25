#!/usr/bin/env python3
"""Replay the marker's test: one socket, every request.

    python marker.py                 # one request at a time, like the slide
    python marker.py --pipeline      # all six in a single send(), answers read back in order
    python marker.py --full          # also the rest of the task list (div 9/3, a=x, no Host)

Run the server first:  python server.py
"""

import argparse
import socket

MARKER_REQUESTS = [
    ("GET", "/add?a=2&b=3", True),
    ("GET", "/sub?a=10&b=4", True),
    ("GET", "/mul?a=6&b=7", True),
    ("GET", "/div?a=1&b=0", True),
    ("GET", "/pow?a=2&b=8", True),
    # A POST with a body: the 7 body bytes must be consumed, not parsed as the next request.
    ("POST", "/add", True),
]
EXTRA_REQUESTS = [
    ("GET", "/div?a=9&b=3", True),
    ("GET", "/add?a=x&b=3", True),
    ("GET", "/add", False),   # no Host header
]


def build(method, target, with_host):
    lines = [f"{method} {target} HTTP/1.1"]
    if with_host:
        lines.append("Host: localhost")
    body = b""
    if method == "POST":
        body = b"a=2&b=3"
        lines += ["Content-Type: application/x-www-form-urlencoded", f"Content-Length: {len(body)}"]
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


class ResponseReader:
    """Reads responses exactly by Content-Length, keeping leftover bytes for the next one."""

    def __init__(self, sock):
        self.sock, self.buf = sock, b""

    def _more(self):
        data = self.sock.recv(65536)
        if not data:
            raise ConnectionError("server closed the connection")
        self.buf += data

    def read(self):
        while b"\r\n\r\n" not in self.buf:
            self._more()
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        status = int(lines[0].split()[1])
        headers = {k.strip().lower(): v.strip() for k, v in (l.split(":", 1) for l in lines[1:])}
        length = int(headers.get("content-length", "0"))
        while len(self.buf) < length:
            self._more()
        body, self.buf = self.buf[:length], self.buf[length:]
        return status, headers, body.decode()


def still_open(sock):
    """Peek without blocking: b'' means the server sent FIN; 'would block' means still open."""
    sock.setblocking(False)
    try:
        return sock.recv(1, socket.MSG_PEEK) != b""
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        sock.setblocking(True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--pipeline", action="store_true", help="send every request in one send()")
    parser.add_argument("--full", action="store_true", help="also run the rest of the task list")
    args = parser.parse_args()

    requests = MARKER_REQUESTS + (EXTRA_REQUESTS if args.full else [])
    s = socket.create_connection((args.host, args.port))   # the one and only TCP handshake
    local = s.getsockname()
    reader = ResponseReader(s)
    print(f"s = socket.create_connection(({args.host!r}, {args.port}))   # local port {local[1]}\n")

    if args.pipeline:
        s.sendall(b"".join(build(m, t, h) for m, t, h in requests))
        answers = [reader.read() for _ in requests]
    else:
        answers = []
        for m, t, h in requests:
            s.sendall(build(m, t, h))
            answers.append(reader.read())

    for (method, target, with_host), (status, headers, body) in zip(requests, answers):
        label = f"{method} {target}" + ("" if with_host else "  (no Host)")
        shown = body if status == 200 else f"({body})"
        print(f"  {label:<26} -> {status}   {shown}")

    open_now = still_open(s)
    print(f"\n  socket still open: {open_now}")
    print(f"  1 TCP handshake, {len(answers)} responses   (same local port {s.getsockname()[1]} throughout)")
    s.close()
    raise SystemExit(0 if open_now else 1)


if __name__ == "__main__":
    main()
