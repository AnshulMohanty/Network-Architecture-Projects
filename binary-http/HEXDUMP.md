# Annotated hexdump: one complete request and response

This is a real exchange between `bcurl` and `bserve`, not bytes written out by hand:

```sh
./bserve ./www 9000
python tools/annotate.py localhost:9000/index.html --save captures/index
```

The raw bytes are kept in [captures/index.request.bin](captures/index.request.bin) (71 octets) and
[captures/index.response.bin](captures/index.response.bin) (230 octets). To regenerate the tables below
from the saved bytes, run `python tools/annotate.py --from captures/index`. The client side is built by bcurl's
own `request_frames()`. The server side is recorded exactly as `recv()` returned it.

```
client                                              server
  |-- preface  "BHTTP/1\n"                    8 --->|
  |-- HEADERS  stream 1  END_STREAM   8 + 55 = 63 -->|   GET /index.html
  |<--- preface  "BHTTP/1\n"                    8 --|
  |<--- HEADERS  stream 1             8 + 84 = 92 --|   200, 5 headers
  |<--- DATA     stream 1  END_STREAM 8 + 122 = 130 |   the 122-octet file
  |                (connection stays open)          |
```

## Client → server: 71 octets

```text
offset  octets                   field
------  -----------------------  --------------------------------------------------------
0000    42 48 54 54 50 2f 31 0a  preface      "BHTTP/1\n": magic + protocol version '1'
        ── frame 1: HEADERS, stream 1, 55-octet payload ────────────
0008    00 37                    length       = 55 (payload octets after this 8-octet header)
000a    01                       type         = 0x01 HEADERS
000b    01                       flags        = 0x01 END_STREAM: last frame of this message
000c    00 00 00 01              stream id    = 1
0010    01                       index 1      -> name ':method' from the static table
0011    00 03                    value length = 3
0013    47 45 54                 value        'GET'
0016    02                       index 2      -> name ':path' from the static table
0017    00 0b                    value length = 11
0019    2f 69 6e 64 65 78 2e 68  value        '/index.html'
0021    74 6d 6c
0024    03                       index 3      -> name ':authority' from the static table
0025    00 0e                    value length = 14
0027    6c 6f 63 61 6c 68 6f 73  value        'localhost:9000'
002f    74 3a 39 30 30 30
0035    09                       index 9      -> name 'user-agent' from the static table
0036    00 09                    value length = 9
0038    62 63 75 72 6c 2f 31 2e  value        'bcurl/1.0'
0040    30
0041    0a                       index 10     -> name 'accept' from the static table
0042    00 03                    value length = 3
0044    2a 2f 2a                 value        '*/*'
```

What to notice:

* **`00 37` comes first.** It is the length (55), the one field a receiver needs to skip a frame it
  doesn't understand. The payload runs from `0x0010` to `0x0046`: 55 octets, exactly as promised.
* **END_STREAM on the HEADERS frame** (`0b: 01`): a GET has no body, so the whole request is one frame.
* **Every name is a single octet** (`01 02 03 09 0a`). No name is spelled out, because all five are in the
  static table. Each value is a `u16` length followed by its bytes. The block has no terminator, so
  the frame length ends it.
* The client sends its preface and the first request in **one TCP segment**, without waiting for the
  server's preface (§1).

## Server → client: 230 octets

