#!/usr/bin/env node
// bserve.mjs - BHTTP/1 static file server, written from SPEC.md only.
// usage: node bserve.mjs <docroot> <port> [--grease]

import net from 'node:net';
import fsp from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import crypto from 'node:crypto';
import {
  PREFACE, MAX_DATA_CHUNK, T_DATA, T_HEADERS, T_GOAWAY, F_END_STREAM,
  E_NO_ERROR, E_PROTOCOL_ERROR, E_INTERNAL_ERROR, E_TIMEOUT, errorName,
  encodeFrame, encodeGoaway, decodeGoaway, encodeHeaderBlock, decodeHeaderBlock,
  validateRequestFields, parseContentLength, printable,
} from './bhttp.mjs';

const IDLE_MS = 60_000;      // §5: idle with no request in progress -> GOAWAY(NO_ERROR)
const PROGRESS_MS = 10_000;  // §5: frame or message started but unfinished -> GOAWAY(TIMEOUT)
const LINGER_MS = 2_000;     // after GOAWAY + FIN, how long we keep draining before destroy
const SERVER_NAME = 'bserve.mjs/1';

const MIME = {
  '.html': 'text/html; charset=utf-8', '.htm': 'text/html; charset=utf-8',
  '.txt': 'text/plain; charset=utf-8', '.md': 'text/markdown; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8', '.json': 'application/json',
  '.xml': 'application/xml', '.svg': 'image/svg+xml', '.png': 'image/png',
  '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif',
  '.webp': 'image/webp', '.ico': 'image/x-icon', '.pdf': 'application/pdf',
  '.wasm': 'application/wasm', '.bin': 'application/octet-stream',
};

function usage(msg) {
  if (msg) process.stderr.write(`bserve: ${msg}\n`);
  process.stderr.write('usage: node bserve.mjs <docroot> <port> [--grease]\n');
  process.exit(1);
}

let grease = false;
const positional = [];
for (const a of process.argv.slice(2)) {
  if (a === '--grease') grease = true;
  else if (a === '-h' || a === '--help') usage();
  else if (a.startsWith('--')) usage(`unknown option ${a}`);
  else positional.push(a);
}
if (positional.length !== 2) usage();
const [docrootArg, portArg] = positional;
if (!/^[0-9]+$/.test(portArg) || +portArg < 1 || +portArg > 65535) usage(`bad port ${portArg}`);
const port = +portArg;

let ROOT;
try {
  ROOT = await fsp.realpath(path.resolve(docrootArg));
  if (!(await fsp.stat(ROOT)).isDirectory()) usage(`${docrootArg} is not a directory`);
} catch (e) {
  usage(`cannot use docroot ${docrootArg}: ${e.message}`);
}

function log(line) {
  process.stderr.write(`${new Date().toISOString()} ${line}\n`);
}

function greaseFrame(stream) {
  const type = 0xf0 + crypto.randomInt(16);
  // §2: an unknown frame may carry any flags and any stream ID.
  return encodeFrame(type, crypto.randomInt(256), stream, crypto.randomBytes(1 + crypto.randomInt(8)));
}

// ---------------------------------------------------------------- path mapping (§5)

class HttpError extends Error {
  constructor(status, reason) { super(reason); this.status = status; }
}

function percentDecode(buf) {
  const out = Buffer.allocUnsafe(buf.length);
  let o = 0;
  const hexv = (c) => (c >= 0x30 && c <= 0x39 ? c - 0x30
    : c >= 0x41 && c <= 0x46 ? c - 0x37
      : c >= 0x61 && c <= 0x66 ? c - 0x57 : -1);
  for (let i = 0; i < buf.length; i++) {
    const c = buf[i];
    if (c !== 0x25) { out[o++] = c; continue; }
    const h = i + 2 < buf.length ? hexv(buf[i + 1]) : -1;
    const l = i + 2 < buf.length ? hexv(buf[i + 2]) : -1;
    if (h < 0 || l < 0) throw new HttpError(400, 'bad percent-escape in :path');
    out[o++] = (h << 4) | l;
    i += 2;
  }
  return out.subarray(0, o);
}

