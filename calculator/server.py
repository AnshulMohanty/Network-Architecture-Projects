#!/usr/bin/env python3
"""A calculator that stays on the line: HTTP/1.1 keep-alive over a raw socket.

    python server.py                          # listens on localhost:8080
    python server.py --port 8081 --idle-timeout 30

    GET /add|sub|mul|div?a=<n>&b=<n>   ->   200, the result as text/plain
    GET /                              ->   200, web/index.html (a page that shows the connection being reused)

No http.server, no urllib: every byte goes through socket.recv() and socket.sendall().

The arithmetic is the easy part. The hard part is framing. The connection stays open, so each
request has to be cut out of the byte stream exactly. The head ends at the first empty line. The body
is exactly Content-Length bytes, or a chunked body up to its zero-size chunk. Whatever arrives after
that belongs to the *next* request and must stay in the buffer.
"""

from __future__ import annotations

import argparse
import itertools
import os
import re
import socket
import sys
import threading
import time
from email.utils import formatdate
from fractions import Fraction

MAX_LINE = 8 * 1024             # request line, one header line, one chunk-size line
MAX_HEAD = 16 * 1024            # request line + every header line
MAX_HEADERS = 100
MAX_BODY = 1024 * 1024          # bodies are never used, but they must be read to find the next request
MAX_OPERAND = 100               # characters per number: arithmetic, not a CPU-burning contest
DEFAULT_IDLE_TIMEOUT = 60.0     # quiet time allowed *between* requests on a kept-alive connection
DEFAULT_REQUEST_TIMEOUT = 10.0  # once a request has started, all of it must arrive within this
DEFAULT_MAX_CONNECTIONS = 64

TEXT_PLAIN = "text/plain; charset=utf-8"
TEXT_HTML = "text/html; charset=utf-8"
INDEX_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html")

