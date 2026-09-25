#!/usr/bin/env python3
"""bserve: a BHTTP/1 file server (SPEC.md).

    ./bserve ./www 9000              (Windows: .\\bserve.cmd .\\www 9000)
    ./bserve ./www 9000 -v           hexdump every frame to stderr
    ./bserve ./www 9000 --grease     send a reserved-type frame before every response

GET and HEAD map :path to a file under the root. The connection stays open until the client closes
it, a timeout expires (GOAWAY) or the client breaks the protocol (GOAWAY + reason).
"""

from __future__ import annotations

import argparse
import io
import itertools
import os
import random
import socket
import sys
import threading
import time
import traceback
from email.utils import formatdate

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bhttp.frames import (DATA, DATA_CHUNK, END_STREAM, ERROR_NAMES, GOAWAY, GREASE_TYPES,  # noqa: E402
                          HEADERS, INTERNAL_ERROR, NO_ERROR, PREFACE, TIMEOUT, ConnectionClosed,
                          Frame, FrameReader, FrameTimeout, ProtocolError, goaway, parse_goaway)
from bhttp.headers import MalformedHeaders, decode_block, encode_block, get, parse_request  # noqa: E402
from bhttp.hexdump import trace_frame, trace_preface  # noqa: E402
from bhttp.net import close_gracefully, listen_all  # noqa: E402

SERVER_NAME = "bserve/1.0"
DEFAULT_IDLE_TIMEOUT = 60.0
DEFAULT_FRAME_TIMEOUT = 10.0
DEFAULT_MAX_CONNECTIONS = 64
TEXT = "text/plain; charset=utf-8"

# Our own table: Python's mimetypes reads the Windows registry, which can map .js to text/plain.
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8", ".txt": TEXT,
    ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".ico": "image/x-icon",
    ".webp": "image/webp", ".pdf": "application/pdf", ".wasm": "application/wasm",
}


class BadPath(Exception):
    """The :path is invalid (SPEC §5): answered with 400."""


class NotFound(Exception):
    """No regular file under the root for this :path: answered with 404."""


def percent_decode(text):
    raw = text.encode("latin-1")
    out = bytearray()
    i = 0
    while i < len(raw):
        if raw[i] == 0x25:
            pair = raw[i + 1:i + 3]
            if len(pair) != 2 or not all(c in b"0123456789abcdefABCDEF" for c in pair):
                raise BadPath("bad percent-escape in :path")
            out.append(int(pair, 16))
            i += 3
        else:
            out.append(raw[i])
            i += 1
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        raise BadPath(":path is not UTF-8 after percent-decoding") from None


def resolve(root, raw_path):
    """:path -> absolute file name under root. Every check happens *after* percent-decoding."""
    path = raw_path.split("?", 1)[0].split("#", 1)[0]
    path = percent_decode(path)
    if "\x00" in path or "\\" in path:
        raise BadPath(":path contains NUL or a backslash")
    segments = path.split("/")[1:]
    if any(s in (".", "..") for s in segments):
        raise BadPath("'.' and '..' segments are not allowed in :path")
    candidate = os.path.join(root, *[s for s in segments if s])
    if os.path.isdir(candidate):
        candidate = os.path.join(candidate, "index.html")
    real = os.path.realpath(candidate)
    try:
        inside = os.path.commonpath([root, real]) == root   # also catches symlinks and "C:" tricks
    except ValueError:                                      # different drives on Windows
        inside = False
    if not inside or not os.path.isfile(real):
        raise NotFound()
    return real


def content_type(path):
    return CONTENT_TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


class Pending:
    """A request whose HEADERS arrived; it is answered when its END_STREAM arrives."""

    def __init__(self, stream_id, deadline):
        self.stream_id = stream_id
        self.deadline = deadline
        self.request = None
        self.error = None
        self.body_octets = 0