const utf8 = new TextDecoder('utf-8', { fatal: true });
const NOT_FOUND_CODES = new Set(['ENOENT', 'ENOTDIR', 'EISDIR', 'EINVAL', 'ENAMETOOLONG', 'ELOOP']);

function notFound(reason) { return new HttpError(404, reason); }

// Returns {fh, size, realPath}; throws HttpError(400|404|500).
async function openForPath(rawPath) {
  let raw = rawPath;
  let cut = raw.length;
  for (let i = 0; i < raw.length; i++) if (raw[i] === 0x3f || raw[i] === 0x23) { cut = i; break; }
  raw = raw.subarray(0, cut);
  const dec = percentDecode(raw);
  if (dec.includes(0x00)) throw new HttpError(400, 'NUL in :path');
  if (dec.includes(0x5c)) throw new HttpError(400, 'backslash in :path');
  let str;
  try { str = utf8.decode(dec); } catch { throw new HttpError(400, ':path is not valid UTF-8 after percent-decoding'); }
  const segs = str.split('/');
  for (const s of segs) if (s === '.' || s === '..') throw new HttpError(400, `"${s}" segment in :path`);
  // §5 step 4: empty segments collapse (so a trailing "/" on a file name is ignored).
  const parts = segs.filter((s) => s !== '');
  if (process.platform === 'win32' && parts.some((s) => s.includes(':'))) {
    throw notFound('not found'); // would address an NTFS stream or a drive
  }
  let target = path.join(ROOT, ...parts);
  try {
    let st = await fsp.stat(target);
    if (st.isDirectory()) target = path.join(target, 'index.html');
    const real = await fsp.realpath(target);
    const rel = path.relative(ROOT, real);
    if (rel === '..' || rel.startsWith('..' + path.sep) || path.isAbsolute(rel)) throw notFound('outside the document root');
    const fh = await fsp.open(real, 'r');
    try {
      st = await fh.stat();
      if (!st.isFile()) throw notFound('not a regular file');
      return { fh, size: st.size, realPath: real };
    } catch (e) {
      await fh.close().catch(() => {});
      throw e;
    }
  } catch (e) {
    if (e instanceof HttpError) throw e;
    if (NOT_FOUND_CODES.has(e.code)) throw notFound('not found');
    throw new HttpError(500, `cannot open file (${e.code ?? e.message})`);
  }
}

const REASONS = { 400: 'Bad Request', 404: 'Not Found', 405: 'Method Not Allowed', 500: 'Internal Server Error' };

// ---------------------------------------------------------------- connection

let connSeq = 0;

class Connection {
  constructor(sock) {
    this.sock = sock;
    this.id = ++connSeq;
    this.peer = `${sock.remoteAddress}:${sock.remotePort}`;
    this.buf = Buffer.alloc(0);
    this.gotPreface = false;
    this.lastStreamId = 0;    // highest stream ID seen on a request HEADERS
    this.lastProcessed = 0;   // highest stream ID whose response was fully sent
    this.cur = null;          // request message in progress (HEADERS seen, END_STREAM not yet)
    this.queue = [];          // complete requests waiting for their response (pipelining)
    this.serving = false;
    this.closing = false;     // stop interpreting input
    this.closeWith = null;    // {code, reason, goaway}: how to close once the queue is drained
    this.goawaySent = false;
    this.ended = false;       // we have sent FIN
    this.destroyed = false;
    this.partialSince = null; // time the currently buffered, incomplete frame (or preface) began
    this.msgSince = null;     // time the in-progress request message began
    this.lastActivity = Date.now(); // last complete frame received, or last response finished
    this.timer = null;
    this.timerAt = 0;

    sock.setNoDelay(true);
    sock.on('data', (d) => this.onData(d));
    sock.on('end', () => this.onEnd());
    sock.on('error', (e) => {
      if (!this.ended) log(`conn#${this.id} ${this.peer} socket error: ${e.code ?? e.message}`);
      this.destroyed = true;
    });
    sock.on('close', () => {
      this.destroyed = true;
      clearTimeout(this.timer);
      this.timer = null;
    });
    sock.write(PREFACE);
    this.schedule();
  }

