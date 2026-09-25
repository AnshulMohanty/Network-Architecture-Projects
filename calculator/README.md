# Calculator that stays on the line

An HTTP/1.1 server on a raw socket (Python standard library only: `socket`, `threading`). It answers
`/add /sub /mul /div` and keeps the TCP connection open between requests.

```sh
python server.py                 # listens on localhost:8080 (::1 and 127.0.0.1)
python marker.py                 # replays the marker's test on ONE socket
python marker.py --pipeline      # same six requests in a single send()
python -m unittest discover -s tests -v
```

Use `python3` on Linux/macOS and `python` or `py` on Windows. On the machine this was built on, Python is
uv-managed and not on the PATH, and `py server.py` fails: the `py` launcher obeys the `#!/usr/bin/env python3`
line and can't find a registered "Python 3". Use `uv run --no-project server.py` there instead.

```
s = socket.create_connection(('localhost', 8080))   # local port 54348

  GET /add?a=2&b=3           -> 200   5
  GET /sub?a=10&b=4          -> 200   6
  GET /mul?a=6&b=7           -> 200   42
  GET /div?a=1&b=0           -> 400   (400 Bad Request: division by zero)
  GET /pow?a=2&b=8           -> 404   (404 Not Found: no such operation /pow; try /add /sub /mul /div)
  POST /add                  -> 405   (405 Method Not Allowed: POST is not allowed on /add; use GET)

  socket still open: True
  1 TCP handshake, 6 responses   (same local port 54348 throughout)
```

Server log for that run: one `open`, six requests, and one `closed: client closed the connection; 6 response(s) on this one connection`.

## Behaviour

| Request | Response | Connection |
|---|---|---|
| `GET /add?a=2&b=3` (also sub, mul, div) | `200`, body `5`, `text/plain` | stays open |
| `GET /div?a=1&b=0`, `a=x`, missing / duplicate `a` or `b` | `400` + reason | stays open |
| `GET /pow…`, any unknown path | `404` | stays open |
| `POST /add` (or PUT, DELETE…) | `405`, `Allow: GET, HEAD` | stays open, **after the body has been consumed** |
| HTTP/1.1 without `Host` | `400` | stays open |
| `HEAD /add?a=2&b=3` | headers only (`Content-Length: 1`) | stays open |
| `Connection: close`, or HTTP/1.0 without `keep-alive` | answered, then closed | closes |
| broken framing (see below) | `400` / `413` / `414` / `431` / `501` / `505` with `Connection: close` | closes |

Numbers: `-?digits[.digits]` only. Arithmetic is exact (`fractions.Fraction`), so `0.1+0.2` is `0.3`, `9/3` is `3`
(not `3.0`), and `7/2` is `3.5`. `nan`, `inf`, `1e5`, `1_000` and ` 5` are all rejected, even though Python's
`float()` accepts them.

## The part that is actually hard: where does a request end?

With HTTP/1.0 the body ended at EOF. Here the socket stays open, so every request is cut out of the byte stream exactly:

* **`Reader`** ([server.py](server.py)) owns the bytes that have arrived but are not consumed yet. `read_line()`
  finds the end of the head and `discard(n)` consumes exactly `n` body bytes. Anything after that stays in
  the buffer for the next request. Nothing assumes that one `recv()` is one request.
* **Every body is read, even when the answer is an error.** `POST /add` with `Content-Length: 7` gets a 405,
  and those 7 bytes are consumed first. Otherwise `a=2&b=3` would be parsed as the next request line.
* Body length (RFC 9112 §6.3): `Transfer-Encoding: chunked` → read chunks until the `0` chunk and trailers.
  `Content-Length: n` → exactly *n* bytes. Neither → no body; the server does not wait for one.
* **Pipelining is free**: requests already in the buffer are answered in order before the next `recv()`.

Tests prove both directions of the bug: a POST body plus the next GET in one segment, a body that *looks like*
a request, a request dribbled one byte at a time, and a body split across segments. Mutation check: making
`discard()` a no-op breaks 5 of the 10 framing tests; dropping leftover bytes breaks 6.

### Two kinds of error

| | Example | What we know | Action |
|---|---|---|---|
| **Semantic** | div by zero, bad number, 404, 405, missing Host | exactly where the next request starts | answer, **keep the connection** |
| **Framing** | `Content-Length: abc`, two different Content-Lengths, CL *and* TE, bad chunk size, oversized head | nothing we can trust | answer with `Connection: close`, then close |

Requests with both Content-Length and Transfer-Encoding are refused outright. Two framings in one message is
how request smuggling starts.

## Decisions to defend

* **Idle timeout = 60 s** (`--idle-timeout`, advertised as `Keep-Alive: timeout=60`). Every idle keep-alive
  connection holds a thread, so it cannot be infinite. It must still outlast a person typing requests into
  a REPL during marking. Apache's 5 s default would fail that. nginx's 75 s is in the same range as ours.
  When it expires the server just sends FIN. No 408 is sent, because there is no request to answer.
* **Request timeout = 10 s**, a separate clock that starts at a request's first byte. This is slowloris
  protection. A client that starts a request and stalls gets `408` and is closed.
* **Listens on every address `localhost` resolves to.** On this machine (and most modern systems) `localhost`
  is `::1` first. A server bound only to `127.0.0.1` makes `create_connection(("localhost", 8080))` hit a
  refused IPv6 connection first. On Windows that costs about 2 s before it falls back to IPv4.
* **One `sendall()` per response** (head and body together), plus `TCP_NODELAY`. A client that does one
  `recv()` per request gets the whole answer. Pipelined answers don't stall on Nagle's algorithm plus
  delayed ACK.
* **Graceful close**: `shutdown(SHUT_WR)`, drain for up to 1 s, then `close()`. Closing with unread input makes the
  kernel send an RST, which can destroy the error response before the client reads it (RFC 9112 §9.6).
* **Thread per connection, capped at 64** (the 65th gets `503`). Keep-alive means connections outlive requests,
  so one slow client must not block the others. `test_concurrent_connections` checks this.
* **Bodies are capped at 1 MiB and discarded**. The calculator never uses a body; it reads one only to stay framed.
* **Responses always carry `Content-Length`.** A server needs chunked *responses* only when it does not know
  the length in advance, and a calculator always knows it.
* Strictness that is cheap and RFC-backed: space before the colon → 400, obsolete line folding → 400,
  Transfer-Encoding in HTTP/1.0 → 400, HTTP/2.0 in a request line → 505. Leniency that helps real clients:
  bare-LF line endings, blank lines between requests, `Expect: 100-continue`, absolute-form targets.

## Stretch goals

| Stretch | Status |
|---|---|
| honour `Connection: close` | done: responds with `Connection: close`, then closes |
| an idle timeout you can defend | done: 60 s idle and a separate 10 s request clock (above) |
| chunked encoding | done: chunked request bodies with sizes, extensions and trailers; strict hex, 413 over 1 MiB |
| pipelining | done: `marker.py --pipeline`, `test_pipelining_six_at_once` |
