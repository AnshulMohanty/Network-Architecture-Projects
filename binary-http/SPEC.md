# BHTTP/1 — HTTP semantics over binary frames

**Protocol version** 1 · **Text revision** 3 (clarifications from two clean-room review rounds; wire format
unchanged) · **Transport** TCP, default port 9000 · **Integers** unsigned, big-endian · MUST / SHOULD / MAY as
in RFC 2119. The rationale for every width is in [DESIGN.md](DESIGN.md).

BHTTP/1 carries HTTP requests and responses (method, path, status, headers, body) in length-prefixed binary
frames over **one persistent TCP connection**. Every frame states its length up front, so a receiver always
knows where the next frame starts. That holds even for frame types it has never seen.

## 1. Connection preface

Each peer's first 8 octets are `42 48 54 54 50 2F 31 0A` (`"BHTTP/1\n"`). Each peer sends its preface
immediately, without waiting for the other's. A client MAY send its first request right after its own preface.
Anything else in those 8 octets is a connection error (§6). A receiver MAY give up at the first octet that
differs. Octet 6 is the protocol version.

## 2. Frames — 8-octet fixed header

```
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-------------------------------+---------------+---------------+
|          Length (16)          |   Type (8)    |   Flags (8)   |
+-------------------------------+---------------+---------------+
|                        Stream ID (32)                         |
+---------------------------------------------------------------+
|                 Payload (Length octets) ...                   |
```

| Field | Octets | Meaning |
|---|---|---|
| Length | 0–1 | Payload octets that follow the header, 0–65535. |
| Type | 2 | Frame type (§3). |
| Flags | 3 | Booleans. For the three defined types, undefined bits MUST be sent as 0 and MUST be ignored. |
| Stream ID | 4–7 | 0 = the connection. 1 to 2³²−1 = one request/response exchange. |

**Unknown frame types MUST be skipped.** The receiver reads and discards exactly `Length` payload octets and
carries on. An unknown frame is never an error and may appear anywhere, even inside a message. It may carry
any flags and any stream ID. It does not open or use up a stream. Types `0xF0`–`0xFF` are reserved and will
never be assigned. Anyone MAY send them at any time to check that peers skip them ("grease").

## 3. Frame types, messages, streams

| Type | Name | Stream | Flags | Payload |
|---|---|---|---|---|
| `0x00` | DATA | ≥1 | `0x01` END_STREAM | body octets (may be empty) |
| `0x01` | HEADERS | ≥1 | `0x01` END_STREAM | header block (§4) |
| `0x02` | GOAWAY | 0 | none | `last_stream_id:u32` `error_code:u32` `debug` (UTF-8, rest of the payload) |

**Messages.** A request or a response is one HEADERS frame followed by zero or more DATA frames on the same
stream. The last of these frames carries END_STREAM, so a body-less message is a single HEADERS frame with
END_STREAM. A header block MUST fit in one HEADERS frame. If `content-length` is present, it MUST be decimal
digits equal to the total DATA length. Otherwise the message is malformed. The exception is a response to
HEAD: it carries `content-length` but is exactly one HEADERS frame with END_STREAM and no DATA.

**Streams.** The client numbers its requests. Each stream ID MUST be greater than every earlier one on the
connection, and gaps are allowed. The response uses the request's stream ID. A request is *in progress*
from its HEADERS frame until the frame carrying its END_STREAM. While it is in progress the client may
send only DATA on that stream, unknown types, or GOAWAY. Version 1 is **not multiplexed**. The server
answers requests one at a time, in arrival order, and finishes each response before starting the next.
A client MAY pipeline, sending request N+1 before response N arrives, but it MUST keep reading.

**GOAWAY** says "I am closing". A peer MUST send it before closing because of an error or a timeout.
A client that is simply done MAY send GOAWAY(NO_ERROR) or just close.
- `last_stream_id` is the highest stream the sender has completely answered (0 if none). A client always sends 0.
- Requests above `last_stream_id` were not processed.
- A server that receives GOAWAY has already answered every complete request that arrived before it. It drops any request in progress, then closes. A client that sent GOAWAY(NO_ERROR) keeps reading until that close.
- Error codes are `0` NO_ERROR, `1` PROTOCOL_ERROR, `2` INTERNAL_ERROR and `3` TIMEOUT. Unknown codes count as errors.

## 4. Header block

A HEADERS payload is a sequence of fields that runs to the end of the payload. There is no count and no
terminator.

```
field = index:u8  [ name_len:u16  name ]  value_len:u16  value
          index 0       literal name follows (lowercase token, see below)
          index 1..10   name from the static table, no name octets
          index 11..255 malformed
```

| # | Name | # | Name | # | Name | # | Name | # | Name |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `:method` | 3 | `:authority` | 5 | `content-type` | 7 | `date` | 9 | `user-agent` |
| 2 | `:path` | 4 | `:status` | 6 | `content-length` | 8 | `server` | 10 | `accept` |

