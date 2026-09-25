# HTTP, in binary: BHTTP/1

`bserve` serves files and `bcurl` fetches them. The only thing that crosses between them is [SPEC.md](SPEC.md).

| Hand-in | File |
|---|---|
| 1. The spec: two pages, enough for a stranger | [SPEC.md](SPEC.md) (the defence of every width is in [DESIGN.md](DESIGN.md)) |
| 2. The programs | [bserve.py](bserve.py), [bcurl.py](bcurl.py), shared code in [bhttp/](bhttp/) |
| 3. Annotated hexdump of one complete request and response | [HEXDUMP.md](HEXDUMP.md), from real bytes in [captures/](captures/) |

Python 3.8+, standard library only (`socket`, `struct`, `threading`).

## Run it

```sh
./bserve ./www 9000                         # terminal 1   (Windows: .\bserve.cmd .\www 9000)
./bcurl -v localhost:9000/index.html        # terminal 2   (Windows: .\bcurl.cmd -v localhost:9000/index.html)
```

The launchers find Python 3.8+ (`python3`, then `python`, then Windows' `py`). `python bserve.py ./www 9000`
works too. On the machine this was built on, `py bserve.py` does **not** work: the `py` launcher reads the
`#!/usr/bin/env python3` line and looks for a registered "Python 3", and a uv-installed Python isn't
registered that way. The launchers work around this, and so does `uv run --no-project bserve.py ./www 9000`.

### bcurl

```
bcurl [-v | -vv] [-I] [-X METHOD] [-H 'name: value']... [-d DATA] [-p] [--grease] URL [URL ...]
      URL = [bhttp://]host[:port][/path]      default port 9000
```

| | |
|---|---|
| body | to **stdout**, raw bytes; several URLs are written one after another |
| `-v` | hexdump of **every frame** sent (`>`) and received (`<`) to **stderr**, including the preface, with decoded headers. `-vv` shows full payloads without truncating. |
| several URLs | all over **one TCP connection**. URLs naming different host:port are refused before connecting. |
| `-p` | pipeline: send every request, then read the responses in order |
| `-I` | HEAD; prints the response headers |
| `--grease` | sends a reserved-type frame before each request; the server must skip it |
| exit status | `0` all < 400 · `4` worst was 4xx · `5` worst was 5xx · `1` usage/connection error · `2` protocol error |

### bserve

```
bserve ROOT [PORT] [--host localhost] [--idle-timeout 60] [--frame-timeout 10] [--grease] [-v|-vv] [--quiet]
```

* Serves GET and HEAD. `/` and directories map to `index.html`. Other methods get 405 (`allow: GET, HEAD`),
  and `..`, bad escapes and backslashes get 400.
* Listens on every address `localhost` resolves to, both `::1` and `127.0.0.1`.
* One line per request on stderr. The connection stays open until the client closes it. An idle connection
  gets GOAWAY(NO_ERROR) after 60 s; a stalled frame gets GOAWAY(TIMEOUT) after 10 s.

```
[conn 3 ::1:52938] open
[conn 3 ::1:52938] stream 1 GET /hello.txt -> 200 (20 octets in 1 DATA frame(s))
[conn 3 ::1:52938] stream 2 GET /docs/ -> 200 (94 octets in 1 DATA frame(s))
[conn 3 ::1:52938] stream 3 GET /big.txt -> 200 (70000 octets in 5 DATA frame(s))
[conn 3 ::1:52938] closed: client closed the connection; 3 response(s) on this one connection
```

## Tests

```sh
python -m unittest discover -s tests -v
```

| Suite | Tests | What it proves |
|---|---|---|
| `test_codec.py` | 17 | exact wire bytes (`00 37 01 01 00 00 00 01`); frames split and merged across segments; a payload that *looks like* a frame isn't parsed; every malformed-block rule |
| `test_e2e.py` | 25 | every status and exit code; 7-frame bodies; empty files; HEAD; path traversal (10 forms); stream errors keep the connection; 10 connection errors end in GOAWAY; unknown types everywhere (on stream 0, mid-message, 65535 octets); grease both ways; idle and stalled timeouts |
| `test_interop.py` | 22 | Python against the clean-room **JavaScript** implementation, in both directions (needs `node`) |

## Was the spec enough for a stranger?

That was tested rather than assumed. An independent implementer saw only `SPEC.md` and wrote a Node.js client
and server ([interop/js/](interop/js/)). The two implementations interoperated on the first try. The
implementer's list of unclear passages ([interop/js/NOTES.md](interop/js/NOTES.md)) produced revision 2 of the
spec text, and a second round produced revision 3. Neither changes a wire octet. The details are in [DESIGN.md §6](DESIGN.md).

## Layout

```
SPEC.md DESIGN.md HEXDUMP.md   the protocol, its defence, the annotated bytes
bserve.py bcurl.py             the programs
bserve bcurl                   POSIX launchers
bserve.cmd bcurl.cmd           Windows launchers
bhttp/frames.py                preface, 8-octet header, FrameReader (cuts frames from the stream)
bhttp/headers.py               static table, header-block encode/decode, validity rules
bhttp/hexdump.py               the -v output
bhttp/net.py                   listen on every address of a name; close without an RST
www/                           sample site (index.html kept tiny so the hexdump stays readable)
tools/annotate.py              captures a real exchange and annotates every octet
captures/                      the raw bytes behind HEXDUMP.md
interop/js/                    the clean-room Node.js implementation, its self-test and notes
tests/                         unit, end-to-end and interop tests
```
