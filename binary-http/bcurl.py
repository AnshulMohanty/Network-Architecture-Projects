#!/usr/bin/env python3
"""bcurl: a BHTTP/1 client (SPEC.md).

    ./bcurl localhost:9000/index.html                  body to stdout
    ./bcurl -v localhost:9000/index.html               + hexdump of every frame to stderr
    ./bcurl localhost:9000/a.html localhost:9000/b.txt two requests, ONE connection
    ./bcurl -I localhost:9000/                         HEAD: print the response headers
    (Windows: .\\bcurl.cmd ...)

Every URL must name the same host:port: bcurl never opens a second connection.
Exit status: 0 all responses < 400 · 4 worst was 4xx · 5 worst was 5xx ·
             1 usage or connection error · 2 protocol error
"""

from __future__ import annotations

import argparse
import os
import random
import re
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bhttp.frames import (DATA, END_STREAM, ERROR_NAMES, GOAWAY, GREASE_TYPES, HEADERS,  # noqa: E402
                          NO_ERROR, PREFACE, PROTOCOL_ERROR, TIMEOUT, ConnectionClosed, Frame, FrameReader,
                          FrameTimeout, ProtocolError, goaway, parse_goaway)
from bhttp.headers import (LITERAL_NAME, MalformedHeaders, decode_block, encode_block, get,  # noqa: E402
                           parse_response_status)
from bhttp.hexdump import trace_frame, trace_preface  # noqa: E402

USER_AGENT = "bcurl/1.0"
DEFAULT_PORT = 9000
EXIT_OK, EXIT_CONNECTION, EXIT_PROTOCOL, EXIT_4XX, EXIT_5XX = 0, 1, 2, 4, 5
PATH_SAFE = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~!$&'()*+,;=:@/?%")


class UsageError(Exception):
    pass


class ServerWentAway(ProtocolError):
    """The server sent GOAWAY: it is already closing, so we just close too (SPEC §6)."""


class ArgParser(argparse.ArgumentParser):
    def error(self, message):   # argparse would exit 2, which here means "protocol error"
        self.print_usage(sys.stderr)
        print(f"bcurl: {message}", file=sys.stderr)
        sys.exit(EXIT_CONNECTION)


def parse_url(url):
    """[bhttp://]host[:port][/path] -> (host, port, authority, path)"""
    rest = url
    scheme = re.match(r"([A-Za-z][A-Za-z0-9+.-]*)://", rest)
    if scheme:
        if scheme.group(1).lower() != "bhttp":
            raise UsageError(f"{scheme.group(1)}:// is not BHTTP/1; write host:port/path or bhttp://host:port/path")
        rest = rest[scheme.end():]
    cut = min([i for i in (rest.find("/"), rest.find("?")) if i >= 0] or [len(rest)])
    authority, path = rest[:cut], rest[cut:] or "/"
    if not path.startswith("/"):
        path = "/" + path
    if authority.startswith("["):
        end = authority.find("]")
        host, port_text = authority[1:end], authority[end + 1:]
        if end < 0 or (port_text and not port_text.startswith(":")):
            raise UsageError(f"bad IPv6 address in {url!r}")
        port_text = port_text[1:]
    else:
        host, _, port_text = authority.partition(":")
    if not host:
        raise UsageError(f"no host in {url!r}")
    if port_text and not (port_text.isdigit() and 0 < int(port_text) < 65536):
        raise UsageError(f"bad port {port_text!r} in {url!r}")
    return host, int(port_text) if port_text else DEFAULT_PORT, authority, quote_path(path)


def quote_path(path):
    return "".join(chr(b) if b in PATH_SAFE else f"%{b:02X}" for b in path.encode("utf-8"))


