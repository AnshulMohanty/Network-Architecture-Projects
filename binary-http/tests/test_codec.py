"""Unit tests for the frame and header-block codecs. Run from binary-http/:  python -m unittest -v"""

import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bhttp.frames import (DATA, END_STREAM, GOAWAY, HEADERS, PREFACE, ConnectionClosed, Frame,  # noqa: E402
                          FrameReader, ProtocolError, encode_frame, goaway, parse_goaway)
from bhttp.headers import (MalformedHeaders, decode_block, encode_block, parse_request,  # noqa: E402
                           parse_response_status)

REQUEST_FIELDS = [(":method", "GET"), (":path", "/index.html"), (":authority", "localhost:9000"),
                  ("user-agent", "bcurl/1.0"), ("accept", "*/*")]


class TestFrameLayout(unittest.TestCase):
    def test_header_is_8_octets_big_endian(self):
        wire = encode_frame(HEADERS, END_STREAM, 1, b"x" * 55)
        self.assertEqual(wire[:8].hex(" "), "00 37 01 01 00 00 00 01")   # len 55, HEADERS, END_STREAM, stream 1
        self.assertEqual(len(wire), 8 + 55)

    def test_field_widths(self):
        wire = encode_frame(0xAB, 0xCD, 0x01020304, b"\xff" * 0xFFFF)
        self.assertEqual(wire[:8].hex(), "ffffabcd01020304")

    def test_length_is_16_bits(self):
        encode_frame(DATA, 0, 1, b"\0" * 65535)
        with self.assertRaises(ValueError):
            encode_frame(DATA, 0, 1, b"\0" * 65536)

    def test_preface(self):
        self.assertEqual(PREFACE.hex(" "), "42 48 54 54 50 2f 31 0a")

    def test_goaway(self):
        frame = goaway(7, 1, "bad")
        self.assertEqual(frame.encode().hex(" "), "00 0b 02 00 00 00 00 00 00 00 00 07 00 00 00 01 62 61 64")
        self.assertEqual(parse_goaway(frame), (7, 1, "bad"))
        with self.assertRaises(ProtocolError):
            parse_goaway(Frame(GOAWAY, 0, 0, b"\0" * 7))
        with self.assertRaises(ProtocolError):
            parse_goaway(Frame(GOAWAY, 0, 3, b"\0" * 8))