REASONS = {
    100: "Continue", 200: "OK", 400: "Bad Request", 404: "Not Found",
    405: "Method Not Allowed", 408: "Request Timeout", 413: "Content Too Large",
    414: "URI Too Long", 431: "Request Header Fields Too Large",
    500: "Internal Server Error", 501: "Not Implemented", 503: "Service Unavailable",
    505: "HTTP Version Not Supported",
}
KNOWN_METHODS = {"GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "CONNECT"}
ALLOWED_METHODS = ("GET", "HEAD")

TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
VERSION = re.compile(r"HTTP/([0-9])\.([0-9])")
NUMBER = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?")   # [0-9], not \d: \d also matches '٣'
CHUNK_SIZE = re.compile(rb"[0-9A-Fa-f]{1,16}")
CONTENT_LENGTH = re.compile(r"[0-9]{1,16}")


class HTTPError(Exception):
    """A request that gets an error status instead of an answer.

    close=False  the request was framed correctly, so we know where the next one starts and the
                 connection stays open (bad number, division by zero, 404, 405, missing Host).
    close=True   the framing itself is broken, so nobody knows where the next request starts.
                 The only safe move is to answer and hang up.
    """

    def __init__(self, status, message, close=False, headers=()):
        super().__init__(message)
        self.status = status
        self.message = message
        self.close = close
        self.headers = list(headers)


class PeerClosed(Exception):
    """recv() returned b'': the client closed its side of the connection."""


class Reader:
    """The bytes that have arrived on one connection but are not consumed yet.

    recv() returns whatever TCP happens to have: half a request, or three of them. So nothing here
    ever assumes one recv() is one request. We take exactly what the current request needs, and
    the rest stays in self.buf for the next one. That is also why pipelining works for free.
    """

    def __init__(self, sock):
        self.sock = sock
        self.buf = bytearray()
        self.deadline = None  # monotonic time by which the current request must be complete

    def fill(self):
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("request timeout")
            self.sock.settimeout(remaining)
        data = self.sock.recv(65536)
        if not data:
            raise PeerClosed()
        self.buf += data

    def read_line(self, limit, status_if_too_long=400):
        """One line, without its CRLF. A bare LF is accepted as well (RFC 9112 §2.2)."""
        scanned = 0
        while True:
            end = self.buf.find(b"\n", scanned)
            if end >= 0:
                line = bytes(self.buf[:end])
                del self.buf[:end + 1]
                if line.endswith(b"\r"):
                    line = line[:-1]
                if len(line) > limit:
                    raise HTTPError(status_if_too_long, "line too long", close=True)
                return line
            if len(self.buf) > limit:
                raise HTTPError(status_if_too_long, "line too long", close=True)
            scanned = len(self.buf)
            self.fill()

    def discard(self, n):
        """Consume exactly n body bytes. Byte n+1 belongs to the next request, so it is left alone."""
        while n > 0:
            if not self.buf:
                self.fill()
            take = min(n, len(self.buf))
            del self.buf[:take]
            n -= take

    def skip_blank_lines(self):
        """Drop stray CRLFs between requests (RFC 9112 §2.2: SHOULD ignore at least one)."""
        i = 0
        while i < len(self.buf) and self.buf[i] in b"\r\n":
            i += 1
        del self.buf[:i]


class Request:
    def __init__(self, method, target, version, headers):
        self.method = method
        self.target = target
        self.version = version    # (1, 1) or (1, 0)
        self.headers = headers    # [(lowercase name, value)], in arrival order

    def get_all(self, name):
        return [value for key, value in self.headers if key == name]

    def connection_tokens(self):
        return {t.strip().lower() for v in self.get_all("connection") for t in v.split(",") if t.strip()}

    @property
    def keep_alive(self):
        tokens = self.connection_tokens()
        if self.version >= (1, 1):
            return "close" not in tokens       # HTTP/1.1: persistent unless told otherwise
        return "keep-alive" in tokens          # HTTP/1.0: close unless asked to keep it


# --------------------------------------------------------------------------------------------
# Reading one request off the wire
# --------------------------------------------------------------------------------------------

def read_request(reader):
    line = reader.read_line(MAX_LINE, 414)
    head_size = len(line)
    parts = line.split(b" ")
    if len(parts) != 3 or not all(parts):
        raise HTTPError(400, "malformed request line", close=True)
    try:
        method, target, version = (p.decode("ascii") for p in parts)
    except UnicodeDecodeError:
        raise HTTPError(400, "request line is not ASCII", close=True)
    if not TOKEN.fullmatch(method):
        raise HTTPError(400, "malformed method", close=True)
    if any(ch < "!" or ch == "\x7f" for ch in target):
        raise HTTPError(400, "control character in request-target", close=True)
    match = VERSION.fullmatch(version)
    if not match:
        raise HTTPError(400, "malformed HTTP version", close=True)
    if match.group(1) != "1":
        raise HTTPError(505, f"{version} is not supported; this server speaks HTTP/1.1", close=True)

    headers = []
    while True:
        line = reader.read_line(MAX_LINE, 431)
        if not line:
            break
        head_size += len(line) + 2
        if head_size > MAX_HEAD or len(headers) >= MAX_HEADERS:
            raise HTTPError(431, "request header section too large", close=True)
        if line[:1] in (b" ", b"\t"):
            raise HTTPError(400, "obsolete line folding is not accepted", close=True)
        name, colon, value = line.partition(b":")
        # A space before the colon ("Host : x") is not a token character, so it is rejected here
        # as RFC 9112 §5.1 requires.
        name = name.decode("latin-1")
        if not colon or not TOKEN.fullmatch(name):
            raise HTTPError(400, "malformed header line", close=True)
        value = value.decode("latin-1").strip(" \t")
        if "\r" in value or "\x00" in value:
            raise HTTPError(400, "control character in header value", close=True)
        headers.append((name.lower(), value))

    request = Request(method, target, (int(match.group(1)), int(match.group(2))), headers)
    read_body(reader, request)
    return request


def read_body(reader, request):
    """Consume the body, however it is framed, so the stream is positioned at the next request."""
    transfer_encoding = request.get_all("transfer-encoding")
    content_length = request.get_all("content-length")

    if transfer_encoding:
        if request.version < (1, 1):
            raise HTTPError(400, "Transfer-Encoding in an HTTP/1.0 request", close=True)
        if content_length:
            # Two framings in one message is how request smuggling starts: refuse it.
            raise HTTPError(400, "both Content-Length and Transfer-Encoding", close=True)
        codings = [c.strip().lower() for c in ",".join(transfer_encoding).split(",")]
        if codings[-1] != "chunked":
            raise HTTPError(400, "chunked must be the final transfer coding", close=True)
        if codings != ["chunked"]:
            raise HTTPError(501, f"transfer coding {codings[0]!r} is not implemented", close=True)
        send_continue_if_expected(reader, request)
        read_chunked(reader)
    elif content_length:
        values = {v.strip() for v in ",".join(content_length).split(",")}
        if len(values) != 1:
            raise HTTPError(400, "conflicting Content-Length values", close=True)
        value = values.pop()
        if not CONTENT_LENGTH.fullmatch(value):
            raise HTTPError(400, "invalid Content-Length", close=True)
        length = int(value)
        if length > MAX_BODY:
            raise HTTPError(413, f"body larger than {MAX_BODY} bytes", close=True)
        if length:
            send_continue_if_expected(reader, request)
            reader.discard(length)
    # Neither header: a request has no body (RFC 9112 §6.3). Do not wait for one.


def read_chunked(reader):
    """chunk = size-in-hex [;ext] CRLF data CRLF ... 0 CRLF [trailers] CRLF"""
    total = 0
    while True:
        line = reader.read_line(MAX_LINE)
        size_text = line.split(b";", 1)[0].strip(b" \t")
        if not CHUNK_SIZE.fullmatch(size_text):
            raise HTTPError(400, "invalid chunk size", close=True)
        size = int(size_text, 16)
        if size == 0:
            break
        total += size
        if total > MAX_BODY:
            raise HTTPError(413, f"body larger than {MAX_BODY} bytes", close=True)
        reader.discard(size)
        if reader.read_line(MAX_LINE) != b"":
            raise HTTPError(400, "chunk data longer than its declared size", close=True)
    for _ in range(MAX_HEADERS):
        line = reader.read_line(MAX_LINE, 431)
        if not line:
            return
        if b":" not in line:
            raise HTTPError(400, "malformed trailer field", close=True)
    raise HTTPError(431, "too many trailer fields", close=True)


def send_continue_if_expected(reader, request):
    # curl sends "Expect: 100-continue" before large bodies, then waits up to a second for this.
    if request.version >= (1, 1) and any("100-continue" in v.lower() for v in request.get_all("expect")):
        reader.sock.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")


# --------------------------------------------------------------------------------------------
# The calculator
# --------------------------------------------------------------------------------------------

def _divide(a, b):
    if b == 0:
        raise HTTPError(400, "division by zero")
    return a / b


OPERATIONS = {
    "/add": lambda a, b: a + b,
    "/sub": lambda a, b: a - b,
    "/mul": lambda a, b: a * b,
    "/div": _divide,
}


def answer(request):
    """Return (body, content type) for a well-framed request, or raise a keep-alive HTTPError."""
    if request.version >= (1, 1):
        hosts = request.get_all("host")
        if not hosts:
            raise HTTPError(400, "missing Host header (required in HTTP/1.1)")
        if len(hosts) > 1:
            raise HTTPError(400, "more than one Host header")
    if request.method not in KNOWN_METHODS:
        raise HTTPError(501, f"method {request.method} is not implemented")

    path, query = split_target(request.target)
    operation = OPERATIONS.get(path)
    if operation is None and path != "/":
        raise HTTPError(404, f"no such operation {path}; try /add /sub /mul /div")
    if request.method not in ALLOWED_METHODS:
        raise HTTPError(405, f"{request.method} is not allowed on {path}; use GET",
                        headers=[("Allow", ", ".join(ALLOWED_METHODS))])
    if operation is None:
        return index_page(), TEXT_HTML

    params = parse_query(query)
    a, b = operand(params, "a"), operand(params, "b")
    return format_number(operation(a, b)), TEXT_PLAIN


def index_page():
    """The page served at /. Its path is fixed, so no part of a request-target reaches the file
    system: there is no static file serving here, and nothing to traverse."""
    try:
        with open(INDEX_HTML, "rb") as f:
            return f.read().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # Must not escape as an OSError: _serve_connection treats those as a dead socket.
        raise HTTPError(500, f"cannot read web/index.html ({exc.__class__.__name__})")


def split_target(target):
    if target.startswith("/"):
        pass
    elif re.match(r"(?i)https?://", target):
        # absolute-form (RFC 9112 §3.2.2): servers MUST accept it. Keep only the path and query.
        rest = target.split("://", 1)[1]
        cut = min([i for i in (rest.find("/"), rest.find("?")) if i >= 0] or [len(rest)])
        target = rest[cut:]
        target = "/" + target if not target.startswith("/") else target
    else:
        raise HTTPError(400, "request-target must start with '/'")
    path, _, query = target.partition("?")
    return percent_decode(path, plus_is_space=False), query


def parse_query(query):
    params = {}
    for pair in query.split("&"):
        if pair:
            name, _, value = pair.partition("=")
            params.setdefault(percent_decode(name), []).append(percent_decode(value))
    return params


def percent_decode(text, plus_is_space=True):
    raw = text.encode("ascii")
    out = bytearray()
    i = 0
    while i < len(raw):
        byte = raw[i]
        if byte == 0x25:  # '%'
            pair = raw[i + 1:i + 3]
            if len(pair) != 2 or not all(c in b"0123456789abcdefABCDEF" for c in pair):
                raise HTTPError(400, "bad percent-encoding in request-target")
            out.append(int(pair, 16))
            i += 3
        else:
            out.append(0x20 if byte == 0x2B and plus_is_space else byte)
            i += 1
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPError(400, "request-target is not valid UTF-8")


def operand(params, name):
    values = params.get(name)
    if not values:
        raise HTTPError(400, f"missing parameter {name}")
    if len(values) > 1:
        raise HTTPError(400, f"parameter {name} given more than once")
    text = values[0]
    if len(text) > MAX_OPERAND:
        raise HTTPError(400, f"parameter {name} is longer than {MAX_OPERAND} characters")
    # Stricter than float(): no 'nan', 'inf', '1_000', ' 5' or '1e9'.
    if not NUMBER.fullmatch(text):
        raise HTTPError(400, f"parameter {name} is not a number: {text!r}")
    return Fraction(text)   # exact, so 0.1 + 0.2 is 0.3 and big integers stay exact


def format_number(value):
    if value.denominator == 1:
        return str(value.numerator)   # 9/3 -> "3", not "3.0"
    return repr(float(value))         # 7/2 -> "3.5", 1/3 -> "0.3333333333333333"


# --------------------------------------------------------------------------------------------
# Writing responses
# --------------------------------------------------------------------------------------------

def build_response(status, body, keep_alive, idle_timeout, extra_headers=(), head_only=False,
                   content_type=TEXT_PLAIN):
    """The whole response as one byte string, so it goes out in one sendall()."""
    body = body.encode("utf-8")
    lines = [
        f"HTTP/1.1 {status} {REASONS[status]}",
        f"Date: {formatdate(usegmt=True)}",
        "Server: calc/1.0",
        f"Content-Type: {content_type}",
        f"Content-Length: {len(body)}",
    ]
    lines += [f"{name}: {value}" for name, value in extra_headers]
    if keep_alive:
        lines.append("Connection: keep-alive")
        if idle_timeout:
            lines.append(f"Keep-Alive: timeout={int(idle_timeout)}")
    else:
        lines.append("Connection: close")
    head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
    return head if head_only else head + body


def close_gracefully(sock):
    """Send FIN, then briefly drain what the client is still sending, then close.

    Closing a socket that still has unread input makes the kernel send an RST, and an RST can
    destroy a response the client has not read yet (RFC 9112 §9.6).
    """
    try:
        sock.shutdown(socket.SHUT_WR)
        sock.settimeout(0.5)
        until = time.monotonic() + 1.0
        while time.monotonic() < until and sock.recv(65536):
            pass
    except OSError:
        pass
    finally:
        sock.close()


# --------------------------------------------------------------------------------------------
# The server
# --------------------------------------------------------------------------------------------

class CalculatorServer:
    def __init__(self, host="localhost", port=8080, idle_timeout=DEFAULT_IDLE_TIMEOUT,
                 request_timeout=DEFAULT_REQUEST_TIMEOUT, max_connections=DEFAULT_MAX_CONNECTIONS,
                 log=True):
        self.host = host
        self.port = port
        self.idle_timeout = idle_timeout or None   # 0 means "never time out"
        self.request_timeout = request_timeout
        self.log_enabled = log
        self.listeners = []
        self.connections_accepted = 0
        self._ids = itertools.count(1)
        self._slots = threading.BoundedSemaphore(max_connections)
        self._stopping = threading.Event()
        self._lock = threading.Lock()

    def log(self, message):
        if self.log_enabled:
            print(message, file=sys.stderr, flush=True)

    def start(self):
        """Listen on every address the host name resolves to.

        'localhost' is ::1 *and* 127.0.0.1, and most systems try ::1 first. A server on 127.0.0.1
        alone makes such a client eat a refused connection (about 2 s on Windows) before it
        falls back.
        """
        infos = socket.getaddrinfo(self.host or None, self.port, type=socket.SOCK_STREAM,
                                   flags=socket.AI_PASSIVE)
        seen = set()
        last_error = None
        for family, kind, proto, _, address in infos:
            if address[0] in seen:
                continue
            seen.add(address[0])
            sock = None
            try:
                sock = socket.socket(family, kind, proto)
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    # Windows: SO_REUSEADDR there would let another process steal the port.
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                else:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if family == socket.AF_INET6:
                    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                sock.bind(address[:1] + (self.port,) + address[2:])
                sock.listen(128)
            except OSError as exc:
                if sock is not None:
                    sock.close()
                last_error = exc
                self.log(f"warning: cannot listen on {address[0]} port {self.port}: {exc}")
                continue
            self.port = sock.getsockname()[1]   # port 0 -> the same ephemeral port for every family
            self.listeners.append(sock)
        if not self.listeners:
            raise last_error or OSError(f"could not listen on {self.host}:{self.port}")
        for sock in self.listeners:
            threading.Thread(target=self._accept_loop, args=(sock,), daemon=True).start()
        return self

    def addresses(self):
        return [s.getsockname()[0] for s in self.listeners]

    def stop(self):
        self._stopping.set()
        for sock in self.listeners:
            sock.close()

    def _accept_loop(self, listener):
        listener.settimeout(0.5)   # wake up regularly so stop() and Ctrl+C work on every OS
        while not self._stopping.is_set():
            try:
                sock, peer = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stopping.is_set():
                    return
                continue
            sock.settimeout(None)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._lock:
                self.connections_accepted += 1
            conn_id = next(self._ids)
            if not self._slots.acquire(blocking=False):
                threading.Thread(target=self._refuse, args=(sock, conn_id), daemon=True).start()
                continue
            threading.Thread(target=self._serve_connection, args=(sock, peer, conn_id), daemon=True).start()

    def _refuse(self, sock, conn_id):
        try:
            sock.settimeout(1.0)
            sock.sendall(build_response(503, "too many connections, try again later", False, None,
                                        [("X-Conn-Id", str(conn_id)), ("X-Conn-Request", "1")]))
        except OSError:
            pass
        close_gracefully(sock)

    def _serve_connection(self, sock, peer, conn_id):
        tag = f"[conn {conn_id} {peer[0]}:{peer[1]}]"
        self.log(f"{tag} open")
        reader = Reader(sock)
        served = 0
        why = "?"

        def conn_headers():
            # A browser cannot see sockets. These tell it which connection answered, and how many
            # responses that connection has carried, this one included.
            return [("X-Conn-Id", str(conn_id)), ("X-Conn-Request", str(served + 1))]

        try:
            while True:
                # Idle phase: between requests the client may take up to idle_timeout seconds.
                reader.skip_blank_lines()
                while not reader.buf:
                    reader.deadline = None
                    sock.settimeout(self.idle_timeout)
                    try:
                        reader.fill()
                    except socket.timeout:
                        why = f"idle for {self.idle_timeout:g}s"
                        return
                    except PeerClosed:
                        why = "client closed the connection"
                        return
                    reader.skip_blank_lines()

                # Request phase: the first byte is here, so the rest must follow promptly.
                reader.deadline = time.monotonic() + self.request_timeout
                request = None
                try:
                    request = read_request(reader)
                    status, extra = 200, []
                    body, content_type = answer(request)
                except HTTPError as err:
                    status, body, extra = err.status, f"{err.status} {REASONS[err.status]}: {err.message}", err.headers
                    content_type = TEXT_PLAIN
                    if err.close:
                        self._send(sock, build_response(status, body, False, None, extra + conn_headers()))
                        self.log(f"{tag} #{served + 1} {self._describe(request)} -> {status} ({err.message}), closing")
                        why = "framing error"
                        return
                except socket.timeout:
                    self._send(sock, build_response(408, "408 Request Timeout: request incomplete", False, None,
                                                    conn_headers()))
                    why = f"request not complete within {self.request_timeout:g}s (sent 408)"
                    return
                except PeerClosed:
                    why = "client closed the connection in the middle of a request"
                    return
                except OSError:
                    raise
                except Exception as exc:   # a bug in here must not take the connection down silently
                    self._send(sock, build_response(500, "500 Internal Server Error", False, None, conn_headers()))
                    why = f"internal error: {exc!r}"
                    return
                reader.deadline = None

                keep = request.keep_alive
                self._send(sock, build_response(status, body, keep, self.idle_timeout, extra + conn_headers(),
                                                head_only=request.method == "HEAD", content_type=content_type))
                served += 1
                shown = repr(body) if content_type == TEXT_PLAIN else f"<{content_type.split(';')[0]}, {len(body)} chars>"
                self.log(f"{tag} #{served} {request.method} {request.target[:80]} "
                         f"HTTP/{request.version[0]}.{request.version[1]} -> {status} {shown}")
                if not keep:
                    why = "Connection: close" if request.version >= (1, 1) else "HTTP/1.0 without keep-alive"
                    return
        except OSError as exc:
            why = f"socket error: {exc}"
        finally:
            close_gracefully(sock)
            self._slots.release()
            self.log(f"{tag} closed: {why}; {served} response(s) on this one connection")

    def _send(self, sock, data):
        sock.settimeout(self.request_timeout)   # a client that never reads must not pin this thread
        sock.sendall(data)

    @staticmethod
    def _describe(request):
        return f"{request.method} {request.target[:80]}" if request else "<unparseable request>"


def main(argv=None):
    parser = argparse.ArgumentParser(description="HTTP/1.1 keep-alive calculator on a raw socket.")
    parser.add_argument("--host", default="localhost",
                        help="name or address to listen on (default: localhost = ::1 and 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT,
                        help="seconds a kept-alive connection may sit idle between requests; 0 = forever")
    parser.add_argument("--request-timeout", type=float, default=DEFAULT_REQUEST_TIMEOUT,
                        help="seconds a request may take to arrive once it has started")
    parser.add_argument("--quiet", action="store_true", help="no per-request log lines")
    args = parser.parse_args(argv)

    server = CalculatorServer(args.host, args.port, args.idle_timeout, args.request_timeout,
                              log=not args.quiet).start()
    where = ", ".join(f"[{a}]:{server.port}" if ":" in a else f"{a}:{server.port}" for a in server.addresses())
    print(f"calculator listening on {where}  (idle timeout {args.idle_timeout:g}s, Ctrl+C to stop)",
          file=sys.stderr, flush=True)
    try:
        while True:
            time.sleep(0.5)   # time.sleep, unlike Event.wait, is interruptible by Ctrl+C on Windows
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)
    finally:
        server.stop()


if __name__ == "__main__":
    main()
