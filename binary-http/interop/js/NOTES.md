# Clean-room notes: where SPEC.md was ambiguous, and what I guessed

This implementation was written from `binary-http/SPEC.md` alone. Each entry gives the
spec section, what was unclear, and what I did. Items marked **[contradiction]** are places
where I think the spec is wrong or disagrees with itself.

## §1 Preface
1. **Fail fast?** "A receiver reads exactly 8 octets." Can it reject early on the first
   byte that doesn't match? *Assumed:* no. It waits for all 8 octets, then compares.
2. **Partial or missing preface.** No timeout is given for it. *Assumed:* a partial preface
   counts as "a frame that has started", so after 10 s the server sends GOAWAY(TIMEOUT). No
   bytes at all counts as idle, so after 60 s it sends GOAWAY(NO_ERROR).
3. **GOAWAY after a bad preface.** §6 says to send GOAWAY(PROTOCOL_ERROR) to a peer that
   just showed it doesn't speak BHTTP. The server does this. The client does not: on a bad
   server preface it just closes and exits 2.
4. **Requests before the peer's preface.** "Without waiting" was read to mean the client can
   send its preface and first request before the server preface arrives. bcurl does this.

## §2 Framing / unknown types
5. **Grease flags.** "Senders MUST send undefined bits as 0." For a reserved type, every flag
   bit is undefined, so grease must have flags = 0. That limits what grease can exercise.
   My grease uses flags 0. The self-test also sends non-zero flags to check that receivers
   ignore them.
6. **Stream IDs on unknown frames.** "Changes no state" was read as: the stream ID is ignored
   completely. It doesn't count as "used" for the increasing-ID rule, it can sit on any
   stream (0, stale or future), and it doesn't reset timers. My grease is sent on the
   stream ID of the next message to test exactly this.
7. **Idle timer and unknown frames.** Following "changes no state", unknown frames do not
   reset the 60 s idle timer, so grease can't keep a connection alive. Tested.

## §3 Messages, streams, GOAWAY
8. **"In progress".** "HEADERS while a request is still in progress" could mean "request
   not yet complete" or "not yet answered". The second reading would make pipelining a
   connection error. *Assumed:* the HEADERS has arrived but END_STREAM hasn't.
9. **[contradiction] GOAWAY in the middle of a request.** §3 says only that request's DATA
   (and unknown types) may appear before END_STREAM. §6's list of connection errors does not
   include a GOAWAY in that position. *Assumed:* it is a connection error (PROTOCOL_ERROR).
10. **Content-length mismatch.** The spec says what MUST hold but not what the receiver does
    if it doesn't. *Assumed:* in a request it is a stream error (400, detected at
    END_STREAM). In a response it is malformed (exit 2). The spec also doesn't cover
    duplicate or non-numeric values. I treat both as malformed and accept `[0-9]{1,15}`.
11. **HEAD exemption.** Is a zero-length DATA frame allowed in a HEAD response? *Assumed:*
    no, because §5 says it is "the same HEADERS frame with END_STREAM set". The client
    rejects any DATA frame, or a missing END_STREAM, in a HEAD response. The server applies
    this to all HEAD responses, including 404s. 204 and 304 get no exemption.
12. **Malformed requests and stream IDs.** Does a malformed request still use up its stream
    ID? *Assumed:* yes. Gaps such as 1, 3, 5 are allowed.
13. **Pipelined requests on error.** When a connection error happens, what about requests
    that are queued and complete but not yet answered? *Assumed:* the response being sent
    finishes, the queued requests are dropped, and GOAWAY's last_stream_id reports that.
    The server never puts a GOAWAY in the middle of a response, except when a file read
    fails mid-body: then it sends GOAWAY(INTERNAL_ERROR), because the stream can't be
    finished.
14. **"Last processed stream".** Not defined. *Assumed:* the highest stream whose response
    was completely written.
