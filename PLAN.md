# Network Architecture — Analysis & Plan

Two deliverables come out of the brief:

| # | Deliverable | Folder | Due |
|---|---|---|---|
| A | **Calculator that stays on the line** — HTTP/1.1 keep-alive server on a raw socket | `calculator/` | before session 7 |
| B | **HTTP, in binary** — design a binary framing protocol, write the spec, build `bserve` + `bcurl` | `binary-http/` | course project |

Language: **Python 3.8+, standard library only** (`socket`, `threading`, `struct`). No `http.server`,
no `urllib` request parsing, no frameworks. It matches the marker's own test harness
(`socket.create_connection(("localhost", 8080))`) and runs on Windows, macOS and Linux unchanged.

---

## A. Calculator — analysis

### Explicit requirements

| Request | Expected |
|---|---|
| `GET /add?a=2&b=3` / `sub` / `mul` / `div` | `200`, body is the number (`5`, `6`, `42`, `3`) |
| `GET /div?a=1&b=0` | `400` (division by zero) |
| `GET /add?a=x&b=3` | `400` (not a number) |
| `GET /pow?a=2&b=8` | `404` (unknown route) |
| `POST /add` | `405` (+ `Allow` header) |
| `GET /add` with no `Host` | `400` (RFC 9112 §3.2: HTTP/1.1 without Host MUST get 400) |
| Marking | **one socket, every request**: after 6 requests, `socket still open: True`, *1 TCP handshake, 6 responses* |

### What is actually being tested — message framing

HTTP/1.0 got framing for free: the server closed the socket, so the body ended at EOF. Once the
connection stays open, the server must know **exactly** where each request ends:

* **Head** ends at the first empty line. Never assume one `recv()` holds one request. A request can arrive
  split across many TCP segments, and one segment can carry several requests.
* **Body** is exactly `Content-Length` bytes, or a chunked body up to its zero-size chunk. Byte *n+1*
  belongs to the **next** request. It must stay in the buffer, not be thrown away.
* Error responses **must still consume the body**. `POST /add` with `Content-Length: 7` gets a 405, *and*
  the server must read those 7 bytes, or it will parse them as the next request line.

Design consequence: a per-connection **buffered reader** (`read_line`, `read_exact(n)`) that owns the
leftover bytes between requests. Pipelining then comes for free: requests that are already buffered get
processed in order before the server calls `recv()` again.

### Two classes of error (the key design decision)

| Class | Examples | Action |
|---|---|---|
| **Semantic** — framing intact, next request's start is known | div by zero, bad number, 404, 405, missing Host | send error, **keep connection open** |
| **Framing** — cannot know where the next request starts | bad request line, bad/duplicate `Content-Length`, CL+TE together, bad chunk size, oversized head/body | send error with `Connection: close`, then close |

This split is what makes `GET /div?a=1&b=0 → 400` leave the socket open, which the marker checks.

### Implicit requirements / traps found during analysis

1. **`localhost` resolves to `::1` first** on this machine (and on most modern macOS/Linux).
   A server bound only to `127.0.0.1` makes the marker's `create_connection` fail on IPv6 first.
   → Listen on **every address `localhost` resolves to** (both `::1` and `127.0.0.1`).
2. **One `sendall()` per response** (head + body together). A naive marker that does one `recv()` per
   request would otherwise get the head and body out of step. Also set `TCP_NODELAY` so pipelined
   responses don't hit the Nagle / delayed-ACK stall (up to 200 ms on Windows).
3. **Graceful close**: when closing after an error, `shutdown(SHUT_WR)` and drain briefly before `close()`.
   Otherwise unread client bytes trigger a TCP RST that can destroy the error response in flight
   (RFC 9112 §9.6).
4. The **idle timeout** has to be long enough that a marker typing requests into a REPL doesn't lose the
   socket ("if the socket dies before I am done…"). It also has to be short enough that dead clients
   don't hold threads forever. Default **60 s**, configurable, advertised with `Keep-Alive: timeout=60`.
5. HTTP/1.0 clients default to close. HTTP/1.1 defaults to keep-alive. `Connection: close` must be honoured.
6. Strict number parsing (`nan`, `inf`, `1_000`, ` 5` must all be 400 even though Python's `float()`
   accepts them). Operand length is capped. Exact arithmetic via `fractions.Fraction`.
7. `HEAD` is supported (RFC 9110 requires GET and HEAD of general-purpose servers), so `Allow: GET, HEAD`.

### Stretch goals — all in scope

* `Connection: close` honoured (both directions).
* Idle timeout (60 s between requests) **plus** a separate 10 s request timeout once a request has
  started. The request timeout is slowloris protection. Idle expiry → silent close. Mid-request expiry → `408`.
* Chunked request bodies (`Transfer-Encoding: chunked`) with strict chunk-size parsing, extensions
  and trailers. Responses always have a known length, so the server always sends `Content-Length`.
* Pipelining: all six requests in one `send()`, answered in order.

