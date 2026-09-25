"""End-to-end tests for the keep-alive calculator. Run from calculator/:  python -m unittest -v"""

import os
import socket
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from server import CalculatorServer  # noqa: E402


class Conn:
    """A raw test client that reads responses strictly by Content-Length."""

    def __init__(self, port, family=socket.AF_INET, host="127.0.0.1"):
        self.sock = socket.socket(family, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect((host, port))
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.buf = b""

    def send(self, data):
        self.sock.sendall(data.encode("latin-1") if isinstance(data, str) else data)

    def get(self, target, extra=""):
        self.send(f"GET {target} HTTP/1.1\r\nHost: localhost\r\n{extra}\r\n")
        return self.read()

    def _more(self):
        data = self.sock.recv(65536)
        if not data:
            raise ConnectionError("closed by server")
        self.buf += data

    def read(self, head_only=False):
        while b"\r\n\r\n" not in self.buf:
            self._more()
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        status = int(lines[0].split()[1])
        headers = {}
        for line in lines[1:]:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
        length = 0 if head_only else int(headers.get("content-length", "0"))
        while len(self.buf) < length:
            self._more()
        body, self.buf = self.buf[:length], self.buf[length:]
        return status, headers, body.decode()

    def closed_by_server(self, wait=2.0):
        """True if the server sent FIN (recv returns b'') within `wait` seconds."""
        self.sock.settimeout(wait)
        try:
            while True:
                data = self.sock.recv(65536)
                if not data:
                    return True
                self.buf += data
        except socket.timeout:
            return False
        except (ConnectionResetError, ConnectionAbortedError):
            return True

    def still_open(self):
        return not self.closed_by_server(wait=0.2)

    def close(self):
        self.sock.close()


class ServerTest(unittest.TestCase):
    idle_timeout = 60
    request_timeout = 10

    def setUp(self):
        self.server = CalculatorServer("localhost", 0, idle_timeout=self.idle_timeout,
                                       request_timeout=self.request_timeout, log=False).start()
        self.conns = []

    def tearDown(self):
        for c in self.conns:
            c.close()
        self.server.stop()

    def connect(self, **kw):
        c = Conn(self.server.port, **kw)
        self.conns.append(c)
        return c


class TestMarker(ServerTest):
    def test_marker_sequence_on_one_socket(self):
        c = self.connect()
        expected = [("/add?a=2&b=3", 200, "5"), ("/sub?a=10&b=4", 200, "6"), ("/mul?a=6&b=7", 200, "42"),
                    ("/div?a=1&b=0", 400, None), ("/pow?a=2&b=8", 404, None)]
        for target, status, body in expected:
            got_status, headers, got_body = c.get(target)
            self.assertEqual(got_status, status, target)
            if body is not None:
                self.assertEqual(got_body, body)
            self.assertEqual(headers["connection"], "keep-alive")
        c.send("POST /add HTTP/1.1\r\nHost: localhost\r\nContent-Length: 7\r\n\r\na=2&b=3")
        status, headers, _ = c.read()
        self.assertEqual(status, 405)
        self.assertEqual(headers["allow"], "GET, HEAD")
        self.assertTrue(c.still_open())
        self.assertEqual(self.server.connections_accepted, 1)   # 1 TCP handshake, 6 responses

    def test_task_list(self):
        c = self.connect()
        self.assertEqual(c.get("/div?a=9&b=3")[::2], (200, "3"))
        self.assertEqual(c.get("/add?a=x&b=3")[0], 400)
        c.send("GET /add HTTP/1.1\r\n\r\n")   # no Host
        status, _, body = c.read()
        self.assertEqual(status, 400)
        self.assertIn("Host", body)
        self.assertTrue(c.still_open())       # framing was fine, so the connection survives


class TestArithmetic(ServerTest):
    def test_results(self):
        c = self.connect()
        cases = {
            "/add?a=2&b=3": "5", "/sub?a=3&b=10": "-7", "/mul?a=-6&b=7": "-42", "/div?a=7&b=2": "3.5",
            "/div?a=1&b=3": "0.3333333333333333", "/add?a=0.1&b=0.2": "0.3", "/add?a=2.5&b=2.5": "5",
            "/mul?a=99999999999999999999&b=99999999999999999999": "9999999999999999999800000000000000000001",
            "/div?a=-9&b=3": "-3", "/add?a=%2D5&b=%2B2": "-3", "/add?b=3&a=2&c=ignored": "5",
            "/div?a=0&b=5": "0",
        }
        for target, expected in cases.items():
            status, _, body = c.get(target)
            self.assertEqual((status, body), (200, expected), target)

    def test_bad_operands_keep_connection(self):
        c = self.connect()
        for target in ["/add?a=x&b=3", "/add?a=&b=3", "/add?a=nan&b=1", "/add?a=inf&b=1", "/add?a=1_000&b=1",
                       "/add?a=+5&b=1", "/add?a=1e5&b=1", "/add?a=%D9%A3&b=1", "/add?a=1.&b=1", "/add?b=1",
                       "/add?a=1&a=2&b=3", "/add?a=%zz&b=1", "/div?a=1&b=0", "/div?a=1&b=0.000",
                       "/add?a=" + "9" * 101 + "&b=1"]:
            status, _, _ = c.get(target)
            self.assertEqual(status, 400, target)
        self.assertEqual(c.get("/add?a=1&b=1")[2], "2")   # still the same connection


class TestRouting(ServerTest):
    def test_404_405_501(self):
        c = self.connect()
        self.assertEqual(c.get("/pow?a=2&b=8")[0], 404)
        self.assertEqual(c.get("/add/?a=1&b=2")[0], 404)
        self.assertEqual(c.get("/")[0], 404)
        for method in ("POST", "PUT", "DELETE", "OPTIONS"):
            c.send(f"{method} /add HTTP/1.1\r\nHost: x\r\n\r\n")
            status, headers, _ = c.read()
            self.assertEqual(status, 405, method)
            self.assertEqual(headers["allow"], "GET, HEAD")
        c.send("POST /pow HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertEqual(c.read()[0], 404)   # a resource that does not exist is 404 whatever the method
        c.send("BREW /add HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertEqual(c.read()[0], 501)
        self.assertTrue(c.still_open())

    def test_head(self):
        c = self.connect()
        c.send("HEAD /add?a=2&b=3 HTTP/1.1\r\nHost: x\r\n\r\n")
        status, headers, _ = c.read(head_only=True)
        self.assertEqual((status, headers["content-length"]), (200, "1"))
        # If a body had been sent after the HEAD response, this read would see "5HTTP/1.1..."
        self.assertEqual(c.get("/mul?a=2&b=3")[::2], (200, "6"))

    def test_absolute_form(self):
        c = self.connect()
        self.assertEqual(c.get("http://localhost:8080/add?a=1&b=2")[::2], (200, "3"))

    def test_multiple_host_headers(self):
        c = self.connect()
        c.send("GET /add?a=1&b=2 HTTP/1.1\r\nHost: a\r\nHost: b\r\n\r\n")
        self.assertEqual(c.read()[0], 400)


class TestFraming(ServerTest):
    """Where does this request end and the next one begin?"""

    def test_post_body_is_consumed_not_parsed(self):
        c = self.connect()
        # Body and the next request arrive in the same TCP segment. Byte n+1 belongs to the GET.
        c.send("POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: 7\r\n\r\na=2&b=3"
               "GET /add?a=40&b=2 HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertEqual(c.read()[0], 405)
        self.assertEqual(c.read()[::2], (200, "42"))

    def test_body_that_looks_like_a_request(self):
        c = self.connect()
        smuggled = "GET /pow?a=1&b=1 HTTP/1.1\r\nHost: x\r\n\r\n"
        c.send(f"POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: {len(smuggled)}\r\n\r\n{smuggled}"
               "GET /add?a=1&b=1 HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertEqual(c.read()[0], 405)
        self.assertEqual(c.read()[::2], (200, "2"))    # not the 404 the body was pretending to be
        self.assertTrue(c.still_open())

    def test_get_with_body(self):
        c = self.connect()
        c.send("GET /add?a=1&b=2 HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\nhello"
               "GET /add?a=3&b=4 HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertEqual(c.read()[2], "3")
        self.assertEqual(c.read()[2], "7")

    def test_one_byte_at_a_time(self):
        c = self.connect()
        for byte in b"GET /add?a=2&b=3 HTTP/1.1\r\nHost: localhost\r\n\r\n":
            c.send(bytes([byte]))
            time.sleep(0.001)
        self.assertEqual(c.read()[::2], (200, "5"))

    def test_body_split_across_segments(self):
        c = self.connect()
        c.send("POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: 10\r\n\r\n01234")
        time.sleep(0.1)
        c.send("56789GET /sub?a=5&b=1 HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertEqual(c.read()[0], 405)
        self.assertEqual(c.read()[2], "4")

    def test_pipelining_six_at_once(self):
        c = self.connect()
        batch = "".join(f"GET {t} HTTP/1.1\r\nHost: x\r\n\r\n" for t in
                        ["/add?a=2&b=3", "/sub?a=10&b=4", "/mul?a=6&b=7", "/div?a=1&b=0", "/pow?a=2&b=8"])
        batch += "POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: 3\r\n\r\nabc"
        c.send(batch)
        self.assertEqual([c.read()[0] for _ in range(6)], [200, 200, 200, 400, 404, 405])
        self.assertTrue(c.still_open())
        self.assertEqual(self.server.connections_accepted, 1)

    def test_chunked_body(self):
        c = self.connect()
        c.send("POST /add HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
               "4;name=value\r\nWiki\r\n5\r\npedia\r\nE\r\n in\r\n\r\nchunks.\r\n0\r\nX-Trailer: 1\r\n\r\n"
               "GET /add?a=20&b=22 HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertEqual(c.read()[0], 405)
        self.assertEqual(c.read()[::2], (200, "42"))

    def test_leading_blank_lines_are_ignored(self):
        c = self.connect()
        c.send("\r\n\r\nGET /add?a=1&b=1 HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertEqual(c.read()[2], "2")

    def test_bare_lf_line_endings(self):
        c = self.connect()
        c.send("GET /add?a=1&b=1 HTTP/1.1\nHost: x\n\n")
        self.assertEqual(c.read()[2], "2")

    def test_expect_100_continue(self):
        c = self.connect()
        c.send("POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: 3\r\nExpect: 100-continue\r\n\r\n")
        self.assertEqual(c.read()[0], 100)
        c.send("abc")
        self.assertEqual(c.read()[0], 405)


class TestFramingErrorsClose(ServerTest):
    """When nobody can know where the next request starts, answer and hang up."""

    def assert_rejected_and_closed(self, raw, status):
        c = self.connect()
        c.send(raw)
        got, headers, _ = c.read()
        self.assertEqual(got, status, raw[:60])
        self.assertEqual(headers["connection"], "close")
        self.assertTrue(c.closed_by_server(), raw[:60])

    def test_cases(self):
        self.assert_rejected_and_closed("GET /add?a=1&b=1 HTTP/1.1\r\nHost: x\r\nContent-Length: 3\r\n"
                                        "Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n", 400)
        self.assert_rejected_and_closed("POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: abc\r\n\r\n", 400)
        self.assert_rejected_and_closed("POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: 3\r\n"
                                        "Content-Length: 4\r\n\r\nabcd", 400)
        self.assert_rejected_and_closed("POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: -1\r\n\r\n", 400)
        self.assert_rejected_and_closed("POST /add HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
                                        "zz\r\n", 400)
        self.assert_rejected_and_closed("POST /add HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: gzip\r\n\r\n", 400)
        self.assert_rejected_and_closed("POST /add HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: gzip, chunked\r\n\r\n", 501)
        self.assert_rejected_and_closed("GET /add HTTP/1.1\r\nHost : x\r\n\r\n", 400)
        self.assert_rejected_and_closed("GET /add HTTP/1.1\r\nHost: x\r\n folded\r\n\r\n", 400)
        self.assert_rejected_and_closed("GET /add?a=1&b=1\r\n\r\n", 400)
        self.assert_rejected_and_closed("GET  /add HTTP/1.1\r\n\r\n", 400)
        self.assert_rejected_and_closed("GET /add?a=1&b=1 HTTP/2.0\r\nHost: x\r\n\r\n", 505)
        self.assert_rejected_and_closed("POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: 99999999\r\n\r\n", 413)
        self.assert_rejected_and_closed("GET /" + "a" * 9000 + " HTTP/1.1\r\n\r\n", 414)
        self.assert_rejected_and_closed("GET / HTTP/1.1\r\n" + "X-A: b\r\n" * 2000 + "\r\n", 431)


class TestConnectionManagement(ServerTest):
    def test_connection_close_is_honoured(self):
        c = self.connect()
        status, headers, body = c.get("/add?a=1&b=1", "Connection: close\r\n")
        self.assertEqual((status, body, headers["connection"]), (200, "2", "close"))
        self.assertTrue(c.closed_by_server())

    def test_http10_closes_by_default(self):
        c = self.connect()
        c.send("GET /add?a=1&b=1 HTTP/1.0\r\n\r\n")   # no Host needed in 1.0
        status, headers, body = c.read()
        self.assertEqual((status, body, headers["connection"]), (200, "2", "close"))
        self.assertTrue(c.closed_by_server())

    def test_http10_keep_alive(self):
        c = self.connect()
        c.send("GET /add?a=1&b=1 HTTP/1.0\r\nConnection: keep-alive\r\n\r\n")
        self.assertEqual(c.read()[1]["connection"], "keep-alive")
        c.send("GET /add?a=2&b=2 HTTP/1.0\r\nConnection: keep-alive\r\n\r\n")
        self.assertEqual(c.read()[2], "4")

    def test_keep_alive_header_advertises_timeout(self):
        self.assertEqual(self.connect().get("/add?a=1&b=1")[1]["keep-alive"], "timeout=60")

    def test_concurrent_connections(self):
        a, b = self.connect(), self.connect()
        a.send("GET /add?a=1&b=")                       # a is half-way through a request...
        self.assertEqual(b.get("/add?a=5&b=5")[2], "10")  # ...and b is not blocked by it
        a.send("1 HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertEqual(a.read()[2], "2")

    def test_ipv6_localhost(self):
        if not any(":" in addr for addr in self.server.addresses()):
            self.skipTest("no IPv6 loopback")
        c = self.connect(family=socket.AF_INET6, host="::1")
        self.assertEqual(c.get("/add?a=2&b=3")[2], "5")


class TestTimeouts(ServerTest):
    idle_timeout = 0.5
    request_timeout = 0.5

    def test_idle_connection_is_closed_silently(self):
        c = self.connect()
        self.assertEqual(c.get("/add?a=1&b=1")[2], "2")
        self.assertTrue(c.closed_by_server(wait=3))
        self.assertEqual(c.buf, b"")   # no 408 on an idle connection, just FIN

    def test_stalled_request_gets_408(self):
        c = self.connect()
        c.send("GET /add?a=1&b=1 HTTP/1.1\r\nHo")    # started, never finished (slowloris)
        status, headers, _ = c.read()
        self.assertEqual((status, headers["connection"]), (408, "close"))
        self.assertTrue(c.closed_by_server())


if __name__ == "__main__":
    unittest.main()