```text
offset  octets                   field
------  -----------------------  --------------------------------------------------------
0000    42 48 54 54 50 2f 31 0a  preface      "BHTTP/1\n": magic + protocol version '1'
        ── frame 1: HEADERS, stream 1, 84-octet payload ────────────
0008    00 54                    length       = 84 (payload octets after this 8-octet header)
000a    01                       type         = 0x01 HEADERS
000b    00                       flags        = 0x00 none: more frames follow
000c    00 00 00 01              stream id    = 1
0010    04                       index 4      -> name ':status' from the static table
0011    00 03                    value length = 3
0013    32 30 30                 value        '200'
0016    05                       index 5      -> name 'content-type' from the static table
0017    00 18                    value length = 24
0019    74 65 78 74 2f 68 74 6d  value        'text/html; charset=utf-8'
0021    6c 3b 20 63 68 61 72 73
0029    65 74 3d 75 74 66 2d 38
0031    06                       index 6      -> name 'content-length' from the static table
0032    00 03                    value length = 3
0034    31 32 32                 value        '122'
0037    07                       index 7      -> name 'date' from the static table
0038    00 1d                    value length = 29
003a    57 65 64 2c 20 32 33 20  value        'Wed, 23 Sep 2026 14:57:06 GMT'
0042    53 65 70 20 32 30 32 36
004a    20 31 34 3a 35 37 3a 30
0052    36 20 47 4d 54
0057    08                       index 8      -> name 'server' from the static table
0058    00 0a                    value length = 10
005a    62 73 65 72 76 65 2f 31  value        'bserve/1.0'
0062    2e 30
        ── frame 2: DATA, stream 1, 122-octet payload ────────────
0064    00 7a                    length       = 122 (payload octets after this 8-octet header)
0066    00                       type         = 0x00 DATA
0067    01                       flags        = 0x01 END_STREAM: last frame of this message
0068    00 00 00 01              stream id    = 1
006c    3c 21 64 6f 63 74 79 70  body         '<!doctyp'
0074    65 20 68 74 6d 6c 3e 0a  body         'e html>\n'
007c    3c 74 69 74 6c 65 3e 62  body         '<title>b'
0084    73 65 72 76 65 3c 2f 74  body         'serve</t'
008c    69 74 6c 65 3e 0a 3c 68  body         'itle>\n<h'
0094    31 3e 48 65 6c 6c 6f 20  body         '1>Hello '
009c    6f 76 65 72 20 42 48 54  body         'over BHT'
00a4    54 50 2f 31 3c 2f 68 31  body         'TP/1</h1'
00ac    3e 0a 3c 70 3e 45 76 65  body         '>\n<p>Eve'
00b4    72 79 20 62 79 74 65 20  body         'ry byte '
00bc    6f 66 20 74 68 69 73 20  body         'of this '
00c4    70 61 67 65 20 61 72 72  body         'page arr'
00cc    69 76 65 64 20 69 6e 20  body         'ived in '
00d4    61 20 44 41 54 41 20 66  body         'a DATA f'
00dc    72 61 6d 65 2e 3c 2f 70  body         'rame.</p'
00e4    3e 0a                    body         '>\n'
```

What to notice:

* **The HEADERS flags are `00`**, which means a body follows. The DATA frame's flags are `01` END_STREAM,
  which marks the end of the response. The stream ID `00 00 00 01` ties both frames to request 1.
* **`content-length: 122` and the DATA length `00 7a` (122) agree.** The client checks this (§3). A
  mismatch is a malformed response.
* **Where the response ends:** after the 122 body octets at `0x00e6`, and not one octet later. The next
  byte on this connection belongs to the next response. This is the Content-Length lesson from the
  calculator assignment, solved once at the frame layer.
* The file is smaller than one 16 KiB DATA frame, so one frame carries it. `big.txt` (70,000 octets) is
  sent as 5 DATA frames of 16384 + 16384 + 16384 + 16384 + 4464 octets, and only the last carries END_STREAM.

## The same exchange as HTTP/1.1 text

| | BHTTP/1 | HTTP/1.1 text | |
|---|---|---|---|
| request head | 63 (8 header + 55 block) | 86 (`GET … HTTP/1.1`, Host, User-Agent, Accept, CRLFs) | −27 % |
| response head | 92 (8 header + 84 block) | 137 (status line, 4 headers, CRLFs) | −33 % |
| connection preface | 8 per side, once | 0 | |
| per body frame | 8 | 0 (Content-Length) | |

The savings come from names only (`content-type` costs 1 octet instead of 14). Values are sent in full every
time. That is where HPACK's dynamic table and Huffman coding would help. Here they are deliberately left out
(see DESIGN.md).

## Appendix: a literal name (405 response, `bcurl -X DELETE`)

`allow` is not one of the ten static names, so it travels as index 0 plus a length-prefixed literal name.
This excerpt is from [captures/delete-405.response.bin](captures/delete-405.response.bin):

```text
0057    08                       index 8      -> name 'server' from the static table
0058    00 0a                    value length = 10
005a    62 73 65 72 76 65 2f 31  value        'bserve/1.0'
0062    2e 30
0064    00                       index 0      -> literal name follows
0065    00 05                    name length  = 5
0067    61 6c 6c 6f 77           name         'allow'
006c    00 09                    value length = 9
006e    47 45 54 2c 20 48 45 41  value        'GET, HEAD'
0076    44
```

Cost: 1 + 2 + 5 = 8 octets for the name, against 1 octet for an indexed name.
