#!/usr/bin/env node
// bcurl.mjs - BHTTP/1 command-line client, written from SPEC.md only.
// usage: node bcurl.mjs [-v] [-I] [-X METHOD] [-H "name: value"]... [--grease] URL [URL ...]
//
// Exit codes: 0 all statuses < 400 | 4 worst was 4xx | 5 worst was 5xx (or above)
//             1 usage or connection error | 2 protocol error

import net from 'node:net';
import process from 'node:process';
import crypto from 'node:crypto';
import {
  PREFACE, FrameReader, T_DATA, T_HEADERS, T_GOAWAY, F_END_STREAM,
  E_NO_ERROR, E_PROTOCOL_ERROR, E_TIMEOUT, errorName, encodeFrame, encodeGoaway, decodeGoaway,
  encodeHeaderBlock, decodeHeaderBlock, validateResponseFields, parseContentLength,
  isValidLiteralName, frameTypeName, flagsText, hexdump, printable,
} from './bhttp.mjs';

const DEFAULT_PORT = 9000;
const CONNECT_TIMEOUT_MS = 10_000;
const RESPONSE_TIMEOUT_MS = 30_000; // no octets received for this long while awaiting a response
const USER_AGENT = 'bcurl.mjs/1';

const USAGE = 'usage: node bcurl.mjs [-v] [-I] [-X METHOD] [-H "name: value"]... [--grease] URL [URL ...]\n'
  + '  URL = [bhttp://]host[:port][/path]   (default port 9000, default path /)\n';

function usageExit(msg) {
  if (msg) process.stderr.write(`bcurl: ${msg}\n`);
  process.stderr.write(USAGE);
  process.exit(1);
}

// ------------------------------------------------------------------ arguments

let verbose = false;
let headOnly = false;
let method = null;
let grease = false;
const userHeaders = [];
const urls = [];
const argv = process.argv.slice(2);
for (let i = 0; i < argv.length; i++) {
  const a = argv[i];
  if (a === '--') { urls.push(...argv.slice(i + 1)); break; }
  if (a === '--grease') grease = true;
  else if (a === '-h' || a === '--help') usageExit();
  else if (a === '-X' || a === '-H') {
    const v = argv[++i];
    if (v === undefined) usageExit(`${a} needs an argument`);
    if (a === '-X') method = v; else userHeaders.push(v);
  } else if (a.startsWith('-X') && a.length > 2) method = a.slice(2);
  else if (a.startsWith('-H') && a.length > 2) userHeaders.push(a.slice(2));
  else if (/^-[vI]+$/.test(a)) {
    if (a.includes('v')) verbose = true;
    if (a.includes('I')) headOnly = true;
  } else if (a.startsWith('-')) usageExit(`unknown option ${a}`);
  else urls.push(a);
}
if (urls.length === 0) usageExit('no URL given');
if (method === null) method = headOnly ? 'HEAD' : 'GET';
if (method.length === 0 || /[\x00\r\n]/.test(method)) usageExit('bad method');

