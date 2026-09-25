# BHTTP/1: design defence

[SPEC.md](SPEC.md) says *what* the protocol is. This document says *why*: for each decision, the
alternatives that were considered, and why each was rejected.

## 0. The question the whole design answers

The calculator assignment already raised it: **where does this message end and the next one begin?**
HTTP/1.1 answers with a mix of mechanisms: an empty line ends the head, Content-Length ends the body, or
chunked encoding does, or EOF does. A receiver has to parse the text to find the boundaries, and any
disagreement between two parsers is a request-smuggling bug.

BHTTP/1 answers it once, at the lowest layer. **Every frame begins with its own length**, so the boundary of
every frame is known from its first two octets, before its type is even read. Everything else follows from
this: bodies of any size, message boundaries (END_STREAM), error recovery (a bad header block does not
desynchronise the stream) and version 2 (unknown frames can be skipped).

## 1. The frame header: 16 / 8 / 8 / 32, in 8 octets

### What HTTP/2 chose, 24/8/8/31 (RFC 9113 §4.1), and why

| Field | Bits | Why HTTP/2 made it this size |
|---|---|---|
| Length | 24 | Frames up to 16 MiB are *possible*, but `SETTINGS_MAX_FRAME_SIZE` defaults to 2¹⁴ = 16 KiB. On a multiplexed connection, one huge frame blocks every other stream behind it (head-of-line blocking inside the connection), so frames are kept small. 24 bits lets a peer raise the limit for bulk transfer without a new wire format. 32 bits would add one octet to *every* frame for sizes a multiplexed connection should avoid. |
| Type | 8 | 256 types. RFC 7540 defined ten (DATA…CONTINUATION) and said unknown types MUST be ignored. That rule is why ALTSVC (RFC 7838), ORIGIN (RFC 8336) and PRIORITY_UPDATE (RFC 9218) could be added later without breaking deployed peers. |
| Flags | 8 | Per-type booleans, e.g. END_STREAM, END_HEADERS, PADDED, PRIORITY, ACK. No type needs more than a few. |
| R + Stream ID | 1 + 31 | About 2³¹ streams per connection: odd IDs from the client, even ones for server push, 0 for the connection. The reserved bit came from SPDY, whose frames began with a control bit. It must be sent as 0 and ignored, which keeps the ID a non-negative *signed* 32-bit integer (easy in Java) and leaves one bit for the future. |

Total 72 bits = **9 octets**, small enough to put in front of every 16 KiB chunk (0.05 % overhead).

### What BHTTP/1 chose, and the alternatives for each field

**Length: 16 bits (0–65535), sent first.**

| Option | Verdict |
|---|---|
| 8 bits | ✗ 255 octets can't hold a request header block with a long path. 3 % overhead on bodies. |
| **16 bits** | ✓ HTTP/2 in practice runs at 16 KiB frames, so a 64 KiB cap loses nothing we would use. A receiver never has to buffer more than 64 KiB for one frame, so a peer cannot make it allocate 4 GiB. Multi-frame bodies are required from day one, so no implementation "works for small files only". Overhead is 8 / 65536 ≈ 0.012 % at full frames, 0.05 % at our 16 KiB frames. |
| 24 bits | ✗ Room we would never use, and an awkward 3-octet read in most languages. |
| 32 bits | ✗ Tempting ("one file, one frame"), but a receiver then has to stream a single frame's payload, skipping an unknown frame could mean discarding 4 GiB, and v2 multiplexing would suffer head-of-line blocking. |
| varint (as in QUIC / HTTP/3) | ✗ Compact and unbounded, but the header is no longer fixed-size, which the brief requires, and it needs more code for a stranger to get right. |

*Why first:* the length is the one field every receiver needs, including for types it doesn't know. HTTP/2 put it first for the same reason.

**Type: 8 bits.** Three types are defined and 256 are possible. Unknown types MUST be skipped, and that
rule is the version-2 extension mechanism. `0xF0`–`0xFF` are reserved **grease**. The idea comes from TLS
GREASE (RFC 8701) and HTTP/3's reserved frame types: send junk types on purpose so that no implementation
ships with "reject unknown frame" behaviour. `bcurl --grease` and `bserve --grease` do this, and the tests
check that both sides skip them.

**Flags: 8 bits.** Only END_STREAM (`0x01`) is used. Undefined bits must be 0 on defined types and are
ignored. v2 can add flags that v1 peers ignore safely.

**Stream ID: 32 bits, unsigned, no reserved bit.**

| Option | Verdict |
|---|---|
| none (like HTTP/1.1) | ✗ Pipelined responses can't be checked against their requests, GOAWAY can't say which requests were processed, and v2 multiplexing would need a new header. |
| 16 bits | ✗ 65,535 requests, and then a long-lived client must open a second connection, which bcurl is never allowed to do. |
| 31 + reserved (HTTP/2) | ≈ Works, but the reserved bit has no job in v1, and masking it is one more thing a stranger can get wrong. |
| **32 bits** | ✓ Never runs out. It sits at a 4-octet-aligned offset. Languages with only signed integers must read it as unsigned (`getUint32`, `Integer.toUnsignedLong`). |