def request_frames(stream_id, method, path, authority, extra=(), body=None, grease=False):
    """The frames of one request, exactly as they go on the wire."""
    fields = [(":method", method), (":path", path), (":authority", authority)]
    defaults = {"user-agent": USER_AGENT, "accept": "*/*"}
    extra = list(extra)
    names = {name for name, _ in extra}
    fields += [(name, value) for name, value in defaults.items() if name not in names]
    if body is not None:
        extra.append(("content-length", str(len(body))))
    fields += extra
    frames = []
    if grease:
        frames.append(Frame(random.choice(GREASE_TYPES), 0, stream_id, os.urandom(random.randint(0, 8))))
    frames.append(Frame(HEADERS, END_STREAM if body is None else 0, stream_id, encode_block(fields)))
    if body is not None:
        frames.append(Frame(DATA, END_STREAM, stream_id, body))
    return frames


class Client:
    def __init__(self, sock, verbose, out):
        self.sock = sock
        self.reader = FrameReader(sock)
        self.verbose = verbose
        self.out = out
        self.have_server_preface = False
        self.pending = bytearray()   # the preface waits here so it shares a segment with the first request

    def trace(self, lines):
        if self.verbose:
            print("\n".join(lines), file=sys.stderr, flush=True)

    def _limit(self):
        return None if self.verbose > 1 else 256

    def send_preface(self):
        self.pending += PREFACE
        self.trace(trace_preface(">", PREFACE))

    def send(self, frames):
        for frame in frames:
            self.trace(trace_frame(">", frame, self._limit()))
            self.pending += frame.encode()
        self.sock.sendall(self.pending)   # one write per batch
        self.pending.clear()

    def go_away(self, code, reason):
        try:
            self.send([goaway(0, code, reason)])   # a client's last_stream_id is always 0
        except OSError:
            pass

    def read_frame(self):
        if not self.have_server_preface:
            self.trace(trace_preface("<", self.reader.read_preface()))
            self.have_server_preface = True
        frame = self.reader.read_frame()
        self.trace(trace_frame("<", frame, self._limit()))
        return frame

    def read_response(self, stream_id, head, show_headers):
        """Read frames until this stream's END_STREAM. Unknown frame types are skipped."""
        status = fields = None
        received = data_frames = 0
        while True:
            try:
                frame = self.read_frame()
            except ConnectionClosed:
                raise ProtocolError(f"server closed the connection before answering stream {stream_id}")
            if frame.type == GOAWAY:
                last, code, debug = parse_goaway(frame)
                if code != NO_ERROR:
                    raise ServerWentAway(f"server sent GOAWAY {ERROR_NAMES.get(code, code)}: {debug}")
                raise ServerWentAway(f"server went away (last stream {last}) before answering stream {stream_id}: {debug}")
            if frame.type not in (HEADERS, DATA):
                continue   # unknown type: SPEC §2 says skip it, and the reader already consumed it
            if frame.stream_id != stream_id:
                raise ProtocolError(f"got a frame for stream {frame.stream_id} while waiting for stream {stream_id}")
            if frame.type == HEADERS:
                if fields is not None:
                    raise ProtocolError(f"second HEADERS frame on stream {stream_id}")
                try:
                    fields = decode_block(frame.payload)
                    status = parse_response_status(fields)
                except MalformedHeaders as exc:
                    raise ProtocolError(f"malformed response headers: {exc}") from None
                if show_headers:
                    self.out.write(b"".join(f"{f.name}: {f.text}\n".encode() for f in fields) + b"\n")
                    self.out.flush()
            else:
                if fields is None:
                    raise ProtocolError(f"DATA before HEADERS on stream {stream_id}")
                if head:
                    raise ProtocolError(f"DATA frame in the response to HEAD on stream {stream_id}")
                received += len(frame.payload)
                data_frames += 1
                if not show_headers:
                    self.out.write(frame.payload)
                    self.out.flush()
            if frame.end_stream:
                break
        declared = get(fields, "content-length")
        if declared is not None and not head and declared != str(received):
            raise ProtocolError(f"content-length is {declared} but DATA frames carried {received} octets")
        if self.verbose:
            print(f"* stream {stream_id}: status {status}, {received} body octets in {data_frames} DATA frame(s)",
                  file=sys.stderr, flush=True)
        return status