15. **GOAWAY from the client.** The spec doesn't say what it means or how the server should
    react. *Assumed:* the server answers queued requests with stream ≤ last_stream_id, drops
    the rest, sends GOAWAY(NO_ERROR), and closes. Also, §5's list of reasons for the server
    to close ("only after the client closes, …") leaves out a client GOAWAY.
16. **Is GOAWAY required before every close?** "It is sent before the sender closes" could
    mean every close. *Assumed:* yes, both ways. bcurl sends GOAWAY(NO_ERROR) after the last
    response. bserve sends GOAWAY(NO_ERROR) even after a clean client EOF. A peer that
    doesn't expect a GOAWAY from the client may react badly.
17. **Client after an error.** "Reports the failure and closes." Should the client send
    GOAWAY first? For a malformed response bcurl sends GOAWAY(PROTOCOL_ERROR). After an
    error GOAWAY or a bad preface it just closes.
18. **Graceful GOAWAY that covers the client's stream.** If last_stream_id ≥ the client's
    stream, the response may still follow, so bcurl keeps reading. If it is below, the
    request was not processed, and with one connection bcurl can't retry: it exits 2.
19. **Client-side connection errors.** §6 only lists server-side triggers. *Assumed* the
    mirror image for the client. These are all protocol errors (exit 2): DATA/HEADERS on
    stream 0, a frame on the wrong stream, DATA before HEADERS, a second HEADERS (no
    trailers), and a short GOAWAY or one on stream ≠ 0.
20. **Client EOF.** On a truncated frame or request the server sends GOAWAY(PROTOCOL_ERROR).
    On a clean half-close it still answers the complete requests it already has.

## §4 Header block
21. **[contradiction] The `allow` header.** "The table lists exactly the ten names the
    reference client and server send", but §5 requires the server to send `allow`, which is
    not in the table. I read "literal header" as a literal-name field (index 0).
22. **Redundant rule.** The rule against a leading ":" is redundant, because ":" is not in
    the allowed character set anyway. Uppercase is not allowed, so bcurl lowercases `-H`
    names.
23. **Syntax that isn't specified:** method (`get`, or an empty value), empty `:authority`,
    duplicate regular headers, and `:status` values such as `000`, `1xx` or `999`.
    *Assumed:* any method other than exactly `GET`/`HEAD` gets 405, not 400. Empty values
    and duplicates are accepted. Any three digits is a final status, and bcurl exits 5 for
    anything ≥ 500. `:authority` is required but its content isn't defined: bcurl sends
    `host[:port]` as typed and bserve ignores it.
24. **Value encoding.** Values are "octets" and may contain non-ASCII bytes and spaces.
    They are kept as bytes and shown with `\xNN` escapes.

## §5 Server behaviour
25. **Order of checks.** Not given. *Assumed:* header-block validity (400) first, then the
    method (405), then the path (400/404). So `POST /../x` gets 405.
26. **Decoded `%2F`.** Is it a segment separator for the `.`/`..` check? *Assumed:* yes.
    Segments are split after decoding, so `/..%2fx` gets 400. The NUL and `\` checks also
    apply after decoding.
27. **Cases the path rules don't cover:**
    - Invalid UTF-8 after decoding: 404.
    - Empty segments (`//`): collapsed.
    - Trailing slash on a file (`/index.html/`): 404.
    - Directory without a trailing slash (`/sub`): its index.html is served directly. There
      is no 3xx, so relative links in that page break for a browser-like client.
    - `...` is allowed.
28. **"Outside the root" is under-specified** (symlinks, Windows). *Assumed:* the file's
    realpath must be inside the root's realpath, otherwise 404. On Windows, a segment that
    contains `:` gets 404, because it would address an NTFS stream or a drive. Device names
    (NUL, CON) fail the realpath check. Trailing-dot and case aliasing are also resolved by
    realpath.
29. **Error-response headers.** Only content-type and the body are given. I also send
    content-length, date and server.