v1 is not multiplexed, but the stream ID still earns its 4 octets: it pairs each response with its request
during pipelining, it gives GOAWAY a precise meaning ("I answered everything up to stream N"), and it makes
v2 multiplexing a change of *rules* rather than of *wire format*.

**Byte-aligned, big-endian.** Every field has a fixed whole-octet offset. Python needs one
`struct.unpack("!HBBI")`. JavaScript needs four `DataView` reads at fixed offsets. There is no 24-bit read
and no bit masking. Big-endian is network byte order, and it makes a hexdump readable by eye: `00 37` is 55.

## 2. The preface: 8 octets, `BHTTP/1\n`

* **It fails fast.** A peer that speaks something else finds out after 8 octets instead of misreading a
  random length. An HTTP/1.1 server rejects `BHTTP/1` as a bad request line. bcurl pointed at an HTTP/1.1
  server sees `HTTP/1.1` and says so.
* **It carries the version.** Octet 6 is `'1'`, so an incompatible v2 changes it and a v1 peer refuses
  cleanly. Compatible changes don't need it: they use new frame types.
* **It is readable in a hexdump**, like HTTP/2's `PRI * HTTP/2.0` preface, which was designed to be
  rejected by HTTP/1.1 servers.
* Rejected alternative: a version field in every frame, which costs 1 octet per frame for information that
  never changes on a connection.

## 3. Header blocks: static names and length-prefixed literals

HPACK (RFC 7541) has four mechanisms: a static table, literals, a dynamic table and Huffman coding. The brief
asks for the first two.

* **Ten static names**, exactly the ones the reference programs send in every exchange (5 request, 5 response).
  Each costs 1 octet instead of 5–16 characters plus `: ` and CRLF. `allow`, and anything added with
  `bcurl -H`, goes as a literal: index 0, then the name.
* **Values are always literal.** HPACK's static table also holds *name+value* pairs (e.g. `:method: GET`).
  We index names only, so the table stays a flat list a stranger can copy without mistakes.
* **`u16` length prefixes**, not HPACK's prefix-coded integers. HPACK packs lengths into 7-bit prefixes with
  continuation octets. That saves an octet on short strings but is the part of HPACK people most often get
  wrong. A `u16` can't overflow a 16-bit frame, so there is nothing to validate beyond "does it fit".
* **No dynamic table.** It is where HPACK's real compression comes from, but it adds per-connection state
  that both sides must keep in step. A single lost or misordered update corrupts every later header, and it
  brings memory-exhaustion attacks with it. For one request at a time over one connection, the state
  isn't worth it.
* **No Huffman coding**: a 257-entry code table to transcribe, for a saving of roughly 20–30 % on values.
* **No count, no terminator.** The frame length already ends the block. A count would only add a way to be
  inconsistent.
* **Result:** a request head is 63 octets against 86 for HTTP/1.1 text, and a response head is 92 against
  137 (see [HEXDUMP.md](HEXDUMP.md)).

## 4. Errors: the two classes from the calculator, again

| | HTTP/1.1 calculator | BHTTP/1 |
|---|---|---|
| framing intact, request bad | 400/404/405, keep the connection | **stream error**: 400 on that stream, keep the connection |
| framing broken | error + `Connection: close` | **connection error**: GOAWAY + reason, close |

The difference is that in BHTTP/1 framing breaks far less often. A garbage header block has a valid frame
length around it, so the server knows exactly where the next frame starts and can answer 400 and carry on.
Only rule violations at the frame level (wrong stream, out-of-order IDs, a bad preface) end the connection.

**GOAWAY carries `last_stream_id`** ("everything up to here was answered"). HTTP/1.1 has a keep-alive race:
the server closes an idle connection just as the client sends a request, and the client can't tell whether
that request was processed. GOAWAY removes the guesswork, because anything above `last_stream_id` was not
processed.

## 5. Timeouts

* **Idle, 60 s** (no request in progress, no frame received): GOAWAY(NO_ERROR). The same defence as the
  calculator: long enough for a person at a REPL, short enough that dead peers don't hold threads. Any
  received frame restarts it, including an unknown one. That was a deliberate choice, so that a future v2
  PING frame works as a keep-alive even against v1 servers.