class Connection:
    def __init__(self, server, sock, peer, conn_id):
        self.server = server
        self.sock = sock
        self.tag = f"[conn {conn_id} {peer[0]}:{peer[1]}]"
        self.reader = FrameReader(sock)
        self.out = bytearray()
        self.last_stream = 0       # highest stream ID the client has opened
        self.last_answered = 0     # highest stream ID we have fully answered (for GOAWAY)
        self.current = None        # Pending request still waiting for its END_STREAM
        self.served = 0

    # ---- output --------------------------------------------------------------------------

    def trace(self, lines):
        if self.server.verbose:
            self.server.log("\n".join(f"{self.tag} {line}" for line in lines))

    def queue(self, frame):
        self.trace(trace_frame(">", frame, None if self.server.verbose > 1 else 256))
        self.out += frame.encode()
        if len(self.out) >= 65536:
            self.flush()

    def flush(self):
        if self.out:
            self.sock.settimeout(self.server.frame_timeout)   # a client that never reads can't pin us
            self.sock.sendall(self.out)
            self.out.clear()

    def go_away(self, code, reason):
        try:
            self.queue(goaway(self.last_answered, code, reason))
            self.flush()
        except OSError:
            pass

    # ---- the connection loop -------------------------------------------------------------

    def run(self):
        why = "?"
        try:
            self.sock.settimeout(self.server.frame_timeout)
            self.sock.sendall(PREFACE)
            self.trace(trace_preface(">", PREFACE))
            if not self.reader.wait_for_data(self.server.idle_timeout):
                self.go_away(NO_ERROR, "idle timeout before preface")
                why = "no preface before idle timeout"
                return
            self.trace(trace_preface("<", self.reader.read_preface(self._deadline())))
            while True:
                if self.current is None:
                    # Between requests the client may stay quiet for up to idle_timeout...
                    if not self.reader.wait_for_data(self.server.idle_timeout):
                        self.go_away(NO_ERROR, f"idle for {self.server.idle_timeout:g}s")
                        why = "idle timeout (sent GOAWAY NO_ERROR)"
                        return
                    deadline = self._deadline()
                else:
                    # ...but a request that has started must finish before its deadline.
                    deadline = self.current.deadline
                frame = self.reader.read_frame(deadline)
                self.trace(trace_frame("<", frame, None if self.server.verbose > 1 else 256))
                if not self.handle(frame):
                    why = "client sent GOAWAY"
                    return
        except ConnectionClosed:
            why = "client closed the connection" + (" in the middle of a request" if self.current else "")
        except FrameTimeout:
            self.go_away(TIMEOUT, f"frame or request not completed within {self.server.frame_timeout:g}s")
            why = "timeout mid-frame (sent GOAWAY TIMEOUT)"
        except ProtocolError as exc:
            self.go_away(exc.code, str(exc))
            why = f"connection error, sent GOAWAY {ERROR_NAMES.get(exc.code, exc.code)}: {exc}"
        except OSError as exc:
            why = f"socket error: {exc}"
        except Exception as exc:  # a bug must not leave the client hanging without a reason
            self.server.log(traceback.format_exc())
            self.go_away(INTERNAL_ERROR, "internal server error")
            why = f"internal error: {exc!r}"
        finally:
            close_gracefully(self.sock)
            self.server.log(f"{self.tag} closed: {why}; {self.served} response(s) on this one connection")

    def _deadline(self):
        return time.monotonic() + self.server.frame_timeout

    def handle(self, frame):
        """Apply one frame to the connection state. Returns False when the client says GOAWAY."""
        sid = frame.stream_id
        if frame.type == HEADERS:
            if sid == 0:
                raise ProtocolError("HEADERS on stream 0")
            if self.current is not None:
                raise ProtocolError(f"HEADERS on stream {sid} while the request on stream "
                                    f"{self.current.stream_id} has not ended")
            if sid <= self.last_stream:
                raise ProtocolError(f"stream {sid} is not above the previous stream {self.last_stream}")
            self.last_stream = sid
            pending = Pending(sid, self._deadline())
            try:
                pending.request = parse_request(decode_block(frame.payload))
            except MalformedHeaders as exc:
                pending.error = str(exc)   # a stream error: answered with 400, connection survives
            if frame.end_stream:
                self.respond(pending)
            else:
                self.current = pending
        elif frame.type == DATA:
            if sid == 0:
                raise ProtocolError("DATA on stream 0")
            if self.current is None or self.current.stream_id != sid:
                raise ProtocolError(f"DATA on stream {sid}, which has no request in progress")
            self.current.body_octets += len(frame.payload)   # request bodies are read, not used
            if frame.end_stream:
                pending, self.current = self.current, None
                self.respond(pending)
        elif frame.type == GOAWAY:
            last, code, debug = parse_goaway(frame)
            self.server.log(f"{self.tag} client GOAWAY {ERROR_NAMES.get(code, code)} last={last} {debug!r}")
            return False
        # Any other type: unknown to v1, so skip it. The reader has already consumed exactly
        # `length` payload octets, so the next read starts at the next frame. Nothing else to do.
        return True

    # ---- answering -----------------------------------------------------------------------

    def respond(self, pending):
        sid, req = pending.stream_id, pending.request
        if pending.error:
            return self.send_text(sid, 400, pending.error, what="<malformed request>")
        what = f"{req.method} {req.path}"
        declared = get(req.fields, "content-length")
        if declared is not None and declared != str(pending.body_octets):
            return self.send_text(sid, 400, f"content-length is {declared} but the DATA frames "
                                            f"carried {pending.body_octets} octets", what=what)
        if req.method not in ("GET", "HEAD"):
            return self.send_text(sid, 405, f"{req.method} is not supported; use GET or HEAD",
                                  extra=[("allow", "GET, HEAD")], what=what)
        head = req.method == "HEAD"
        try:
            path = resolve(self.server.root, req.path)
        except BadPath as exc:
            return self.send_text(sid, 400, str(exc), head=head, what=what)
        except NotFound:
            return self.send_text(sid, 404, f"{req.path} not found", head=head, what=what)
        try:
            f = open(path, "rb")
        except OSError as exc:   # it exists but cannot be read (permissions, locked on Windows...)
            return self.send_text(sid, 500, f"{req.path} cannot be read: {exc.strerror}", head=head, what=what)
        with f:
            size = os.fstat(f.fileno()).st_size
            self.send_response(sid, 200, content_type(path), size, f.read, head=head, what=what)

    def send_text(self, sid, status, message, extra=(), head=False, what=""):
        body = f"{status} {message}\n".encode("utf-8")
        self.send_response(sid, status, TEXT, len(body), io.BytesIO(body).read, extra, head, what)

    def send_response(self, sid, status, ctype, size, read, extra=(), head=False, what=""):
        fields = [(":status", str(status)), ("content-type", ctype), ("content-length", str(size)),
                  ("date", formatdate(usegmt=True)), ("server", SERVER_NAME)] + list(extra)
        if self.server.grease:
            self.queue(Frame(random.choice(GREASE_TYPES), 0, sid, os.urandom(random.randint(0, 8))))
        bodyless = head or size == 0
        self.queue(Frame(HEADERS, END_STREAM if bodyless else 0, sid, encode_block(fields)))
        sent = frames = 0
        while not bodyless and sent < size:
            chunk = read(min(DATA_CHUNK, size - sent))
            if not chunk:
                raise ProtocolError("file shrank while it was being sent", INTERNAL_ERROR)
            sent += len(chunk)
            frames += 1
            self.queue(Frame(DATA, END_STREAM if sent == size else 0, sid, chunk))
        self.flush()
        self.last_answered = sid
        self.served += 1
        self.server.log(f"{self.tag} stream {sid} {what} -> {status} "
                        f"({size} octets{'' if bodyless else f' in {frames} DATA frame(s)'})")