def parse_header_option(text):
    name, colon, value = text.partition(":")
    name = name.strip().lower()
    if not colon or not LITERAL_NAME.fullmatch(name.encode("ascii", "replace")):
        raise UsageError(f"-H wants 'name: value' with a token name, got {text!r}")
    return name, value.strip()


def main(argv=None):
    parser = ArgParser(prog="bcurl", description="BHTTP/1 client (see SPEC.md). Body goes to stdout.")
    parser.add_argument("urls", nargs="+", metavar="URL", help="[bhttp://]host[:port][/path], default port 9000")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="hexdump every frame to stderr (-vv: without truncating payloads)")
    parser.add_argument("-I", "--head", action="store_true", help="send HEAD and print the response headers")
    parser.add_argument("-X", "--request", metavar="METHOD", help="method to send (default GET)")
    parser.add_argument("-H", "--header", action="append", default=[], metavar="'NAME: VALUE'",
                        help="extra request header (repeatable)")
    parser.add_argument("-d", "--data", help="send this string as the request body in a DATA frame")
    parser.add_argument("-p", "--pipeline", action="store_true",
                        help="send every request before reading any response")
    parser.add_argument("--grease", action="store_true",
                        help="send a reserved-type frame before every request (the server must skip it)")
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds to wait for the server")
    args = parser.parse_args(argv)
    out = sys.stdout.buffer

    try:
        targets = [parse_url(u) for u in args.urls]
        extra = [parse_header_option(h) for h in args.header]
    except UsageError as exc:
        print(f"bcurl: {exc}", file=sys.stderr)
        return EXIT_CONNECTION
    endpoints = {(host, port) for host, port, _, _ in targets}
    if len(endpoints) > 1:
        print("bcurl: every URL must use the same host:port - bcurl never opens a second connection",
              file=sys.stderr)
        return EXIT_CONNECTION
    host, port = endpoints.pop()
    method = "HEAD" if args.head else (args.request or ("POST" if args.data is not None else "GET"))
    body = args.data.encode("utf-8") if args.data is not None else None

    try:
        sock = socket.create_connection((host, port), timeout=args.timeout)
    except OSError as exc:
        print(f"bcurl: cannot connect to {host} port {port}: {exc}", file=sys.stderr)
        return EXIT_CONNECTION
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    client = Client(sock, args.verbose, out)
    peer = sock.getpeername()[0]
    if args.verbose:
        print(f"* connected to {host} ({peer}) port {port}; {len(targets)} request(s), 1 connection",
              file=sys.stderr, flush=True)

    worst = 0
    try:
        client.send_preface()
        batches = [request_frames(i, method, path, authority, extra, body, args.grease)
                   for i, (_, _, authority, path) in enumerate(targets, start=1)]
        if args.pipeline:
            client.send([f for batch in batches for f in batch])
        for stream_id, batch in enumerate(batches, start=1):
            if not args.pipeline:
                client.send(batch)
            status = client.read_response(stream_id, method == "HEAD", args.head)
            if status >= 400:
                print(f"bcurl: {targets[stream_id - 1][3]} -> {status}", file=sys.stderr, flush=True)
            worst = max(worst, status)
    except ProtocolError as exc:
        print(f"bcurl: protocol error: {exc}", file=sys.stderr)
        if not isinstance(exc, ServerWentAway):
            client.go_away(PROTOCOL_ERROR, str(exc))   # SPEC §3: GOAWAY before an error close
        return EXIT_PROTOCOL
    except (FrameTimeout, socket.timeout):
        print(f"bcurl: no answer from {host} port {port} within {args.timeout:g}s", file=sys.stderr)
        client.go_away(TIMEOUT, f"no answer within {args.timeout:g}s")
        return EXIT_CONNECTION
    except BrokenPipeError:   # e.g. piped into `head`
        return EXIT_OK
    except OSError as exc:
        print(f"bcurl: connection error: {exc}", file=sys.stderr)
        return EXIT_CONNECTION
    finally:
        sock.close()
        if args.verbose:
            print("* connection closed (1 TCP connection used)", file=sys.stderr, flush=True)
    return EXIT_5XX if worst >= 500 else EXIT_4XX if worst >= 400 else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