function parseUrl(u) {
  let s = u;
  const m = /^([A-Za-z][A-Za-z0-9+.-]*):\/\//.exec(s);
  if (m) {
    if (m[1].toLowerCase() !== 'bhttp') usageExit(`unsupported scheme in ${u} (only bhttp://)`);
    s = s.slice(m[0].length);
  }
  let cut = s.search(/[/?#]/);
  if (cut < 0) cut = s.length;
  const authority = s.slice(0, cut);
  let p = s.slice(cut);
  if (p === '') p = '/';
  else if (p[0] !== '/') p = '/' + p;
  let host;
  let portStr = null;
  if (authority.startsWith('[')) {
    const j = authority.indexOf(']');
    if (j < 0) usageExit(`bad IPv6 literal in ${u}`);
    host = authority.slice(1, j);
    const rest = authority.slice(j + 1);
    if (rest) {
      if (!rest.startsWith(':')) usageExit(`bad authority in ${u}`);
      portStr = rest.slice(1);
    }
  } else {
    const j = authority.lastIndexOf(':');
    if (j >= 0) { host = authority.slice(0, j); portStr = authority.slice(j + 1); } else host = authority;
  }
  if (!host) usageExit(`no host in ${u}`);
  let port = DEFAULT_PORT;
  if (portStr !== null) {
    if (!/^[0-9]{1,5}$/.test(portStr) || +portStr < 1 || +portStr > 65535) usageExit(`bad port in ${u}`);
    port = +portStr;
  }
  if (/[\x00\r\n]/.test(p)) usageExit(`path in ${u} contains NUL/CR/LF`);
  return { url: u, host, port, authority, path: p, key: `${host.toLowerCase()}:${port}` };
}

const targets = urls.map(parseUrl);
for (const t of targets) {
  if (t.key !== targets[0].key) {
    usageExit(`all URLs must use the same host:port (one connection): ${targets[0].key} vs ${t.key}`);
  }
}

const baseHeaders = [['user-agent', USER_AGENT], ['accept', '*/*']];
const overridden = new Set(); // default headers replaced by a -H of the same name
for (const h of userHeaders) {
  const j = h.indexOf(':');
  if (j <= 0) usageExit(`bad header ${JSON.stringify(h)} (want "name: value")`);
  const name = h.slice(0, j).trim().toLowerCase();
  const value = h.slice(j + 1).trim();
  if (!isValidLiteralName(name)) usageExit(`bad header name ${JSON.stringify(name)}`);
  if (/[\x00\r\n]/.test(value)) usageExit(`header ${name} value contains NUL/CR/LF`);
  const existing = baseHeaders.findIndex(([n]) => n === name && (n === 'user-agent' || n === 'accept'));
  if (existing >= 0 && !overridden.has(name)) {
    baseHeaders[existing][1] = value;
    overridden.add(name);
  } else baseHeaders.push([name, value]);
}

// ------------------------------------------------------------------ verbose output

function vlog(s) { if (verbose) process.stderr.write(s + '\n'); }

function dumpItem(dir, item, offset) {
  if (!verbose) return;
  const out = [];
  if (item.kind === 'preface') {
    const ok = item.raw.equals(PREFACE);
    out.push(`${dir} PREFACE "${printable(item.raw)}" length=8${ok ? '' : '  <-- MISMATCH'}`);
    out.push(hexdump(item.raw, offset));
  } else {
    const { type, flags, stream, payload, raw } = item;
    out.push(`${dir} ${frameTypeName(type)} ${flagsText(type, flags)} stream=${stream} length=${payload.length}`);
    out.push(hexdump(raw, offset));
    if (type === T_HEADERS) {
      const { fields, error } = decodeHeaderBlock(payload);
      for (const f of fields) out.push(`      ${f.name}: ${printable(f.value)}${f.index === 0 ? '   (literal name)' : ''}`);
      if (error) out.push(`      !! malformed header block: ${error}`);
    } else if (type === T_GOAWAY) {
      const g = decodeGoaway(payload);
      if (g) out.push(`      last_stream_id=${g.lastStreamId} error_code=${g.errorCode}(${errorName(g.errorCode)}) debug="${printable(Buffer.from(g.debug))}"`);
      else out.push('      !! GOAWAY payload shorter than 8 octets');
    }
  }
  process.stderr.write(out.join('\n') + '\n');
}

// ------------------------------------------------------------------ connection

const { host, port } = targets[0];
const reader = new FrameReader({ expectPreface: true });
let txOff = 0;
let rxOff = 0;
let connected = false;
let prefaceOk = false;
let finished = false; // no more requests will be sent / responses validated
let allDone = false;  // every response was received successfully
let nextIdx = 0;
let nextStream = 1;
let cur = null;
let worst = 0;
let respTimer = null;

process.stdout.on('error', () => {}); // e.g. EPIPE when piped into head

function send(buf, kind = 'frame') {
  const item = kind === 'preface'
    ? { kind, raw: buf }
    : { kind, type: buf[2], flags: buf[3], stream: buf.readUInt32BE(4), payload: buf.subarray(8), raw: buf };
  dumpItem('>', item, txOff);
  txOff += buf.length;
  sock.write(buf);
}

function armResponseTimer() {
  clearTimeout(respTimer);
  respTimer = null;
  if (finished || !cur) return;
  respTimer = setTimeout(() => fail(1, `no data from server for ${RESPONSE_TIMEOUT_MS / 1000} s`, { goaway: E_TIMEOUT }), RESPONSE_TIMEOUT_MS);
}

// §3: a peer MUST send GOAWAY before closing because of an error or a timeout, and a client's
// last_stream_id is always 0. `goaway` is the error code to send, or null when the socket is
// unusable (connect failure, reset).
function fail(code, msg, { goaway = E_PROTOCOL_ERROR } = {}) {
  if (finished) return;
  finished = true;
  clearTimeout(respTimer);
  clearTimeout(connectTimer);
  process.stderr.write(`bcurl: ${msg}\n`);
  process.exitCode = code;
  if (connected && goaway !== null && !sock.destroyed && sock.writable) {
    try { send(encodeGoaway(0, goaway, msg)); } catch { /* ignore */ }
  }
  if (connected) {
    sock.end();
    setTimeout(() => sock.destroy(), 1000).unref();
  } else sock.destroy();
}
const protoFail = (msg) => fail(2, `protocol error: ${msg}`);

function finishAll() {
  finished = true;
  allDone = true;
  clearTimeout(respTimer);
  process.exitCode = worst >= 500 ? 5 : worst >= 400 ? 4 : 0;
  send(encodeGoaway(0, E_NO_ERROR, 'client done')); // optional for a client that is simply done (§3)
  sock.end();
  setTimeout(() => sock.destroy(), 1000).unref();
}

function sendNext() {
  if (nextIdx >= targets.length) return finishAll();
  const t = targets[nextIdx++];
  const stream = nextStream++;
  cur = { stream, target: t, method, gotHeaders: false, status: null, contentLength: null, dataTotal: 0 };
  const fields = [[':method', method], [':path', t.path], [':authority', t.authority], ...baseHeaders];
  let block;
  try { block = encodeHeaderBlock(fields); } catch (e) { return fail(1, e.message); }
  if (grease) {
    const type = 0xf0 + crypto.randomInt(16);
    // §2: an unknown frame may carry any flags and any stream ID.
    send(encodeFrame(type, crypto.randomInt(256), stream, crypto.randomBytes(1 + crypto.randomInt(8))));
  }
  send(encodeFrame(T_HEADERS, F_END_STREAM, stream, block));
  armResponseTimer();
}

function handleFrame(f) {
  const { type, flags, stream, payload } = f;
  if (type === T_GOAWAY) {
    if (stream !== 0) return protoFail(`GOAWAY on stream ${stream}`);
    const g = decodeGoaway(payload);
    if (!g) return protoFail(`GOAWAY payload is ${payload.length} octets (< 8)`);
    if (finished) {
      if (allDone && g.errorCode !== E_NO_ERROR) process.stderr.write(`bcurl: warning: server sent GOAWAY ${errorName(g.errorCode)} "${g.debug}" after all responses\n`);
      return;
    }
    if (g.errorCode !== E_NO_ERROR) {
      // An error GOAWAY is a client connection error (§6); our own GOAWAY before closing says
      // NO_ERROR because the fault is the server's, not ours (spec does not say which code).
      return fail(2, `server sent GOAWAY ${errorName(g.errorCode)} (last_stream_id=${g.lastStreamId}): ${g.debug}`, { goaway: E_NO_ERROR });
    }
    if (cur && cur.stream > g.lastStreamId) {
      return fail(2, `server sent GOAWAY(NO_ERROR, last_stream_id=${g.lastStreamId}: ${g.debug}); request on stream ${cur.stream} was not processed`, { goaway: E_NO_ERROR });
    }
    vlog(`* server sent GOAWAY(NO_ERROR) last_stream_id=${g.lastStreamId}; will read remaining response`);
    return;
  }
  if (type !== T_DATA && type !== T_HEADERS) return; // unknown frame type: skip (§2)
  if (finished) return;
  const name = frameTypeName(type);
  if (stream === 0) return protoFail(`${name} on stream 0`);
  if (!cur || stream !== cur.stream) return protoFail(`${name} on stream ${stream}, expected ${cur ? `stream ${cur.stream}` : 'no frame'}`);
  const end = (flags & F_END_STREAM) !== 0;
  if (type === T_HEADERS) {
    if (cur.gotHeaders) return protoFail(`second HEADERS frame on stream ${stream}`);
    cur.gotHeaders = true;
    const { fields, error } = decodeHeaderBlock(payload);
    const err = error ?? validateResponseFields(fields);
    if (err) return protoFail(`malformed response header block on stream ${stream}: ${err}`);
    const cl = parseContentLength(fields);
    if (cl.error) return protoFail(`bad content-length on stream ${stream}: ${cl.error}`);
    cur.status = Number(fields.find((x) => x.name === ':status').value.toString('latin1'));
    cur.contentLength = cl.value;
    if (cur.method === 'HEAD' && !end) return protoFail(`response to HEAD on stream ${stream} lacks END_STREAM on HEADERS`);
    if (headOnly) {
      const lines = [];
      for (const x of fields) lines.push(Buffer.from(`${x.name}: `, 'latin1'), x.value, Buffer.from('\n'));
      lines.push(Buffer.from('\n'));
      process.stdout.write(Buffer.concat(lines));
    }
  } else {
    if (!cur.gotHeaders) return protoFail(`DATA before HEADERS on stream ${stream}`);
    if (cur.method === 'HEAD') return protoFail(`DATA in a response to HEAD on stream ${stream}`);
    cur.dataTotal += payload.length;
    if (cur.contentLength !== null && cur.dataTotal > cur.contentLength) {
      return protoFail(`DATA on stream ${stream} exceeds content-length ${cur.contentLength}`);
    }
    if (!headOnly && payload.length) process.stdout.write(payload);
  }
  if (end) {
    if (cur.method !== 'HEAD' && cur.contentLength !== null && cur.dataTotal !== cur.contentLength) {
      return protoFail(`content-length ${cur.contentLength} but DATA totals ${cur.dataTotal} on stream ${stream}`);
    }
    vlog(`* stream ${stream} ${cur.method} ${cur.target.path}: status ${cur.status}, ${cur.dataTotal} body octets`);
    worst = Math.max(worst, cur.status);
    cur = null;
    sendNext();
  }
}

function onData(chunk) {
  reader.push(chunk);
  let item;
  while ((item = reader.next())) {
    const off = rxOff;
    rxOff += item.raw.length;
    dumpItem('<', item, off);
    if (item.kind === 'preface') {
      if (!item.ok) {
        fail(2, `bad preface from server: "${printable(item.raw)}"`);
        continue;
      }
      prefaceOk = true;
      continue;
    }
    handleFrame(item);
  }
  if (!finished) armResponseTimer();
}

function onEnd() {
  if (finished) return;
  if (!prefaceOk) return fail(2, 'unexpected EOF before the server preface');
  if (reader.buffered > 0) return fail(2, `unexpected EOF inside a frame (${reader.buffered} octets buffered)`);
  return fail(2, `unexpected EOF while waiting for the response on stream ${cur ? cur.stream : '?'}`);
}

// allowHalfOpen: after the server's FIN we can still send our GOAWAY before closing.
const sock = net.connect({ host, port, allowHalfOpen: true });
const connectTimer = setTimeout(() => fail(1, `connect to ${host}:${port} timed out`), CONNECT_TIMEOUT_MS);
sock.setNoDelay(true);
sock.on('connect', () => {
  clearTimeout(connectTimer);
  connected = true;
  vlog(`* connected to ${sock.remoteAddress}:${sock.remotePort} (from ${sock.localAddress}:${sock.localPort})`);
  send(PREFACE, 'preface');
  sendNext();
});
sock.on('data', onData);
sock.on('end', onEnd);
sock.on('error', (e) => {
  if (finished) return;
  fail(1, connected ? `connection error: ${e.code ?? e.message}` : `cannot connect to ${host}:${port}: ${e.code ?? e.message}`, { goaway: null });
});