  // ---- input

  onData(chunk) {
    if (this.closing || this.destroyed) return; // draining after GOAWAY / peer GOAWAY
    this.buf = this.buf.length ? Buffer.concat([this.buf, chunk]) : chunk;
    let consumed = false;
    while (!this.closing) {
      if (!this.gotPreface) {
        // §1: give up at the first octet that differs (MAY), otherwise wait for all 8.
        const n = Math.min(this.buf.length, PREFACE.length);
        const p = this.buf.subarray(0, n);
        if (!p.equals(PREFACE.subarray(0, n))) {
          this.connError(E_PROTOCOL_ERROR, `bad connection preface ${JSON.stringify(p.toString('latin1'))}`);
          break;
        }
        if (n < PREFACE.length) break;
        this.buf = this.buf.subarray(PREFACE.length);
        consumed = true;
        this.gotPreface = true;
        continue;
      }
      if (this.buf.length < 8) break;
      const len = this.buf.readUInt16BE(0);
      if (this.buf.length < 8 + len) break;
      const type = this.buf[2];
      const flags = this.buf[3];
      const stream = this.buf.readUInt32BE(4);
      const payload = this.buf.subarray(8, 8 + len);
      this.buf = this.buf.subarray(8 + len);
      consumed = true;
      this.onFrame(type, flags, stream, payload);
    }
    if (this.closing) return;
    // §5 idle rule: "no frame has been received for 60 s" -- any complete frame counts,
    // unknown/grease frames included.
    if (consumed) this.lastActivity = Date.now();
    if (consumed || this.buf.length === 0) this.partialSince = null;
    if (this.buf.length > 0 && this.partialSince === null) this.partialSince = Date.now();
    this.schedule();
  }

  onFrame(type, flags, stream, payload) {
    const end = (flags & F_END_STREAM) !== 0;
    if (type === T_GOAWAY) {
      if (stream !== 0) return this.connError(E_PROTOCOL_ERROR, `GOAWAY on stream ${stream}`);
      if (payload.length < 8) return this.connError(E_PROTOCOL_ERROR, `GOAWAY payload is ${payload.length} octets (< 8)`);
      return this.onPeerGoaway(decodeGoaway(payload)); // allowed even while a request is in progress (§3)
    }
    if (type === T_HEADERS) {
      if (stream === 0) return this.connError(E_PROTOCOL_ERROR, 'HEADERS on stream 0');
      if (this.cur) return this.connError(E_PROTOCOL_ERROR, `HEADERS on stream ${stream} while the request on stream ${this.cur.stream} is in progress`);
      if (stream <= this.lastStreamId) return this.connError(E_PROTOCOL_ERROR, `HEADERS stream ID ${stream} is not above the previous one (${this.lastStreamId})`);
      this.lastStreamId = stream;
      const req = { stream, bodyLen: 0, error: null, method: null, path: null, contentLength: null, t0: Date.now() };
      const { fields, error } = decodeHeaderBlock(payload);
      const methods = fields.filter((f) => f.name === ':method');
      const paths = fields.filter((f) => f.name === ':path');
      if (methods.length === 1) req.method = methods[0].value.toString('latin1');
      if (paths.length === 1) req.path = paths[0].value;
      req.error = error ?? validateRequestFields(fields);
      if (!req.error) {
        const cl = parseContentLength(fields);
        if (cl.error) req.error = cl.error;
        else req.contentLength = cl.value;
      }
      if (end) return this.finishRequest(req);
      this.cur = req;
      this.msgSince = Date.now();
      return;
    }
    if (type === T_DATA) {
      if (stream === 0) return this.connError(E_PROTOCOL_ERROR, 'DATA on stream 0');
      if (!this.cur || this.cur.stream !== stream) return this.connError(E_PROTOCOL_ERROR, `DATA on stream ${stream} with no request in progress on that stream`);
      this.cur.bodyLen += payload.length;
      if (end) {
        const req = this.cur;
        this.cur = null;
        this.msgSince = null;
        this.finishRequest(req);
      }
      return;
    }
    // Unknown type (including grease 0xF0-0xFF), on any stream: skip, no state change (§2).
  }

