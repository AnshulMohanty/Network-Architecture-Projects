"""End-to-end tests: real sockets against an in-process bserve, and bcurl as a subprocess.

Run from binary-http/:  python -m unittest -v
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from bhttp.frames import (DATA, DATA_CHUNK, END_STREAM, GOAWAY, HEADERS, NO_ERROR, PREFACE,  # noqa: E402
                          PROTOCOL_ERROR, TIMEOUT, ConnectionClosed, Frame, FrameReader,
                          encode_frame, parse_goaway)
from bhttp.headers import decode_block, encode_block, get  # noqa: E402
from bhttp.net import close_gracefully  # noqa: E402
from bserve import BServer  # noqa: E402

BCURL = os.path.join(ROOT, "bcurl.py")


def request_block(path="/index.html", method="GET", authority="localhost", extra=()):
    return encode_block([(":method", method), (":path", path), (":authority", authority)] + list(extra))


class Raw:
    """A hand-driven BHTTP/1 peer: send exact frames, read exact frames."""

    def __init__(self, port, preface=PREFACE):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.reader = FrameReader(self.sock)
        self.sock.sendall(preface)
        self.server_preface = self.reader.read_exact(8, what="preface")

    def send(self, *frames):
        self.sock.sendall(b"".join(f if isinstance(f, bytes) else f.encode() for f in frames))

    def get(self, stream_id, path="/index.html", method="GET", extra=()):
        self.send(Frame(HEADERS, END_STREAM, stream_id, request_block(path, method, extra=extra)))
        return self.response(stream_id)

    def frame(self):
        return self.reader.read_frame()

    def response(self, stream_id):
        """-> (status, fields, body, [frames]) for one stream, skipping unknown frame types."""
        frames, fields, body = [], None, b""
        while True:
            f = self.frame()
            frames.append(f)
            if f.type == HEADERS:
                assert f.stream_id == stream_id, f
                fields = decode_block(f.payload)
            elif f.type == DATA:
                assert f.stream_id == stream_id, f
                body += f.payload
            elif f.type == GOAWAY:
                raise AssertionError(f"unexpected GOAWAY {parse_goaway(f)}")
            if f.end_stream:
                return int(get(fields, ":status")), fields, body, frames

    def expect_goaway(self, code):
        f = self.frame()
        while f.type not in (GOAWAY,):
            f = self.frame()
        last, got, debug = parse_goaway(f)
        assert got == code, (got, debug)
        return last, debug

    def closed(self):
        try:
            self.frame()
        except (ConnectionClosed, ConnectionResetError, ConnectionAbortedError):
            return True
        return False

    def close(self):
        self.sock.close()


class E2E(unittest.TestCase):
    idle_timeout = 60
    frame_timeout = 10
    grease = False

    @classmethod
    def setUpClass(cls):
        cls.www = tempfile.mkdtemp(prefix="bserve-test-")
        with open(os.path.join(cls.www, "index.html"), "wb") as f:
            f.write(b"<h1>hi</h1>\n")
        os.mkdir(os.path.join(cls.www, "docs"))
        with open(os.path.join(cls.www, "docs", "index.html"), "wb") as f:
            f.write(b"docs index\n")
        with open(os.path.join(cls.www, "empty.txt"), "wb"):
            pass
        cls.big = os.urandom(100_000)
        with open(os.path.join(cls.www, "big.bin"), "wb") as f:
            f.write(cls.big)
        with open(os.path.join(cls.www, "héllo wörld.txt"), "wb") as f:
            f.write(b"unicode name\n")
        cls.outside = os.path.join(os.path.dirname(cls.www), "outside-docroot.txt")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.www, ignore_errors=True)

    def setUp(self):
        self.server = BServer(self.www, "localhost", 0, idle_timeout=self.idle_timeout,
                              frame_timeout=self.frame_timeout, grease=self.grease, log=False).start()
        self.raws = []

    def tearDown(self):
        for r in self.raws:
            r.close()
        self.server.stop()

    def raw(self, **kw):
        r = Raw(self.server.port, **kw)
        self.raws.append(r)
        return r

    def bcurl(self, *args):
        return subprocess.run([sys.executable, BCURL, *args], capture_output=True, timeout=30)

    def url(self, path):
        return f"localhost:{self.server.port}{path}"


class TestServing(E2E):
    def test_get_body_to_stdout_exit_0(self):
        result = self.bcurl(self.url("/index.html"))
        self.assertEqual((result.returncode, result.stdout), (0, b"<h1>hi</h1>\n"))

    def test_large_file_spans_many_data_frames(self):
        status, fields, body, frames = self.raw().get(1, "/big.bin")
        data = [f for f in frames if f.type == DATA]
        self.assertEqual((status, body, get(fields, "content-length")), (200, self.big, "100000"))
        self.assertEqual(len(data), -(-100_000 // DATA_CHUNK))            # 7 frames
        self.assertTrue(all(len(f.payload) <= DATA_CHUNK for f in data))
        self.assertEqual([f.end_stream for f in data], [False] * (len(data) - 1) + [True])
        self.assertEqual(self.bcurl(self.url("/big.bin")).stdout, self.big)

    def test_empty_file_is_one_headers_frame(self):
        status, fields, body, frames = self.raw().get(1, "/empty.txt")
        self.assertEqual((status, body, get(fields, "content-length"), len(frames)), (200, b"", "0", 1))
        self.assertTrue(frames[0].end_stream)

    def test_head(self):
        status, fields, body, frames = self.raw().get(1, "/big.bin", method="HEAD")
        self.assertEqual((status, get(fields, "content-length"), body, len(frames)), (200, "100000", b"", 1))
        result = self.bcurl("-I", self.url("/big.bin"))
        self.assertEqual(result.returncode, 0)
        self.assertIn(b":status: 200\ncontent-type: application/octet-stream\ncontent-length: 100000\n", result.stdout)

    def test_response_headers_are_the_five_static_names(self):
        _, fields, _, _ = self.raw().get(1)
        self.assertEqual([(f.name, f.index) for f in fields],
                         [(":status", 4), ("content-type", 5), ("content-length", 6), ("date", 7), ("server", 8)])

    def test_directory_index_and_path_forms(self):
        r = self.raw()
        self.assertEqual(r.get(1, "/")[2], b"<h1>hi</h1>\n")
        self.assertEqual(r.get(2, "/docs/")[2], b"docs index\n")
        self.assertEqual(r.get(3, "/docs")[2], b"docs index\n")
        self.assertEqual(r.get(4, "/index.html?cache=no#frag")[0], 200)
        self.assertEqual(r.get(5, "/%69ndex.html")[0], 200)
        self.assertEqual(r.get(6, "//docs//index.html")[0], 200)
        self.assertEqual(self.bcurl(self.url("/héllo wörld.txt")).stdout, b"unicode name\n")

    def test_errors_and_exit_codes(self):
        r = self.raw()
        self.assertEqual(r.get(1, "/missing.html")[0], 404)
        status, fields, _, _ = r.get(2, method="POST")
        self.assertEqual((status, get(fields, "allow")), (405, "GET, HEAD"))
        self.assertEqual([f.index for f in fields if f.name == "allow"], [0])   # sent as a literal name
        stream = 3
        for path in ("/../x", "/%2e%2e/x", "/..%2fx", "/docs/../index.html", "/a\\b", "/a%5cb", "/a%00b",
                     "/%zz", "/./index.html", "/%ff"):
            self.assertEqual(r.get(stream, path)[0], 400, path)
            stream += 1
        self.assertEqual(r.get(stream, "/index.html")[0], 200)   # all of that on one connection
        self.assertEqual(self.bcurl(self.url("/missing.html")).returncode, 4)
        self.assertEqual(self.bcurl("-X", "POST", self.url("/")).returncode, 4)
        self.assertEqual(self.bcurl(self.url("/%2e%2e/x")).returncode, 4)
        self.assertEqual(self.server.connections_accepted, 4)

    def test_head_of_missing_file(self):
        status, _, body, frames = self.raw().get(1, "/missing", method="HEAD")
        self.assertEqual((status, body, len(frames)), (404, b"", 1))


class TestOneConnection(E2E):
    def test_many_urls_one_connection(self):
        result = self.bcurl(self.url("/index.html"), self.url("/docs/"), self.url("/big.bin"))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"<h1>hi</h1>\ndocs index\n" + self.big)
        self.assertEqual(self.server.connections_accepted, 1)

    def test_pipelined(self):
        result = self.bcurl("-p", self.url("/big.bin"), self.url("/missing"), self.url("/index.html"))
        self.assertEqual(result.returncode, 4)
        self.assertTrue(result.stdout.endswith(b"<h1>hi</h1>\n"))
        self.assertEqual(self.server.connections_accepted, 1)

    def test_raw_pipeline_answers_in_order(self):
        r = self.raw()
        r.send(*[Frame(HEADERS, END_STREAM, sid, request_block(p)) for sid, p in
                 [(1, "/big.bin"), (3, "/nope"), (8, "/index.html")]])
        self.assertEqual([r.response(sid)[0] for sid in (1, 3, 8)], [200, 404, 200])

    def test_different_hosts_refused_before_connecting(self):
        result = self.bcurl(self.url("/"), f"127.0.0.2:{self.server.port}/")
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"never opens a second connection", result.stderr)
        self.assertEqual(self.server.connections_accepted, 0)

    def test_request_body_is_consumed(self):
        r = self.raw()
        r.send(Frame(HEADERS, 0, 1, request_block(extra=[("content-length", "11")])),
               Frame(DATA, 0, 1, b"hello "), Frame(DATA, END_STREAM, 1, b"world"))
        self.assertEqual(r.response(1)[0], 200)
        r.send(Frame(HEADERS, 0, 2, request_block(extra=[("content-length", "3")])),
               Frame(DATA, END_STREAM, 2, b"four"))
        self.assertEqual(r.response(2)[0], 400)            # content-length does not match the DATA
        self.assertEqual(r.get(3)[0], 200)
        self.assertEqual(self.bcurl("-d", "a=1", self.url("/index.html")).returncode, 4)   # POST -> 405


class TestExtensibility(E2E):
    """A receiver meeting a frame type it does not know MUST skip it cleanly."""

    def test_unknown_types_everywhere(self):
        r = self.raw()
        decoy = encode_frame(HEADERS, END_STREAM, 99, request_block("/missing"))
        r.send(Frame(0x07, 0xFF, 0, b"on stream 0"),
               Frame(0x7F, 0, 1, decoy * 50),                          # payload that looks like frames
               Frame(HEADERS, 0, 1, request_block()),
               Frame(0xF0, 0, 1, b""),                                 # between HEADERS and DATA
               Frame(0xEE, 0, 1, os.urandom(65535)),                   # maximum-size unknown frame
               Frame(DATA, END_STREAM, 1, b""))
        status, _, body, _ = r.response(1)
        self.assertEqual((status, body), (200, b"<h1>hi</h1>\n"))
        self.assertEqual(r.get(2)[0], 200)

    def test_unknown_flag_bits_are_ignored(self):
        r = self.raw()
        r.send(Frame(HEADERS, 0xFE | END_STREAM, 1, request_block()))
        self.assertEqual(r.response(1)[0], 200)

    def test_bcurl_grease(self):
        result = self.bcurl("--grease", "-v", self.url("/index.html"))
        self.assertEqual((result.returncode, result.stdout), (0, b"<h1>hi</h1>\n"))
        self.assertIn(b"(grease)", result.stderr)


class TestServerGrease(E2E):
    grease = True

    def test_client_skips_server_grease(self):
        result = self.bcurl("-v", self.url("/index.html"), self.url("/big.bin"))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"<h1>hi</h1>\n" + self.big)
        self.assertIn(b"(unknown type: skipped)", result.stderr)


class TestStreamErrors(E2E):
    """Well-formed frames, invalid request: 400 on that stream, the connection lives on."""

    def test_malformed_blocks_get_400_and_connection_survives(self):
        r = self.raw()
        blocks = [
            b"\x2a\x00\x01x",                                        # index 42
            request_block()[:-2],                                    # truncated
            b"\x00\x00\x03Bad\x00\x00" + request_block(),            # uppercase literal name
            encode_block([(":method", "GET"), (":path", "/")]),      # no :authority
            encode_block([("accept", "*/*")]) + request_block(),     # pseudo-header after regular
            encode_block([(":method", "GET"), (":path", "x"), (":authority", "a")]),
            encode_block([(":status", "200")]) + request_block(),
        ]
        for sid, block in enumerate(blocks, start=1):
            r.send(Frame(HEADERS, END_STREAM, sid, block))
            status, fields, body, _ = r.response(sid)
            self.assertEqual(status, 400, block)
            self.assertEqual(get(fields, "content-type"), "text/plain; charset=utf-8")
            self.assertTrue(body.startswith(b"400 "))
        self.assertEqual(r.get(len(blocks) + 1)[0], 200)
        self.assertEqual(self.server.connections_accepted, 1)


class TestConnectionErrors(E2E):
    """Frame sequence broken: GOAWAY(PROTOCOL_ERROR, reason), then close."""

    def check(self, *frames, preface=PREFACE, answered_first=0):
        r = self.raw(preface=preface)
        for sid in range(1, answered_first + 1):
            self.assertEqual(r.get(sid)[0], 200)
        r.send(*frames)
        last, debug = r.expect_goaway(PROTOCOL_ERROR)
        self.assertEqual(last, answered_first)
        self.assertTrue(r.closed())
        return debug

    def test_cases(self):
        self.assertIn("preface", self.check(preface=b"GET / HT"))
        self.assertIn("preface", self.check(preface=b"BHTTP/2\n"))
        self.assertIn("no request in progress", self.check(Frame(DATA, END_STREAM, 1, b"x")))
        self.assertIn("stream 0", self.check(Frame(HEADERS, END_STREAM, 0, request_block())))
        self.assertIn("stream 0", self.check(Frame(DATA, END_STREAM, 0, b"")))
        self.assertIn("not above", self.check(Frame(HEADERS, END_STREAM, 2, request_block()), answered_first=2))
        self.assertIn("has not ended", self.check(Frame(HEADERS, 0, 1, request_block()),
                                                  Frame(HEADERS, END_STREAM, 3, request_block())))
        self.assertIn("no request in progress", self.check(Frame(HEADERS, 0, 1, request_block()),
                                                           Frame(DATA, END_STREAM, 2, b"")))
        self.assertIn("GOAWAY on stream", self.check(Frame(GOAWAY, 0, 5, b"\0" * 8)))
        self.assertIn("minimum 8", self.check(Frame(GOAWAY, 0, 0, b"\0" * 3)))

    def test_bcurl_against_something_that_is_not_bserve(self):
        listener = socket.create_server(("127.0.0.1", 0))
        port = listener.getsockname()[1]

        def http11():
            conn, _ = listener.accept()
            conn.recv(100)
            conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            close_gracefully(conn)
            listener.close()
        threading.Thread(target=http11, daemon=True).start()
        result = self.bcurl(f"127.0.0.1:{port}/")
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"looks like HTTP/1.x", result.stderr)

    def test_bcurl_sends_goaway_on_malformed_response(self):
        listener = socket.create_server(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        seen = {}

        def bad_server():
            conn, _ = listener.accept()
            reader = FrameReader(conn)
            reader.read_preface()
            reader.read_frame()                                         # the request
            conn.sendall(PREFACE + encode_frame(DATA, END_STREAM, 1, b"no headers first"))
            seen["goaway"] = reader.read_frame()
            close_gracefully(conn)
            listener.close()
        worker = threading.Thread(target=bad_server, daemon=True)
        worker.start()
        result = self.bcurl(f"127.0.0.1:{port}/")
        worker.join(5)
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"DATA before HEADERS", result.stderr)
        last, code, debug = parse_goaway(seen["goaway"])
        self.assertEqual((last, code), (0, PROTOCOL_ERROR))

    def test_bcurl_exit_5_on_5xx(self):
        listener = socket.create_server(("127.0.0.1", 0))
        port = listener.getsockname()[1]

        def broken_server():
            conn, _ = listener.accept()
            conn.sendall(PREFACE + encode_frame(HEADERS, END_STREAM, 1, encode_block([(":status", "503")])))
            close_gracefully(conn)
            listener.close()
        threading.Thread(target=broken_server, daemon=True).start()
        self.assertEqual(self.bcurl(f"127.0.0.1:{port}/").returncode, 5)


class TestTimeouts(E2E):
    idle_timeout = 0.5
    frame_timeout = 0.5

    def test_idle_connection_gets_goaway_no_error(self):
        r = self.raw()
        self.assertEqual(r.get(1)[0], 200)
        last, debug = r.expect_goaway(NO_ERROR)
        self.assertEqual(last, 1)            # "I processed stream 1 and nothing after it"
        self.assertIn("idle", debug)
        self.assertTrue(r.closed())

    def test_half_a_frame_gets_goaway_timeout(self):
        r = self.raw()
        r.send(encode_frame(HEADERS, END_STREAM, 1, request_block())[:5])
        r.expect_goaway(TIMEOUT)
        self.assertTrue(r.closed())

    def test_request_without_end_stream_times_out(self):
        r = self.raw()
        r.send(Frame(HEADERS, 0, 1, request_block()))   # promises DATA that never comes
        r.expect_goaway(TIMEOUT)


if __name__ == "__main__":
    unittest.main()
