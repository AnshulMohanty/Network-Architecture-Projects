# Network Architecture: assignments

Python 3.8+, standard library only, raw sockets throughout. The analysis and plan are in [PLAN.md](PLAN.md).

| | What | Where | Run |
|---|---|---|---|
| **Assignment** (before session 7) | *Build a calculator that stays on the line*: HTTP/1.1 keep-alive on a raw socket, plus a browser UI at `/` that shows one connection carrying every request | [calculator/](calculator/) | `python server.py`, open <http://localhost:8080>, then `python marker.py` |
| **Course project** | *HTTP, in binary*: design the framing, write the spec, build `bserve` + `bcurl` | [binary-http/](binary-http/) | `./bserve ./www 9000`, then `./bcurl -v localhost:9000/index.html` |

## Checklist against the brief

**Calculator** ([README](calculator/README.md))
- [x] add / sub / mul / div → 200; div by zero, bad number, no Host → 400; `/pow` → 404; POST → 405
- [x] one socket, every request: `marker.py` shows *socket still open: True, 1 TCP handshake, 6 responses*
- [x] exactly Content-Length bytes consumed; byte n+1 left for the next request (10 framing tests, mutation-checked)
- [x] stretch: `Connection: close`, a defended idle timeout (60 s, plus a separate 10 s request clock), chunked request bodies, pipelining
- [x] extra: a one-file UI served by the same raw-socket server; `X-Conn-Id` / `X-Conn-Request` headers show which connection answered, and it runs the marking sequence in the browser

**Binary HTTP** ([README](binary-http/README.md))
- [x] fixed 8-octet frame header, 16/8/8/32, defended against HTTP/2's 24/8/8/31 ([DESIGN.md](binary-http/DESIGN.md))
- [x] ten static header names plus length-prefixed literals
- [x] unknown frame types MUST be skipped, with reserved "grease" types that prove it
- [x] `bserve`: path → file under a root, 404, 400 on a malformed frame, connection kept open
- [x] `bcurl`: body to stdout, `-v` hexdumps every frame, non-zero exit on 4xx/5xx, never a second connection
- [x] hand-in 1: the two-page spec ([SPEC.md](binary-http/SPEC.md))
- [x] hand-in 2: the programs
- [x] hand-in 3: annotated hexdump of a real request and response ([HEXDUMP.md](binary-http/HEXDUMP.md))
- [x] "a client that only works against your own server is an implementation, not a protocol": a clean-room
      JavaScript implementation written from the spec alone interoperates with ours

## Tests

```sh
(cd calculator  && python -m unittest discover -s tests -v)   # 34 tests
(cd binary-http && python -m unittest discover -s tests -v)   # 64 tests (22 need node for interop)
```