* **Started, 10 s** (a preface, frame or request that began but didn't finish): GOAWAY(TIMEOUT). This is
  slowloris protection.
* After any GOAWAY, the peer half-closes and drains input before closing, so a TCP RST can't destroy the
  GOAWAY.

## 6. Did the spec work for a stranger? The clean-room review

**Method.** An independent implementer was given `SPEC.md` (revision 1) and nothing else: no Python code
and no other file. They wrote a Node.js server and client ([interop/js/](interop/js/)) using only Node
built-ins, plus a 97-check self-test.

**Result.** The two implementations interoperated **on the first attempt**, and again after each spec revision. The matrix is in
[tests/test_interop.py](tests/test_interop.py), 22 tests: Python bcurl against JS bserve and JS bcurl
against Python bserve, each with and without grease, covering multi-frame bodies, pipelining, HEAD, 400/404/405,
path traversal, raw malformed blocks and connection errors.

**The implementer's notes.** They listed 34 places where the text was unclear
([interop/js/NOTES.md](interop/js/NOTES.md)), including two real contradictions. Revision 2 of the text
resolved them without changing a single wire octet:

| Notes | Problem | Resolution in revision 2 |
|---|---|---|
| 21 | **Contradiction:** "the table lists exactly the names sent", yet `allow` is sent | the ten names sent in *every exchange*; others are literals (§4) |
| 9 | **Contradiction:** GOAWAY mid-request not allowed by §3, not an error in §6 | GOAWAY is allowed at any time; the request in progress is dropped (§3) |
| 8, 12 | "in progress" undefined; do malformed requests use up an ID? | defined: HEADERS to END_STREAM; every HEADERS uses its ID; gaps allowed (§3) |
| 13, 14, 18 | "last processed stream" undefined | highest stream *completely answered*; a client sends 0 (§3) |
| 15, 16, 17, 20 | client GOAWAY, and whether GOAWAY must precede every close | MUST before an error/timeout close, MAY otherwise; what a server does on receiving one (§3) |
| 10, 11 | content-length mismatch; HEAD with an empty DATA frame | malformed (request → 400); HEAD is exactly one HEADERS frame (§3) |
| 19 | §6 listed only server-side errors | client-side row added (§6) |
| 25–28, 30 | order of checks, `%2F`, UTF-8, `//`, directories, unreadable files | numbered check order; split after decoding; not UTF-8 → 400; `//` collapses; no redirects; 500 (§5) |
| 2, 7, 31 | when each timer starts and what restarts it | idle vs started, defined precisely (§5); unknown frames *do* restart idle (a different choice from their guess, reasoned in §5 above) |
| 5, 6 | grease flags must be 0? unknown frames on any stream? | unknown types: any flags, any stream, no stream opened (§2) |
| 1, 4 | fail fast on preface; send before the peer's preface | both allowed explicitly (§1) |
| 22–24, 29 | redundant `:` rule, method/authority/status syntax, value encoding, error headers | tightened (§4, §5) |
| 33, 34 | rationale said "one DataView call" and ignored signed readers of a 32-bit ID | corrected (§7, and §1 above) |
| 32 | stream-ID exhaustion | the ID range is stated (§2); strictly increasing means a client must close at 2³²−1 |

**Round 2.** The implementer brought their code in line with revision 2, again from `SPEC.md` alone.
Their self-test passes 122 of 122 checks, and the interop matrix stayed green. Their second pass found one
more contradiction: a client that sent GOAWAY was told to discard input, which would throw away the answers
it was still waiting for. It also found three gaps: malformed GOAWAY was missing from the client error
list, the idle rule could fire during a slow response, and "token" was undefined. **Revision 3** fixed all
of these in a few sentences, again with no wire change. Both implementations already behaved as revision 3
says, except that Python bcurl now sends GOAWAY before an error close
(`test_bcurl_sends_goaway_on_malformed_response`).

Deliberately left open, with the reason for each:
- The content-type mapping is the server's business.
- The send timeout is an implementation choice.
- Whether queued complete requests are answered before a connection-error GOAWAY is a MAY; `last_stream_id` tells the client which were answered.
- The meaning of 1xx and ≥ 600 statuses: a v1 server never sends them.
- Windows path quirks are implementation-defined, but a resolved path must still stay inside the root.
- A trailing `/` on a file name: both implementations serve the file.
- Exhausting stream IDs takes 2³²−1 requests on one connection.

## 7. Questions to expect, with short answers

* **Why not just use HTTP/2's header?** It is designed for multiplexing, push and flow control. We use none of them, so the 24-bit length and the reserved bit would have no job. Ours is one octet smaller and has no field that needs masking.
* **Why keep a stream ID without multiplexing?** Pipelining checks, GOAWAY semantics, and v2 room (§1 above).
* **What stops a 4 GiB allocation?** The length is 16-bit, so no frame is ever larger than 64 KiB.
* **How can v2 add something without breaking v1?** New frame type → v1 skips it. New flag bit → v1 ignores it. Incompatible change → new preface version, and v1 refuses cleanly.
* **What if the client sends a body with GET?** The server reads its DATA frames to END_STREAM and ignores them. If `content-length` disagrees with the DATA, that's a 400.
* **Where does one response end?** At the frame carrying END_STREAM on that stream. Its length says exactly where the next byte belongs.
* **Why 16384-octet DATA frames if 65535 fit?** It matches HTTP/2's default and is the size a v2 multiplexer would want for fair interleaving. Receivers still accept anything up to 65535.