### Files

```
calculator/
  server.py             the server (python server.py [--port 8080] [--idle-timeout 60])
  marker.py             replays the marker's exact test (+ --pipeline mode)
  tests/test_server.py  unittest suite: routes, errors, framing edge cases, pipelining, timeouts
  README.md             run instructions + design notes to defend in class
```

---

## B. Binary HTTP — analysis

### Explicit requirements

* **`./bserve ./www 9000`**: accept TCP, read a binary request frame, map path → file under root,
  reply with status + headers + bytes. `404` if missing, `400` if the frame is malformed. **Keep the connection open.**
* **`./bcurl -v localhost:9000/index.html`**: build the binary request, body to **stdout**,
  `-v` hexdumps **every frame** (to stderr), **exit non-zero on 4xx/5xx**, **never open a second connection**
  (so bcurl accepts several URLs and fetches them all over one socket).
* **Fixed-size frame header**: pick the fields and widths, and defend them. Explain HTTP/2's 24/8/8/31.
* **Headers**: a static table numbering the ten names we actually send, plus length-prefixed literals for
  everything else (HPACK's first two mechanisms, with no dynamic table and no Huffman coding).
* **MUST skip unknown frame types cleanly.** This is the version-2 extension point.
* Hand in: **(1) a two-page spec, enough for a stranger, (2) the programs, (3) an annotated hexdump**
  of one complete request + response.
* "A client that only works against your own server is an implementation, not a protocol."

### Protocol design (summary; full text in `binary-http/SPEC.md`)

* **Preface**: each side first sends 8 bytes `BHTTP/1\n`. A mismatch fails fast, an HTTP/1.1 peer rejects it,
  and the version byte leaves room for v2.
* **Frame header, 8 bytes, all byte-aligned, big-endian: `Length:16 | Type:8 | Flags:8 | Stream:32`.**
  * Length first. It is the only field needed to skip a frame you don't understand.
  * 16-bit length: HTTP/2's 24 bits exist so the max can be raised, but it runs at 16 KiB frames by default.
    64 KiB caps receiver memory per frame, and it forces multi-frame bodies from day one.
    Overhead ≈ 0.01 %.
  * No 24-bit or 31-bit fields: the header parses with one `struct.unpack("!HBBI")` / `DataView` call in any language.
* **Types**: `DATA 0x0`, `HEADERS 0x1`, `GOAWAY 0x2`. `0xF0–0xFF` are reserved "grease" types that
  implementations send on purpose (`--grease`) to prove peers skip them. Any unknown type → skip `Length` bytes.
* **Flag**: `END_STREAM 0x1`, which ends the message.
* **Header block**: `index:u8` (1–10 static name, 0 = literal name `u16 len + bytes`), then value `u16 len + bytes`.
  Static table (exactly the ten names sent): `:method :path :authority :status content-type
  content-length date server user-agent accept`.
* **Errors mirror part A**: a malformed header block is a *stream error*, so the server answers `400` and
  keeps the connection. A broken frame sequence or bad preface is a *connection error*, so the server
  sends `GOAWAY` + reason and closes.

### Files

```
binary-http/
  SPEC.md          the two-page protocol spec (deliverable 1)
  HEXDUMP.md       annotated hexdump of a real request + response (deliverable 3)
  bhttp/           shared library: frames.py, headers.py, hexdump.py
  bserve.py bcurl.py              programs (deliverable 2)
  bserve bcurl                    POSIX launchers   (./bserve ./www 9000)
  bserve.cmd bcurl.cmd            Windows launchers (.\bserve.cmd .\www 9000)
  www/             sample site (small index.html so the hexdump stays readable)
  tools/annotate.py               produces the annotated dump from real captured bytes
  interop/         clean-room JS client + server written ONLY from SPEC.md
  tests/           unit + end-to-end + interop matrix
```

---

## Execution order

1. **Calculator**: server → tests → marker replay → README.
2. **SPEC.md first** (the spec *is* the project), then hand it to an independent agent that builds a
   Node.js client + server **from the spec alone**, in parallel with step 3.
3. **Python `bhttp` lib → `bserve` → `bcurl`**, with unit tests for framing and the header codec, and
   end-to-end tests (multi-frame files, HEAD, 404/405/400, pipelining, grease, idle GOAWAY, path traversal).
4. **Interop matrix**: py↔py, js-client→py-server, py-client→js-server. Any failure is a spec ambiguity →
   fix the spec.
5. **HEXDUMP.md** from a real captured exchange.
6. Final full verification run.

## Verification

* `py -m unittest` green in both folders.
* `calculator/marker.py` prints the exact marker transcript: 6 responses, `socket still open: True`,
  server log shows 1 connection.
* bcurl exit codes checked for 200 / 404 / 400. `-v` shows every frame.
* Interop matrix all green.

## Not doing (yet)

* No `git push` (as instructed). No git repo is initialised. Nothing leaves this machine.