class TestFrameReader(unittest.TestCase):
    def pair(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        b.settimeout(5)
        return a, FrameReader(b)

    def test_frames_split_and_merged_across_segments(self):
        a, reader = self.pair()
        wire = b"".join(encode_frame(t, 0, 1, p) for t, p in [(HEADERS, b"abc"), (0x7F, b"skip me"), (DATA, b"")])

        def dribble():
            for i in range(0, len(wire), 3):
                a.sendall(wire[i:i + 3])
                time.sleep(0.002)
        threading.Thread(target=dribble).start()
        frames = [reader.read_frame() for _ in range(3)]
        self.assertEqual([(f.type, f.payload) for f in frames], [(HEADERS, b"abc"), (0x7F, b"skip me"), (DATA, b"")])

    def test_leftover_bytes_stay_for_next_frame(self):
        a, reader = self.pair()
        a.sendall(encode_frame(DATA, 0, 1, b"one") + encode_frame(DATA, END_STREAM, 1, b"two"))
        self.assertEqual(reader.read_frame().payload, b"one")
        self.assertEqual(reader.buf, bytearray(encode_frame(DATA, END_STREAM, 1, b"two")))
        self.assertTrue(reader.read_frame().end_stream)

    def test_unknown_payload_that_looks_like_a_frame_is_not_parsed(self):
        a, reader = self.pair()
        decoy = encode_frame(HEADERS, END_STREAM, 99, b"decoy")
        a.sendall(encode_frame(0xF3, 0, 0, decoy) + encode_frame(DATA, END_STREAM, 1, b"real"))
        self.assertEqual(reader.read_frame().type, 0xF3)
        self.assertEqual(reader.read_frame().payload, b"real")

    def test_eof_at_boundary_vs_mid_frame(self):
        a, reader = self.pair()
        a.sendall(encode_frame(DATA, 0, 1, b"ok"))
        a.shutdown(socket.SHUT_WR)
        reader.read_frame()
        with self.assertRaises(ConnectionClosed):
            reader.read_frame()
        a2, reader2 = self.pair()
        a2.sendall(encode_frame(DATA, 0, 1, b"truncated")[:10])
        a2.shutdown(socket.SHUT_WR)
        with self.assertRaises(ProtocolError):
            reader2.read_frame()

    def test_bad_preface(self):
        a, reader = self.pair()
        a.sendall(b"GET / HTTP/1.1\r\n")
        with self.assertRaisesRegex(ProtocolError, "HTTP/1"):
            reader.read_preface()


class TestHeaderBlock(unittest.TestCase):
    def test_request_block_is_55_octets_all_indexed(self):
        block = encode_block(REQUEST_FIELDS)
        self.assertEqual(len(block), 55)
        self.assertEqual(block[:6].hex(" "), "01 00 03 47 45 54")    # index 1 (:method), len 3, "GET"
        self.assertTrue(all(f.index for f in decode_block(block)))

    def test_literal_name(self):
        block = encode_block([("allow", "GET, HEAD")])
        self.assertEqual(block.hex(" "), "00 00 05 61 6c 6c 6f 77 00 09 47 45 54 2c 20 48 45 41 44")
        (field,) = decode_block(block)
        self.assertEqual((field.name, field.value, field.index), ("allow", b"GET, HEAD", 0))

    def test_round_trip(self):
        fields = REQUEST_FIELDS + [("x-empty", ""), ("x-long", "v" * 30000)]
        self.assertEqual([(f.name, f.text) for f in decode_block(encode_block(fields))], fields)

    def test_receiver_accepts_literal_form_of_a_static_name(self):
        block = b"\x00\x00\x06accept\x00\x03*/*"
        self.assertEqual(decode_block(block)[0].name, "accept")

    def test_malformed_blocks(self):
        cases = {
            "index 11": b"\x0b\x00\x00",
            "index 255": b"\xff\x00\x00",
            "truncated value length": b"\x01\x00",
            "value past end": b"\x01\x00\x09GET",
            "name past end": b"\x00\x00\x09ab",
            "empty literal name": b"\x00\x00\x00\x00\x00",
            "uppercase literal": b"\x00\x00\x03Foo\x00\x00",
            "literal pseudo": b"\x00\x00\x07:method\x00\x03GET",
            "space in name": b"\x00\x00\x03a b\x00\x00",
            "CR in value": b"\x01\x00\x04GE\rT",
            "LF in value": b"\x01\x00\x04GE\nT",
            "NUL in value": b"\x01\x00\x04GE\x00T",
        }
        for label, block in cases.items():
            with self.assertRaises(MalformedHeaders, msg=label):
                decode_block(block)

    def test_request_rules(self):
        def req(fields):
            return parse_request(decode_block(encode_block(fields)))
        self.assertEqual(req(REQUEST_FIELDS).path, "/index.html")
        bad = {
            "missing :authority": REQUEST_FIELDS[:2],
            "duplicate :path": REQUEST_FIELDS + [(":path", "/x")],
            "pseudo after regular": [REQUEST_FIELDS[0], ("accept", "*/*")] + REQUEST_FIELDS[1:3],
            ":status in request": REQUEST_FIELDS[:3] + [(":status", "200")],
            "relative path": [(":method", "GET"), (":path", "index.html"), (":authority", "x")],
            "method not a token": [(":method", "G T"), (":path", "/"), (":authority", "x")],
        }
        for label, fields in bad.items():
            with self.assertRaises(MalformedHeaders, msg=label):
                req(fields)

    def test_response_status(self):
        self.assertEqual(parse_response_status(decode_block(encode_block([(":status", "404")]))), 404)
        for fields in ([(":status", "20")], [(":status", "2000")], [("server", "x")],
                       [(":status", "200"), (":path", "/")]):
            with self.assertRaises(MalformedHeaders):
                parse_response_status(decode_block(encode_block(fields)))


if __name__ == "__main__":
    unittest.main()