  finishRequest(req) {
    if (!req.error && req.contentLength !== null && req.contentLength !== req.bodyLen) {
      req.error = `content-length is ${req.contentLength} but DATA totals ${req.bodyLen} octets`;
    }
    this.queue.push(req);
    this.pump();
  }

  // §3: "A server that receives GOAWAY has already answered every complete request that arrived
  // before it. It drops any request in progress, then closes." Clients always send
  // last_stream_id 0, so it is not used to filter anything. The close is not caused by an error
  // or timeout on our side, so no GOAWAY is sent back.
  onPeerGoaway(g) {
    log(`conn#${this.id} ${this.peer} peer GOAWAY last_stream_id=${g.lastStreamId} ${errorName(g.errorCode)}${g.debug ? ` "${printable(Buffer.from(g.debug))}"` : ''}`
      + `${this.cur ? `; dropping request in progress on stream ${this.cur.stream}` : ''}`);
    this.closing = true;
    this.cur = null;
    this.partialSince = this.msgSince = null;
    this.closeWith = { code: E_NO_ERROR, reason: 'peer sent GOAWAY', goaway: false };
    if (!this.serving) this.afterQueue();
    this.schedule();
  }

  // Client closed its side: answer the complete requests already received, drop anything
  // partial, then close. No GOAWAY: the client closed, we are not closing on an error/timeout.
  onEnd() {
    if (this.ended || this.closing) return; // already on the way out
    if (this.buf.length > 0 || this.cur !== null) {
      log(`conn#${this.id} ${this.peer} client closed in the middle of a ${this.cur ? `request (stream ${this.cur.stream})` : this.gotPreface ? 'frame' : 'preface'}; dropped`);
    }
    this.closing = true;
    this.cur = null;
    this.partialSince = this.msgSince = null;
    this.closeWith = { code: E_NO_ERROR, reason: 'client closed the connection', goaway: false };
    if (!this.serving) this.afterQueue();
    this.schedule();
  }

  // ---- errors / shutdown

  connError(code, reason) {
    if (this.ended || this.destroyed) return;
    if (this.closeWith && this.closeWith.code !== E_NO_ERROR) return;
    log(`conn#${this.id} ${this.peer} connection error ${errorName(code)}: ${reason}`);
    this.closing = true;
    this.cur = null;
    this.queue.length = 0; // complete but unanswered requests are dropped; GOAWAY's last_stream_id says so
    this.partialSince = this.msgSince = null;
    this.closeWith = { code, reason, goaway: true };
    if (!this.serving) this.afterQueue();
    this.schedule();
  }

  // §3: GOAWAY is mandatory before closing because of an error or a timeout.
  // §5: then half-close and discard input briefly so a RST cannot destroy the GOAWAY.
  sendGoawayAndClose(code, reason) {
    if (this.goawaySent || this.ended || this.destroyed) return;
    this.goawaySent = true;
    this.closeQuietly(encodeGoaway(this.lastProcessed, code, reason));
  }