30. **Missing status codes and details:**
    - An unreadable file (EACCES) gets 500. The spec defines no 403 or 500.
    - content-type mapping is not specified. I use a small extension table, defaulting to
      application/octet-stream.
    - An empty file is sent as a single HEADERS frame with END_STREAM.
    - A body on GET/HEAD is read and discarded.
    - The 16384-octet DATA limit is taken to apply to this server only. The client accepts
      frames up to 65535.
31. **What starts the timers?** *Assumed:*
    - The idle timer runs from accept, or from the end of the last response.
    - It is paused while a frame is half received, because the 10 s rule covers that case.
    - The 10 s clock for a frame starts at its first octet. For a message it starts at the
      HEADERS frame, and DATA arriving later doesn't extend it.
    - There is no timer while the server is writing to a client that never reads. The spec
      has no send timeout.
    - After a GOAWAY, the server half-closes and discards input for 2 s, so a RST doesn't
      destroy the GOAWAY. No linger time is given.
32. **Stream-ID exhaustion** (2³²−1) isn't mentioned.

## §7 Rationale nits
33. **Signed 32-bit integers.** §7 gives the HTTP/2 reserved bit's purpose as keeping the ID
    positive in languages that only have signed 32-bit integers. BHTTP drops that bit, so
    IDs ≥ 2³¹ go negative in such languages (in JS, `|0`). I used `>>> 0` and
    `readUInt32BE`.
34. **"One DataView call".** A DataView can't read all four header fields in one call, as
    §7 claims. Minor.

## Revision 2

I re-read SPEC.md text revision 2 end to end. (It points to DESIGN.md for rationale; I did not
read that file, under the clean-room rules.) Status of each original note:

1. **Resolved.** A receiver MAY give up at the first differing octet. Both bserve and bcurl
   now do.
2. **Resolved, as I guessed.** Waiting for the preface counts as idle (60 s); a partial
   preface gets 10 s and then TIMEOUT.
3. **Resolved, differently.** §3's "MUST send GOAWAY before closing because of an error"
   applies to clients too. bcurl now sends GOAWAY(PROTOCOL_ERROR) even after a bad server
   preface.
4. **Resolved, as I guessed.**
5. **Resolved, differently.** The zero-flags rule covers only the three defined types; an
   unknown frame may carry any flags. Grease uses random flags again.
6. **Resolved, as I guessed.** An unknown frame "does not open or use up a stream".
7. **Resolved, differently.** The idle rule is "no frame has been received for 60 s", so grease
   now restarts the idle clock. Mild disagreement: 8 octets every 59 s keeps a connection open
   forever. That is no worse than repeated HEAD requests, so I don't object hard.
8. **Resolved, as I guessed.**
9. **Resolved, differently.** A GOAWAY during a request is legal: the server drops the request
   in progress and closes. Changed.
10. **Mostly resolved.** A mismatch is malformed: 400 for a request, exit 2 for a response.
    Still inferred: repeats and leading zeros. The spec says names may repeat and each value
    MUST be digits equal to the total, so bcurl and bserve accept `3` + `003` and reject
    `3` + `4`.
11. **Resolved, as I guessed.** Now open (see N5): is a HEAD response *without*
    content-length malformed? bcurl accepts it.
12. **Resolved implicitly.** "Greater than every earlier one" means any earlier HEADERS stream
    ID.
13. **Still open.** On a connection error, may or must the server answer complete queued
    requests first? May a GOAWAY cut into a response already being sent? bserve finishes the
    current response and drops the queue.
14. **Resolved, as I guessed.** last_stream_id is the highest stream "completely answered".
15. **Resolved, differently.** Clients always send 0. bserve used to drop queued requests
    above the client's last_stream_id, which with 0 would have dropped all of them. It now
    answers every complete request, drops the one in progress, and closes without a GOAWAY of
    its own. See also N2 and N11.
16. **Resolved.** GOAWAY is mandatory only on an error or timeout, and optional for a client
    that is done. bserve no longer sends GOAWAY after a clean client EOF or a client GOAWAY.
    bcurl still sends GOAWAY(NO_ERROR, 0) when done, which is allowed.