class BServer:
    def __init__(self, root, host="localhost", port=9000, idle_timeout=DEFAULT_IDLE_TIMEOUT,
                 frame_timeout=DEFAULT_FRAME_TIMEOUT, grease=False, verbose=0, log=True,
                 max_connections=DEFAULT_MAX_CONNECTIONS):
        self.root = os.path.realpath(root)
        self.host, self.port = host, port
        self.idle_timeout = idle_timeout or None
        self.frame_timeout = frame_timeout
        self.grease = grease
        self.verbose = verbose
        self.log_enabled = log
        self.connections_accepted = 0
        self.listeners = []
        self._ids = itertools.count(1)
        self._slots = threading.BoundedSemaphore(max_connections)
        self._stopping = threading.Event()
        self._log_lock = threading.Lock()

    def log(self, message):
        if self.log_enabled:
            with self._log_lock:
                print(message, file=sys.stderr, flush=True)

    def start(self):
        self.listeners, self.port = listen_all(self.host, self.port, self.log)
        for sock in self.listeners:
            threading.Thread(target=self._accept_loop, args=(sock,), daemon=True).start()
        return self

    def stop(self):
        self._stopping.set()
        for sock in self.listeners:
            sock.close()

    def addresses(self):
        return [s.getsockname()[0] for s in self.listeners]

    def _accept_loop(self, listener):
        listener.settimeout(0.5)
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
            self.connections_accepted += 1
            if not self._slots.acquire(blocking=False):
                threading.Thread(target=self._refuse, args=(sock,), daemon=True).start()
                continue
            conn = Connection(self, sock, peer, next(self._ids))
            self.log(f"{conn.tag} open")
            threading.Thread(target=self._run, args=(conn,), daemon=True).start()

    def _run(self, conn):
        try:
            conn.run()
        finally:
            self._slots.release()

    def _refuse(self, sock):
        try:
            sock.settimeout(1.0)
            sock.sendall(PREFACE + goaway(0, INTERNAL_ERROR, "too many connections").encode())
        except OSError:
            pass
        close_gracefully(sock)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="bserve", description="BHTTP/1 file server (see SPEC.md).")
    parser.add_argument("root", help="document root, e.g. ./www")
    parser.add_argument("port", nargs="?", type=int, default=9000, help="TCP port (default 9000)")
    parser.add_argument("--host", default="localhost",
                        help="name or address to listen on (default localhost = ::1 and 127.0.0.1)")
    parser.add_argument("--idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT,
                        help="seconds with no request in progress before GOAWAY (0 = never)")
    parser.add_argument("--frame-timeout", type=float, default=DEFAULT_FRAME_TIMEOUT,
                        help="seconds a started frame or request has to complete")
    parser.add_argument("--grease", action="store_true",
                        help="send a reserved-type frame before every response (peers must skip it)")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="hexdump every frame to stderr (-vv: without truncating payloads)")
    parser.add_argument("--quiet", action="store_true", help="no per-request log lines")
    args = parser.parse_args(argv)
    if not os.path.isdir(args.root):
        parser.error(f"document root {args.root!r} is not a directory")

    server = BServer(args.root, args.host, args.port, args.idle_timeout, args.frame_timeout,
                     args.grease, args.verbose, log=not args.quiet).start()
    where = ", ".join(f"[{a}]:{server.port}" if ":" in a else f"{a}:{server.port}" for a in server.addresses())
    print(f"bserve: serving {server.root} on {where}  (BHTTP/1, idle timeout {args.idle_timeout:g}s, "
          f"Ctrl+C to stop)", file=sys.stderr, flush=True)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nbserve: shutting down", file=sys.stderr)
    finally:
        server.stop()


if __name__ == "__main__":
    main()