  closeQuietly(lastBytes) {
    if (this.ended || this.destroyed) return;
    this.ended = true;
    this.closing = true;
    clearTimeout(this.timer);
    this.timer = null;
    if (lastBytes) this.sock.end(lastBytes); else this.sock.end();
    setTimeout(() => this.sock.destroy(), LINGER_MS).unref();
  }

  // ---- timers (§5)

  deadline() {
    let d = null;
    const starts = [this.partialSince, this.msgSince].filter((x) => x !== null);
    if (starts.length) d = Math.min(...starts) + PROGRESS_MS;
    if (this.isIdle()) {
      const i = this.lastActivity + IDLE_MS;
      d = d === null ? i : Math.min(d, i);
    }
    return d;
  }

  // Idle = no request in progress, nothing queued or being answered, no half-received frame
  // (that case belongs to the 10 s rule).
  isIdle() {
    return this.partialSince === null && !this.cur && !this.serving && this.queue.length === 0;
  }

  schedule() {
    const d = this.closing || this.destroyed ? null : this.deadline();
    if (d === null) {
      clearTimeout(this.timer);
      this.timer = null;
      return;
    }
    if (this.timer && this.timerAt === d) return;
    clearTimeout(this.timer);
    this.timerAt = d;
    this.timer = setTimeout(() => this.onTimer(), Math.max(0, d - Date.now()));
  }

  onTimer() {
    this.timer = null;
    if (this.closing || this.destroyed) return;
    const now = Date.now();
    const starts = [this.partialSince, this.msgSince].filter((x) => x !== null);
    if (starts.length && Math.min(...starts) + PROGRESS_MS <= now) {
      let what;
      if (this.msgSince !== null && (this.partialSince === null || this.msgSince <= this.partialSince)) {
        what = `request on stream ${this.cur.stream} not finished within ${PROGRESS_MS / 1000} s`;
      } else {
        what = `${this.gotPreface ? 'frame' : 'preface'} not finished within ${PROGRESS_MS / 1000} s`;
      }
      return this.connError(E_TIMEOUT, what);
    }
    if (this.isIdle() && this.lastActivity + IDLE_MS <= now) {
      log(`conn#${this.id} ${this.peer} idle timeout, sending GOAWAY(NO_ERROR)`);
      return this.sendGoawayAndClose(E_NO_ERROR, `idle for ${IDLE_MS / 1000} s`);
    }
    this.schedule();
  }

  // ---- output

  write(buf) {
    if (this.destroyed) return Promise.resolve();
    if (this.sock.write(buf)) return Promise.resolve();
    return new Promise((resolve) => {
      const done = () => {
        this.sock.off('drain', done);
        this.sock.off('close', done);
        resolve();
      };
      this.sock.on('drain', done);
      this.sock.on('close', done);
    });
  }

  async pump() {
    if (this.serving) return;
    this.serving = true;
    this.schedule();
    while (this.queue.length && !this.destroyed) {
      const req = this.queue.shift();
      try {
        await this.respond(req);
        this.lastProcessed = req.stream;
      } catch (e) {
        if (this.destroyed) {
          log(`conn#${this.id} ${this.peer} stream=${req.stream} connection closed while the response was being sent`);
          this.queue.length = 0;
          break;
        }
        log(`conn#${this.id} ${this.peer} internal error on stream ${req.stream}: ${e.stack ?? e}`);
        this.closing = true;
        this.queue.length = 0;
        this.cur = null;
        this.partialSince = this.msgSince = null;
        this.closeWith = { code: E_INTERNAL_ERROR, reason: 'internal error while sending a response', goaway: true };
      }
    }
    this.serving = false;
    this.lastActivity = Date.now(); // the idle clock starts after the last response, too
    this.afterQueue();
  }

  afterQueue() {
    if (this.destroyed || this.ended) return;
    if (this.closeWith) {
      if (this.closeWith.goaway) return this.sendGoawayAndClose(this.closeWith.code, this.closeWith.reason);
      return this.closeQuietly();
    }
    this.schedule();
  }

