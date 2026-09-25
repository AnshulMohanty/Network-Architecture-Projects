"""Interop matrix: our Python programs against the clean-room JavaScript ones (interop/js/),
which were written by someone who saw only SPEC.md.

    python bcurl  ->  node bserve.mjs
    node bcurl.mjs ->  python bserve

Skipped when node or interop/js is missing. Run from binary-http/:  python -m unittest tests.test_interop -v
"""

import os
import shutil
import socket
import subprocess
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
JS = os.path.join(ROOT, "interop", "js")
DOCROOT = os.path.join(JS, "testroot")
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
from bhttp.frames import DATA, END_STREAM, HEADERS, PROTOCOL_ERROR, Frame  # noqa: E402
from bserve import BServer  # noqa: E402
from test_e2e import Raw, request_block  # noqa: E402

NODE = shutil.which("node")
BCURL_PY = os.path.join(ROOT, "bcurl.py")
available = NODE and os.path.exists(os.path.join(JS, "bserve.mjs"))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def read(name):
    with open(os.path.join(DOCROOT, name), "rb") as f:
        return f.read()


@unittest.skipUnless(available, "node or interop/js not available")
class PythonClientJsServer(unittest.TestCase):
    grease = False

    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        args = [NODE, os.path.join(JS, "bserve.mjs"), DOCROOT, str(cls.port)] + (["--grease"] if cls.grease else [])
        cls.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", cls.port), timeout=0.5).close()
                return
            except OSError:
                time.sleep(0.1)
        cls.proc.kill()
        raise RuntimeError("node bserve.mjs did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.kill()
        cls.proc.wait()

    def bcurl(self, *args):
        return subprocess.run([sys.executable, BCURL_PY, *args], capture_output=True, timeout=30)

    def url(self, path):
        return f"127.0.0.1:{self.port}{path}"

    def test_get_and_multi_frame_body(self):
        result = self.bcurl(self.url("/index.html"), self.url("/big.bin"), self.url("/sub/"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, read("index.html") + read("big.bin") + read("sub/index.html"))

    def test_pipelined(self):
        result = self.bcurl("-p", self.url("/big.bin"), self.url("/empty.txt"), self.url("/hello%20world.txt"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, read("big.bin") + read("hello world.txt"))

    def test_head(self):
        result = self.bcurl("-I", self.url("/big.bin"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"content-length: {len(read('big.bin'))}".encode(), result.stdout)

    def test_error_statuses(self):
        self.assertEqual(self.bcurl(self.url("/missing")).returncode, 4)
        self.assertEqual(self.bcurl("-X", "POST", self.url("/")).returncode, 4)
        self.assertEqual(self.bcurl(self.url("/%2e%2e/outside-docroot.txt")).returncode, 4)

    def test_client_grease(self):
        result = self.bcurl("--grease", self.url("/index.html"))
        self.assertEqual((result.returncode, result.stdout), (0, read("index.html")))

    def test_raw_stream_error_then_success(self):
        r = Raw(self.port)
        try:
            r.send(Frame(HEADERS, END_STREAM, 1, b"\x2a\x00\x00"))
            self.assertEqual(r.response(1)[0], 400)
            r.send(Frame(0x7F, 0, 0, b"unknown"), Frame(HEADERS, 0, 2, request_block("/index.html")),
                   Frame(0xF5, 0, 2, b""), Frame(DATA, END_STREAM, 2, b""))
            self.assertEqual(r.response(2)[2], read("index.html"))
            r.send(Frame(DATA, END_STREAM, 9, b"no request"))
            last, _ = r.expect_goaway(PROTOCOL_ERROR)
            self.assertEqual(last, 2)
        finally:
            r.close()


class PythonClientJsServerGrease(PythonClientJsServer):
    grease = True


@unittest.skipUnless(available, "node or interop/js not available")
class JsClientPythonServer(unittest.TestCase):
    grease = False

    def setUp(self):
        self.server = BServer(DOCROOT, "localhost", 0, grease=self.grease, log=False).start()

    def tearDown(self):
        self.server.stop()

    def node(self, script, *args):
        return subprocess.run([NODE, os.path.join(JS, script), *args], capture_output=True, timeout=30)

    def url(self, path):
        return f"localhost:{self.server.port}{path}"

    def test_get_many_urls_one_connection(self):
        result = self.node("bcurl.mjs", self.url("/index.html"), self.url("/big.bin"), self.url("/sub/page.txt"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, read("index.html") + read("big.bin") + read("sub/page.txt"))
        self.assertEqual(self.server.connections_accepted, 1)

    def test_head_and_empty(self):
        result = self.node("bcurl.mjs", "-I", self.url("/big.bin"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"content-length: {len(read('big.bin'))}".encode(), result.stdout)
        result = self.node("bcurl.mjs", self.url("/empty.txt"))
        self.assertEqual((result.returncode, result.stdout), (0, b""))

    def test_error_statuses(self):
        self.assertEqual(self.node("bcurl.mjs", self.url("/missing")).returncode, 4)
        self.assertEqual(self.node("bcurl.mjs", "-X", "DELETE", self.url("/")).returncode, 4)
        self.assertEqual(self.node("bcurl.mjs", self.url("/..%2foutside-docroot.txt")).returncode, 4)

    def test_verbose_and_grease(self):
        result = self.node("bcurl.mjs", "-v", "--grease", "-H", "x-trace: 1", self.url("/index.html"))
        self.assertEqual((result.returncode, result.stdout), (0, read("index.html")), result.stderr)

    def test_raw_probe(self):
        result = self.node("rawtest.mjs", "127.0.0.1", str(self.server.port))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class JsClientPythonServerGrease(JsClientPythonServer):
    grease = True


if __name__ == "__main__":
    unittest.main()