These are the ten names the reference programs send in every exchange. Any other name travels as a literal,
for example `allow` on a 405 or anything added with `bcurl -H`. Senders SHOULD index names that are in the
table; receivers MUST accept either form. Values are always literals: there is no value table, no Huffman
coding and no dynamic table. Values are octets (UTF-8 by convention), and a name may repeat.

A header block is **malformed** if any of these hold:
- a field runs past the end of the payload
- an index is above 10
- a literal name is empty or has a character outside `a-z 0-9 ! # $ % & ' * + - . ^ _ ` | ~`. `:` is not in that set, so pseudo-headers can only be sent indexed.
- a value contains `0x00`, `0x0A` or `0x0D`
- a pseudo-header comes after a regular header
- *request*: it does not have exactly one each of `:method` (a token: RFC 9110 `tchar`, i.e. the literal-name set plus `A-Z`), `:path` (starting with `/`) and `:authority` (any value, which the server ignores), or it has a `:status`
- *response*: it does not have exactly one `:status` (three digits), or it has a request pseudo-header

## 5. Server behaviour

A server checks each complete request in this order. The first failure decides the status.

1. Header block and `content-length` valid (§3, §4)? Otherwise **400**.
2. `:method` is exactly `GET` or `HEAD`? Otherwise **405**, with the literal header `allow: GET, HEAD`.
3. `:path` syntax: cut it at the first `?` or `#`, percent-decode it, and only then split it on `/`. A bad escape, a result that is not UTF-8, NUL, `\`, or a `.` or `..` segment → **400**.
4. File: empty segments collapse. `/` and directories map to their `index.html`; there are no redirects. Missing, not a regular file, or resolving outside the root → **404**. Exists but unreadable → **500**.

Every response carries `:status`, `content-type`, `content-length`, `date` (IMF-fixdate) and `server`.
Errors use `text/plain; charset=utf-8` with a short readable reason as the body. Bodies go in DATA frames of
at most 16384 octets; receivers accept any length up to 65535. The response to HEAD follows §3.

The connection stays open after every response, 4xx included. The server closes only when the client
closes or sends GOAWAY, on a connection error, or on a timeout:
- **Idle, 60 s.** No request is in progress, and 60 s have passed since the later of the last frame received and the last response finished (waiting for the preface counts as idle) → GOAWAY(NO_ERROR).
- **Started, 10 s.** A preface, a frame, or a request (HEADERS to END_STREAM) that is not complete 10 s after its first octet → GOAWAY(TIMEOUT).

After sending an error or timeout GOAWAY, a peer half-closes and discards input for about a second before closing, so a TCP RST cannot destroy the GOAWAY.

## 6. Errors

| Kind | Trigger | Action |
|---|---|---|
| **Stream error** | frames are fine but the request is not (§4 malformed, `content-length` mismatch, bad path) | read the request to its END_STREAM, answer **400** on its stream; the **connection stays open** |
| **Connection error** (server) | bad preface · HEADERS or DATA on stream 0 · HEADERS whose ID is not above the previous one · HEADERS while a request is in progress · DATA with no request in progress on its stream · GOAWAY on a stream ≠ 0 or shorter than 8 octets | GOAWAY(PROTOCOL_ERROR, last answered stream, reason), then close. Complete requests that arrived before the offending frame MAY be answered first; `last_stream_id` says which were |
| **Connection error** (client) | bad preface · any frame other than GOAWAY or an unknown type on a stream other than the one it is reading · DATA before HEADERS · a second HEADERS · malformed response block · `content-length` mismatch · GOAWAY on a stream ≠ 0 or shorter than 8 octets | report it, send GOAWAY(PROTOCOL_ERROR, 0), close. On receiving an error GOAWAY: report it and close |

## 7. Why these widths (full defence in DESIGN.md)

HTTP/2 uses **24/8/8/31**. The 24-bit length leaves room above its 16 KiB default frame size. The 8-bit type
lets later RFCs add frames that old peers ignore. The 8-bit flags hold per-type booleans. The 31-bit stream
ID, plus one reserved bit, gives about 2³¹ streams per connection, split odd (client) and even (server push).

BHTTP/1 uses **16/8/8/32 in 8 octets**, with the length first:
- It is the one field needed to skip an unknown frame.
- 64 KiB is plenty when HTTP/2 itself runs at 16 KiB. It bounds what a peer can make you buffer and forces multi-frame bodies from day one. The overhead is about 0.01 %.
- The type and flags reason as HTTP/2 does. Unknown types are the version-2 extension point, alongside new flag bits and a new preface version.
- The stream ID is unsigned 32-bit with no reserved bit, because nothing in v1 needs one. Read it as unsigned: `getUint32`, or Java's `Integer.toUnsignedLong`.
- Every field sits at a fixed whole-octet offset: one `struct.unpack("!HBBI")` in Python, four fixed-offset `DataView` reads in JS, and no bit masking anywhere.