  async respond(req) {
    // req.method is set only when exactly one :method was decoded, so a malformed HEAD request
    // whose :method could still be read gets a body-less 400, as any HEAD response must be.
    const isHead = req.method === 'HEAD';
    let status;
    let reason = '';
    let file = null;
    const extra = [];
    if (req.error) {
      status = 400;
      reason = req.error;
    } else if (req.method !== 'GET' && req.method !== 'HEAD') {
      status = 405;
      reason = `method ${JSON.stringify(req.method)} is not allowed; use GET or HEAD`;
      extra.push(['allow', 'GET, HEAD']);
    } else {
      try {
        file = await openForPath(req.path);
        status = 200;
      } catch (e) {
        if (!(e instanceof HttpError)) throw e;
        status = e.status;
        reason = e.message;
      }
    }

    let sent = 0;
    try {
      if (grease) await this.write(greaseFrame(req.stream));
      if (file) {
        const ext = path.extname(file.realPath).toLowerCase();
        const fields = [
          [':status', '200'],
          ['content-type', MIME[ext] ?? 'application/octet-stream'],
          ['content-length', String(file.size)],
          ['date', new Date().toUTCString()],
          ['server', SERVER_NAME],
        ];
        const bodyless = isHead || file.size === 0;
        await this.write(encodeFrame(T_HEADERS, bodyless ? F_END_STREAM : 0, req.stream, encodeHeaderBlock(fields)));
        if (!bodyless) {
          let pos = 0;
          while (pos < file.size) {
            const n = Math.min(MAX_DATA_CHUNK, file.size - pos);
            const frame = Buffer.allocUnsafe(8 + n);
            let got = 0;
            while (got < n) {
              const { bytesRead } = await file.fh.read(frame, 8 + got, n - got, pos + got);
              if (bytesRead === 0) throw new Error(`file ${file.realPath} shrank while being sent`);
              got += bytesRead;
            }
            pos += n;
            frame.writeUInt16BE(n, 0);
            frame[2] = T_DATA;
            frame[3] = pos === file.size ? F_END_STREAM : 0;
            frame.writeUInt32BE(req.stream, 4);
            await this.write(frame);
            if (this.destroyed) throw new Error('connection closed while sending body');
          }
          sent = file.size;
        }
      } else {
        const body = Buffer.from(`${status} ${REASONS[status]}: ${reason}\n`, 'utf8');
        const fields = [
          [':status', String(status)],
          ['content-type', 'text/plain; charset=utf-8'],
          ['content-length', String(body.length)],
          ['date', new Date().toUTCString()],
          ['server', SERVER_NAME],
          ...extra,
        ];
        await this.write(encodeFrame(T_HEADERS, isHead ? F_END_STREAM : 0, req.stream, encodeHeaderBlock(fields)));
        if (!isHead) {
          await this.write(encodeFrame(T_DATA, F_END_STREAM, req.stream, body));
          sent = body.length;
        }
      }
    } finally {
      if (file) await file.fh.close().catch(() => {});
    }
    const p = req.path ? printable(req.path) : '-';
    log(`conn#${this.id} ${this.peer} stream=${req.stream} ${req.method ? printable(Buffer.from(req.method, 'latin1')) : '-'} ${p} -> ${status} ${sent}B ${Date.now() - req.t0}ms${reason ? ` (${reason})` : ''}`);
  }
}

// ---------------------------------------------------------------- main

const server = net.createServer({ allowHalfOpen: true }, (sock) => new Connection(sock));
server.on('error', (e) => {
  process.stderr.write(`bserve: ${e.message}\n`);
  process.exit(1);
});
server.listen(port, () => {
  log(`bserve listening on port ${port}, docroot ${ROOT}${grease ? ', grease on' : ''}`);
});
for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, () => {
    log(`${sig}, exiting`);
    process.exit(0);
  });
}