17. **Partly open.** bcurl now always sends GOAWAY before an error close. Unresolved: which
    code when closing *because the server sent an error GOAWAY*? I send NO_ERROR.
18. **Resolved.** A last_stream_id at or above the client's stream now means the response was
    already complete.
19. **Mostly resolved.** There is now a client row in §6. Gap: it omits GOAWAY on stream ≠ 0,
    or shorter than 8 octets, which only the server row lists. bcurl still treats both as
    protocol errors.
20. **Still open.** "The server closes when the client closes" doesn't say whether complete
    pipelined requests are still answered after a half-close. I answer them, by analogy with
    GOAWAY. A truncated frame or request at EOF is dropped without a GOAWAY; is that "an
    error"?
21. **Resolved.** `allow` is explicitly a literal.
22. **Resolved.**
23. **Mostly resolved.** A `:method` that is not a token gets 400; one that is not exactly
    GET/HEAD gets 405. `:authority` is ignored, and names may repeat. "Token" is still not
    defined; I use RFC 9110 tchar, with uppercase, because literal names are called a
    "lowercase token". The meaning of statuses 1xx and ≥ 600 is still unstated.
24. **Resolved.**
25. **Resolved, as I guessed.** `POST /../x` gets 405.
26. **Resolved, as I guessed.**
27. **Resolved, differently.** Invalid UTF-8 now gets 400 (I had 404). "Empty segments
    collapse" makes `/index.html/` and `/sub/page.txt/` get 200 (I had 404). I disagree mildly:
    this makes a file an alias of a pretend directory, and I doubt it was intended. `...` is
    still unmentioned; it is allowed.
28. **Partly open.** "Resolving outside the root" suggests symlinks are resolved, which matches
    my realpath check. Nothing is said about Windows: NTFS `name:stream`, device names such as
    NUL and CON, or trailing-dot aliases. I keep 404 for any segment containing `:`.
29. **Resolved, as I guessed.** Every response carries all five headers.
30. **Mostly resolved.** An unreadable file gets 500, and the 16384 limit binds senders only.
    Still unspecified: the content-type mapping, how an empty body is sent, and whether a
    request body on GET is ignored.
31. **Mostly resolved.** Open: taken literally, the idle rule fires *during* a response that
    takes longer than 60 s to reach a slow reader. That condition ("no request in progress,
    no frame received") ignores what the server is sending. bserve doesn't run the idle clock
    while answering, and restarts it when the last response ends. There is still no send
    timeout, and "briefly" is undefined (bserve 2 s, bcurl 1 s).
32. **Still open.** The ID range is stated, but nothing says what happens at 2³²−1.
33. **Resolved.** Stream IDs are read as unsigned.
34. **Resolved.**

### New in revision 2
- **N1.** SPEC.md now points to DESIGN.md. If anything normative lives there, a clean-room
  implementer misses it. I assume it is rationale only.
- **N2.** "A server that receives GOAWAY *has already answered* every complete request that
  arrived before it" reads as a description, and is only true of a server that doesn't read
  ahead. I treat it as an obligation.
- **N3.** What should a server do with a non-zero last_stream_id from a client, such as a
  rev-1 client? bserve ignores it.
- **N4.** Nothing says whether a client may time out on its own. bcurl gives up after 30 s of
  silence and sends GOAWAY(TIMEOUT).
- **N5.** §5 says every response carries content-length, but §3 says "if content-length is
  present" and §4 doesn't list its absence as malformed. So is a response without
  content-length malformed? bcurl accepts it.
- **N6.** **[contradiction]** The server must answer every complete request that arrived
  before a client GOAWAY. But the GOAWAY sender "half-closes and discards input briefly", so
  it throws those responses away. A client should therefore only send GOAWAY after it has
  read everything, and the spec should say so.
- **N7.** The client row of §6 says only "report it, then close". The GOAWAY duty lives in
  §3, easy to miss, and it means sending a BHTTP GOAWAY even to a peer that just failed the
  preface.
