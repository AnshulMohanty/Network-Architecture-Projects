#!/usr/bin/env node
// selftest.mjs - exercises bserve.mjs and bcurl.mjs against each other, against raw
// hand-crafted frames, and bcurl against a scripted fake server.
// usage: node selftest.mjs [--quick]     (--quick skips the 10 s / 60 s timeout tests)
// Uses ports 9111-9119. Every server it starts is killed before it exits.

import net from 'node:net';
import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { spawn, execFile } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import {
  PREFACE, FrameReader, T_DATA, T_HEADERS, T_GOAWAY, F_END_STREAM,
  encodeFrame, encodeGoaway, decodeGoaway, encodeHeaderBlock, decodeHeaderBlock,
} from './bhttp.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(HERE, 'testroot');
const P_MAIN = 9111, P_GREASE = 9112, P_FAKE = 9113, P_REFUSED = 9119;
const QUICK = process.argv.includes('--quick');

let passed = 0;
const failures = [];
function check(name, ok, detail = '') {
  if (ok) { passed++; console.log(`  ok   ${name}`); } else { failures.push(name); console.log(`  FAIL ${name}${detail ? ` -- ${detail}` : ''}`); }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const file = (p) => fs.readFileSync(path.join(ROOT, p));

// ------------------------------------------------------------ processes

const children = [];
function startServer(port, extra = []) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, ['bserve.mjs', 'testroot', String(port), ...extra], { cwd: HERE, stdio: ['ignore', 'ignore', 'pipe'] });
    children.push(child);
    child.log = '';
    child.stderr.setEncoding('utf8');
    child.stderr.on('data', (d) => {
      child.log += d;
      if (child.log.includes('listening')) resolve(child);
    });
    child.on('exit', (c) => reject(new Error(`server on ${port} exited with ${c}: ${child.log}`)));
  });
}
function killAll() { for (const c of children) { try { c.kill(); } catch { /* */ } } }
process.on('exit', killAll);

function bcurl(args) {
  return new Promise((resolve) => {
    execFile(process.execPath, ['bcurl.mjs', ...args], { cwd: HERE, encoding: 'buffer', maxBuffer: 64 << 20, timeout: 60_000 },
      (err, stdout, stderr) => resolve({ code: err ? (err.code ?? -1) : 0, stdout, stderr: stderr.toString('utf8') }));
  });
}

// ------------------------------------------------------------ raw socket client

function field(nameOrIndex, value) {
  const v = Buffer.from(value, 'latin1');
  if (typeof nameOrIndex === 'number') {
    const h = Buffer.alloc(3); h[0] = nameOrIndex; h.writeUInt16BE(v.length, 1);
    return Buffer.concat([h, v]);
  }
  const n = Buffer.from(nameOrIndex, 'latin1');
  const h = Buffer.alloc(3); h[0] = 0; h.writeUInt16BE(n.length, 1);
  const vl = Buffer.alloc(2); vl.writeUInt16BE(v.length, 0);
  return Buffer.concat([h, n, vl, v]);
}
const GOOD = (p = '/', m = 'GET') => [[':method', m], [':path', p], [':authority', 'test'], ['user-agent', 'selftest']];
const headers = (stream, fields, flags = F_END_STREAM) => encodeFrame(T_HEADERS, flags, stream, encodeHeaderBlock(fields));
const rawHeaders = (stream, block, flags = F_END_STREAM) => encodeFrame(T_HEADERS, flags, stream, block);

function raw(port, { preface = true } = {}) {
  const sock = net.connect({ host: '127.0.0.1', port });
  const reader = new FrameReader({ expectPreface: true });
  const c = { sock, frames: [], preface: null, ended: false, endAt: 0, t0: Date.now(), waiters: [] };
  const poke = () => { for (const w of [...c.waiters]) w(); };
  sock.on('data', (d) => {
    reader.push(d);
    let it;
    while ((it = reader.next())) {
      if (it.kind === 'preface') c.preface = Buffer.from(it.raw);
      else c.frames.push({ ...it, payload: Buffer.from(it.payload), at: Date.now() });
    }
    poke();
  });
  sock.on('end', () => { c.ended = true; c.endAt = Date.now(); poke(); });
  sock.on('error', () => { c.ended = true; c.endAt = c.endAt || Date.now(); poke(); });
  sock.on('close', () => { c.ended = true; c.endAt = c.endAt || Date.now(); poke(); });
  c.send = (...bufs) => sock.write(Buffer.concat(bufs));
  if (preface) c.send(PREFACE);
  c.waitFor = (pred, ms = 5000) => new Promise((resolve) => {
    const done = (v) => { clearTimeout(t); c.waiters = c.waiters.filter((w) => w !== test); resolve(v); };
    const test = () => { const v = pred(c); if (v) done(v); else if (c.ended) done(pred(c) || null); };
    const t = setTimeout(() => done(null), ms);
    c.waiters.push(test);
    test();
  });
  c.response = (stream, ms) => c.waitFor(() => {
    const fs_ = c.frames.filter((f) => f.stream === stream && (f.type === T_HEADERS || f.type === T_DATA));
    if (!fs_.length || !(fs_[fs_.length - 1].flags & F_END_STREAM)) return null;
    const h = decodeHeaderBlock(fs_[0].payload).fields;
    const get = (n) => h.find((x) => x.name === n)?.value.toString('latin1');
    return { status: get(':status'), get, fields: h, body: Buffer.concat(fs_.slice(1).map((f) => f.payload)), frames: fs_ };
  }, ms);
  c.goaway = (ms) => c.waitFor(() => {
    const g = c.frames.find((f) => f.type === T_GOAWAY);
    return g ? { ...decodeGoaway(g.payload), at: g.at } : null;
  }, ms);
  c.close = () => sock.destroy();
  return c;
}

// ------------------------------------------------------------ fake server for client tests

let fakeScript = null; // (sock, stream, requestFields) => void
let fakeRx = [];       // frames the client sent on the current fake connection (after the preface)
function startFake() {
  return new Promise((resolve) => {
    const srv = net.createServer((sock) => {
      const reader = new FrameReader({ expectPreface: true });
      let fired = false;
      sock.on('data', (d) => {
        reader.push(d);
        let it;
        while ((it = reader.next())) {
          if (it.kind === 'frame') fakeRx.push({ type: it.type, stream: it.stream, payload: Buffer.from(it.payload) });
          if (it.kind === 'frame' && it.type === T_HEADERS && !fired) {
            fired = true;
            const fields = decodeHeaderBlock(it.payload).fields;
            fakeScript(sock, it.stream, fields);
          }
        }
      });
      sock.on('error', () => {});
    });
    srv.listen(P_FAKE, '127.0.0.1', () => resolve(srv));
  });
}
const respHeaders = (stream, status, extra = [], flags = 0) => headers(stream, [[':status', String(status)], ...extra], flags);

// ------------------------------------------------------------ tests

async function timeoutTests() {
  if (QUICK) return [];
  const results = [];
  // partial frame header -> GOAWAY(TIMEOUT) ~10 s
  const a = raw(P_MAIN);
  a.send(Buffer.from([0x00, 0x10, 0x01]));
  // request message started (HEADERS without END_STREAM), DATA at 5 s must not extend it
  const b = raw(P_MAIN);
  b.send(headers(1, GOOD('/'), 0));
  setTimeout(() => b.send(encodeFrame(T_DATA, 0, 1, Buffer.from('abc'))), 5000);
  // partial preface -> TIMEOUT
  const c = raw(P_MAIN, { preface: false });
  c.send(PREFACE.subarray(0, 4));
  // rev 2 §5: idle = "no frame has been received for 60 s", so a grease frame 10 s after the
  // response restarts the clock -> GOAWAY(NO_ERROR) ~70 s after the response.
  const d = raw(P_MAIN);
  d.send(headers(1, GOOD('/')));
  const dr = await d.response(1);
  const idleStart = Date.now();
  setTimeout(() => d.send(encodeFrame(0xfe, 0xff, 9, Buffer.from('x'))), 10_000);
  // waiting for the preface counts as idle -> GOAWAY(NO_ERROR, 0) after ~60 s of silence
  const e = raw(P_MAIN, { preface: false });

  const ga = await a.goaway(15_000);
  const gb = await b.goaway(15_000);
  const gc = await c.goaway(15_000);
  const inRange = (g, t0, lo, hi) => g && g.at - t0 >= lo && g.at - t0 <= hi;
  results.push(['partial frame header -> GOAWAY(TIMEOUT) after ~10 s', ga && ga.errorCode === 3 && inRange(ga, a.t0, 9500, 12000), JSON.stringify(ga && { ...ga, dt: ga.at - a.t0 })]);
  results.push(['unfinished request message (DATA mid-way) -> GOAWAY(TIMEOUT) ~10 s after HEADERS', gb && gb.errorCode === 3 && inRange(gb, b.t0, 9500, 12000) && gb.lastStreamId === 0, JSON.stringify(gb && { ...gb, dt: gb.at - b.t0 })]);
  results.push(['partial preface -> GOAWAY(TIMEOUT) ~10 s', gc && gc.errorCode === 3 && inRange(gc, c.t0, 9500, 12000), JSON.stringify(gc && { ...gc, dt: gc.at - c.t0 })]);
  const ge = await e.goaway(70_000);
  results.push(['no preface at all: idle 60 s -> GOAWAY(NO_ERROR, last=0)', ge && ge.errorCode === 0 && ge.lastStreamId === 0 && inRange(ge, e.t0, 59_000, 62_000), JSON.stringify(ge && { ...ge, dt: ge.at - e.t0 })]);
  const gd = await d.goaway(85_000);
  results.push(['idle: grease at +10 s restarts the 60 s clock -> GOAWAY(NO_ERROR, last=1) at ~70 s', dr && gd && gd.errorCode === 0 && gd.lastStreamId === 1 && inRange(gd, idleStart, 69_000, 72_500), JSON.stringify(gd && { ...gd, dt: gd.at - idleStart })]);
  await d.waitFor((x) => x.ended, 5000);
  results.push(['server closes after idle GOAWAY', d.ended]);
  for (const x of [a, b, c, d, e]) x.close();
  return results;
}

async function main() {
  const big = file('big.bin');
  const index = file('index.html');
  const subIndex = file('sub/index.html');
  const srv = await startServer(P_MAIN);
  const gsrv = await startServer(P_GREASE, ['--grease']);
  const fake = await startFake();
  const timeoutsP = timeoutTests(); // runs in the background (~72 s)

  console.log('bcurl <-> bserve');
  let r = await bcurl([`localhost:${P_MAIN}/`]);
  check('GET / -> index.html, exit 0', r.code === 0 && r.stdout.equals(index), `code=${r.code}`);
  r = await bcurl(['-v', `bhttp://localhost:${P_MAIN}/big.bin`]);
  const dataFrames = (r.stderr.match(/^< DATA /gm) || []).length;
  check('GET /big.bin (200003 B) byte-exact over 13 DATA frames', r.code === 0 && r.stdout.equals(big) && dataFrames === 13, `code=${r.code} len=${r.stdout.length} frames=${dataFrames}`);
  check('-v shows preface both ways, hexdump and decoded headers', /^> PREFACE/m.test(r.stderr) && /^< PREFACE/m.test(r.stderr) && /00000000  42 48 54 54 50 2f 31 0a/.test(r.stderr) && /content-length: 200003/.test(r.stderr));
  r = await bcurl(['-v', `localhost:${P_MAIN}/empty.txt`]);
  check('GET /empty.txt -> 200, empty body, single HEADERS+END_STREAM', r.code === 0 && r.stdout.length === 0 && /< HEADERS flags=0x01\(END_STREAM\)/.test(r.stderr) && /content-length: 0/.test(r.stderr) && !/< DATA/.test(r.stderr), r.stderr.slice(0, 300));
  r = await bcurl([`localhost:${P_MAIN}/sub`, `localhost:${P_MAIN}/sub/`]);
  check('/sub and /sub/ -> sub/index.html', r.code === 0 && r.stdout.equals(Buffer.concat([subIndex, subIndex])));
  r = await bcurl([`localhost:${P_MAIN}/hello%20world.txt`, `localhost:${P_MAIN}/index.html?x=1&y=%zz#frag`]);
  check('percent-decoding and ?query/#fragment stripping', r.code === 0 && r.stdout.equals(Buffer.concat([file('hello world.txt'), index])), `code=${r.code} ${r.stderr}`);
  r = await bcurl(['-I', `localhost:${P_MAIN}/big.bin`]);
  const hs = r.stdout.toString();
  check('-I (HEAD) prints headers only, content-length of full file', r.code === 0 && /^:status: 200$/m.test(hs) && /^content-length: 200003$/m.test(hs) && /^date: \w{3}, \d\d \w{3} \d{4} \d\d:\d\d:\d\d GMT$/m.test(hs) && r.stdout.length < 400, hs);
  r = await bcurl(['-v', '-I', `localhost:${P_MAIN}/nope.html`]);
  check('HEAD 404 has no DATA', r.code === 4 && /^:status: 404$/m.test(r.stdout.toString()) && !/< DATA/.test(r.stderr));
  r = await bcurl([`localhost:${P_MAIN}/nope.html`]);
  check('404 -> exit 4, text/plain reason', r.code === 4 && /^404 /.test(r.stdout.toString()));
  r = await bcurl(['-X', 'POST', `localhost:${P_MAIN}/`]);
  check('POST -> 405, exit 4', r.code === 4 && /^405 /.test(r.stdout.toString()));
  r = await bcurl(['-I', '-X', 'DELETE', `localhost:${P_MAIN}/`]);
  check('405 carries literal allow: GET, HEAD', r.code === 4 && /^allow: GET, HEAD$/m.test(r.stdout.toString()) && /^content-type: text\/plain; charset=utf-8$/m.test(r.stdout.toString()), r.stdout.toString());
  const bad = ['/../outside-docroot.txt', '/%2e%2e/outside-docroot.txt', '/%2E%2e/outside-docroot.txt', '/sub/../index.html', '/./index.html', '/sub/.',
    '/..%2foutside-docroot.txt', '/sub%2f..%2f..%2foutside-docroot.txt', '/sub%5c..%5c..%5coutside-docroot.txt', '/a\\b', '/%00', '/%zz', '/abc%4', '/abc%',
    '/%ff', '/%c3', '/caf%e9.txt' /* rev 2 §5 step 3: not UTF-8 after decoding -> 400 */];
  for (const p of bad) {
    r = await bcurl([`localhost:${P_MAIN}${p}`]);
    check(`bad path ${p} -> 400`, r.code === 4 && /^400 /.test(r.stdout.toString()), `code=${r.code} ${r.stdout}`);
  }
  for (const p of ['/nope', '/sub/nope/', '/sub/page.txt/x', '/caf%c3%a9.txt', '/C:/Windows/win.ini']) {
    r = await bcurl([`localhost:${P_MAIN}${p}`]);
    check(`missing ${p} -> 404`, r.code === 4 && /^404 /.test(r.stdout.toString()), `code=${r.code} ${r.stdout}`);
  }
  // rev 2 §5 step 4: empty segments collapse, so a trailing "/" after a file name is ignored
  r = await bcurl([`localhost:${P_MAIN}/index.html/`, `localhost:${P_MAIN}/sub//page.txt`, `localhost:${P_MAIN}//sub///page.txt//`]);
  check('empty segments collapse: /index.html/, /sub//page.txt, //sub///page.txt// -> 200', r.code === 0 && r.stdout.equals(Buffer.concat([index, file('sub/page.txt'), file('sub/page.txt')])), `code=${r.code}`);
  r = await bcurl(['-X', 'get', `localhost:${P_MAIN}/`]);
  check('lowercase method "get" is a token but not GET -> 405', r.code === 4 && /^405 /.test(r.stdout.toString()));
  r = await bcurl(['-X', 'POST', `localhost:${P_MAIN}/../x`]);
  check('check order: POST /../x -> 405 (method before path)', r.code === 4 && /^405 /.test(r.stdout.toString()));
  const before = (srv.log.match(/stream=/g) || []).length;
  r = await bcurl(['-v', `localhost:${P_MAIN}/`, `localhost:${P_MAIN}/sub/page.txt`, `localhost:${P_MAIN}/nope`, `localhost:${P_MAIN}/big.bin`, `bhttp://localhost:${P_MAIN}/empty.txt`]);
  await sleep(200);
  const newLines = srv.log.split('\n').filter((l) => /stream=/.test(l)).slice(before);
  const peers = new Set(newLines.map((l) => l.split(' ')[2]));
  check('5 URLs over one connection: one preface, streams 1..5, one peer in server log, worst=404 -> exit 4',
    r.code === 4 && (r.stderr.match(/^> PREFACE/gm) || []).length === 1 && /stream=5 length/.test(r.stderr) && newLines.length === 5 && peers.size === 1
    && r.stdout.equals(Buffer.concat([index, file('sub/page.txt'), Buffer.from('404 Not Found: not found\n'), big])), `code=${r.code} lines=${newLines.length} peers=${[...peers]}`);
  check('client ends with GOAWAY(NO_ERROR, last_stream_id=0) (rev 2: a client always sends 0)', /^> GOAWAY [^\n]*\n(?:[^\n]*\n){1,4}\s+last_stream_id=0 error_code=0\(NO_ERROR\)/m.test(r.stderr));
  const connsBefore = (srv.log.match(/stream=/g) || []).length;
  r = await bcurl([`localhost:${P_MAIN}/`, `127.0.0.1:${P_MAIN}/`]);
  await sleep(200);
  check('URLs naming different host:port -> exit 1, no connection made', r.code === 1 && /same host:port/.test(r.stderr) && (srv.log.match(/stream=/g) || []).length === connsBefore, r.stderr);
  r = await bcurl([`localhost:${P_REFUSED}/`]);
  check('connection refused -> exit 1', r.code === 1, `code=${r.code} ${r.stderr}`);
  r = await bcurl(['-q', 'x']);
  check('bad option -> exit 1', r.code === 1);
  r = await bcurl(['-v', '--grease', `localhost:${P_GREASE}/big.bin`, `localhost:${P_GREASE}/`, `localhost:${P_GREASE}/nope`]);
  check('grease both directions: body byte-exact, reserved frames seen', r.code === 4 && r.stdout.equals(Buffer.concat([big, index, Buffer.from('404 Not Found: not found\n')]))
    && (r.stderr.match(/^> UNKNOWN\(0xf/gm) || []).length === 3 && (r.stderr.match(/^< UNKNOWN\(0xf/gm) || []).length === 3, `code=${r.code}`);
  const par = await Promise.all(Array.from({ length: 8 }, () => bcurl([`localhost:${P_MAIN}/big.bin`, `localhost:${P_MAIN}/`])));
  check('8 concurrent connections all byte-exact', par.every((x) => x.code === 0 && x.stdout.equals(Buffer.concat([big, index]))));
  r = await bcurl(['-H', 'X-Custom: hi', '-H', 'user-agent: other', '-v', `localhost:${P_MAIN}/`]);
  check('-H adds literal header (lowercased) and overrides user-agent', r.code === 0 && /x-custom: hi   \(literal name\)/.test(r.stderr) && /^      user-agent: other$/m.test(r.stderr));

  console.log('raw frames -> bserve');
  let c = raw(P_MAIN);
  c.send(rawHeaders(1, Buffer.concat([field(1, 'GET'), field(2, '/'), field(3, 'x'), field(11, 'bad')])));
  let resp = await c.response(1);
  check('HEADERS with index 11 -> 400 on that stream', resp && resp.status === '400' && /index 11/.test(resp.body.toString()), resp && resp.body.toString());
  c.send(headers(3, GOOD('/')));
  resp = await c.response(3);
  check('... connection stays open, next valid request -> 200', resp && resp.status === '200' && resp.body.equals(index) && !c.ended && !c.frames.some((f) => f.type === T_GOAWAY));
  const malformed = [
    ['uppercase literal name', Buffer.concat([field(1, 'GET'), field(2, '/'), field(3, 'x'), field('User-Agent', 'x')])],
    ['empty literal name', Buffer.concat([field(1, 'GET'), field(2, '/'), field(3, 'x'), Buffer.from([0, 0, 0, 0, 1, 0x41])])],
    ['literal name starting with ":"', Buffer.concat([field(1, 'GET'), field(2, '/'), field(3, 'x'), field(':foo', 'x')])],
    ['value with LF', Buffer.concat([field(1, 'GET'), field(2, '/'), field(3, 'x'), field(9, 'a\nb')])],
    ['value with CR', Buffer.concat([field(1, 'GET'), field(2, '/'), field(3, 'x\r')])],
    ['value with NUL', Buffer.concat([field(1, 'GET'), field(2, '/\x00'), field(3, 'x')])],
    ['pseudo-header after regular', Buffer.concat([field(1, 'GET'), field(9, 'ua'), field(2, '/'), field(3, 'x')])],
    ['missing :authority', Buffer.concat([field(1, 'GET'), field(2, '/')])],
    ['two :path', Buffer.concat([field(1, 'GET'), field(2, '/'), field(2, '/'), field(3, 'x')])],
    [':status in request', Buffer.concat([field(1, 'GET'), field(2, '/'), field(3, 'x'), field(4, '200')])],
    [':path not starting with /', Buffer.concat([field(1, 'GET'), field(2, 'index.html'), field(3, 'x')])],
    ['field runs past end', Buffer.concat([field(1, 'GET'), field(2, '/'), field(3, 'x'), Buffer.from([9, 0, 10, 0x61])])],
    ['truncated name_len', Buffer.concat([field(1, 'GET'), field(2, '/'), field(3, 'x'), Buffer.from([0, 0])])],
    ['empty header block', Buffer.alloc(0)],
    [':method not a token (space)', Buffer.concat([field(1, 'GE T'), field(2, '/'), field(3, 'x')])],
    [':method empty', Buffer.concat([field(1, ''), field(2, '/'), field(3, 'x')])],
    ['content-length not digits', Buffer.concat([field(1, 'GET'), field(2, '/'), field(3, 'x'), field(6, 'abc')])],
    ['content-length empty', Buffer.concat([field(1, 'GET'), field(2, '/'), field(3, 'x'), field(6, '')])],
    ['content-length 1 but no DATA', Buffer.concat([field(1, 'GET'), field(2, '/'), field(3, 'x'), field(6, '1')])],
    ['two content-length values that disagree', Buffer.concat([field(1, 'GET'), field(2, '/'), field(3, 'x'), field(6, '0'), field(6, '1')])],
  ];
  let s = 5;
  for (const [name, block] of malformed) {
    c.send(rawHeaders(s, block));
    resp = await c.response(s);
    check(`malformed (${name}) -> 400, connection open`, resp && resp.status === '400' && !c.ended, resp ? resp.body.toString() : 'no response');
    s += 2;
  }
  c.send(rawHeaders(s, Buffer.concat([field(1, 'HEAD'), field(2, '/'), field(3, 'x'), field(11, '')])));
  resp = await c.response(s);
  check('malformed HEAD request -> body-less 400', resp && resp.status === '400' && resp.frames.length === 1);
  s += 2;
  // literal forms of table names are accepted; unknown flag bits ignored
  c.send(rawHeaders(s, Buffer.concat([field(1, 'GET'), field(2, '/sub/page.txt'), field(3, 'x'), field('user-agent', 'lit'), field('accept', '*/*')]), 0xff));
  resp = await c.response(s);
  check('literal names for table entries + undefined flag bits set -> 200', resp && resp.status === '200' && resp.body.equals(file('sub/page.txt')));
  s += 2;
  // request with a body split across frames with grease in between
  c.send(headers(s, [...GOOD('/'), ['content-length', '6']], 0), encodeFrame(0xf3, 0, s, Buffer.from('zz')), encodeFrame(T_DATA, 0, s, Buffer.from('abc')),
    encodeFrame(0x42, 0, 0, Buffer.from('unknown-type-on-stream-0')), encodeFrame(T_DATA, 0, s, Buffer.alloc(0)), encodeFrame(T_DATA, F_END_STREAM, s, Buffer.from('def')));
  resp = await c.response(s);
  check('GET with body in 3 DATA frames + unknown frames between -> 200', resp && resp.status === '200');
  s += 2;
  c.send(headers(s, [...GOOD('/'), ['content-length', '5']], 0), encodeFrame(T_DATA, F_END_STREAM, s, Buffer.from('abc')));
  resp = await c.response(s);
  check('request content-length mismatch -> 400', resp && resp.status === '400', resp && resp.body.toString());
  s += 2;
  c.send(headers(s, [...GOOD('/'), ['content-length', '007'], ['content-length', '7']], 0), encodeFrame(T_DATA, F_END_STREAM, s, Buffer.from('1234567')));
  resp = await c.response(s);
  check('repeated content-length, all equal to the DATA total ("007" and "7") -> 200', resp && resp.status === '200', resp && resp.body.toString());
  s += 2;
  c.send(headers(s, GOOD('/', 'HEAD')));
  resp = await c.response(s);
  check('HEAD -> single HEADERS with END_STREAM', resp && resp.status === '200' && resp.frames.length === 1 && resp.get('content-length') === String(index.length));
  s += 2;
  // pipelining: three requests in one write
  c.send(headers(s, GOOD('/big.bin')), headers(s + 1, GOOD('/nope')), headers(s + 2, GOOD('/')));
  const p1 = await c.response(s, 10_000), p2 = await c.response(s + 1), p3 = await c.response(s + 2);
  const order = c.frames.filter((f) => f.stream >= s && f.type === T_HEADERS).map((f) => f.stream);
  check('3 pipelined requests answered in order, each complete before the next', p1 && p1.body.equals(big) && p2 && p2.status === '404' && p3 && p3.body.equals(index) && order.join() === [s, s + 1, s + 2].join());
  const streamsSeq = c.frames.filter((f) => f.stream >= s && f.type !== 0xff).map((f) => f.stream);
  check('... responses not interleaved', streamsSeq.every((x, i) => i === 0 || x >= streamsSeq[i - 1]));
  check('connection still open after all of the above', !c.ended);
  c.close();

  const connErr = async (name, frames, expectLast = 0, opts = {}) => {
    const x = raw(P_MAIN, opts);
    x.send(...frames);
    const g = await x.goaway();
    await x.waitFor((y) => y.ended, 4000);
    check(`${name} -> GOAWAY(PROTOCOL_ERROR, last=${expectLast}) and close`, g && g.errorCode === 1 && g.lastStreamId === expectLast && x.ended, JSON.stringify(g));
    x.close();
    return x;
  };
  await connErr('HEADERS stream ID not above previous', [headers(3, GOOD('/')), headers(3, GOOD('/'))], 3);
  await connErr('HEADERS stream ID lower than previous', [headers(5, GOOD('/')), headers(2, GOOD('/'))], 5);
  await connErr('DATA with no request in progress', [encodeFrame(T_DATA, F_END_STREAM, 1, Buffer.from('x'))]);
  await connErr('DATA on another stream while a request is in progress', [headers(1, GOOD('/'), 0), encodeFrame(T_DATA, F_END_STREAM, 3, Buffer.from('x'))]);
  await connErr('HEADERS while a request is in progress', [headers(1, GOOD('/'), 0), headers(3, GOOD('/'))]);
  await connErr('HEADERS on stream 0', [headers(0, GOOD('/'))]);
  await connErr('DATA on stream 0', [encodeFrame(T_DATA, 0, 0, Buffer.alloc(0))]);
  await connErr('GOAWAY shorter than 8 octets', [encodeFrame(T_GOAWAY, 0, 0, Buffer.alloc(4))]);
  await connErr('GOAWAY on stream 1', [encodeFrame(T_GOAWAY, 0, 1, Buffer.alloc(8))]);
  const hx = await connErr('HTTP/1.1 request instead of preface', [Buffer.from('GET / HTTP/1.1\r\nHost: x\r\n\r\n')], 0, { preface: false });
  check('... server sent its own preface first', hx.preface && hx.preface.equals(PREFACE));

  // §1 (rev 2): a receiver MAY give up at the first differing octet -- bserve does.
  let x = raw(P_MAIN, { preface: false });
  x.send(Buffer.from('GET'));
  let g = await x.goaway(3000);
  check('3 wrong preface octets and a stall -> GOAWAY(PROTOCOL_ERROR) at once, not after 10 s', g && g.errorCode === 1 && g.at - x.t0 < 1500, JSON.stringify(g && { ...g, dt: g.at - x.t0 }));
  x.close();

  // rev 2 §3: GOAWAY from a client (last_stream_id 0) -- every complete request that arrived
  // before it is answered, the request in progress is dropped, then the server closes. The
  // server is not closing on an error/timeout, so it need not (and bserve does not) send GOAWAY.
  const noErrGoaway = (y) => !y.frames.some((f) => f.type === T_GOAWAY && decodeGoaway(f.payload).errorCode !== 0);
  c = raw(P_MAIN);
  c.send(headers(1, GOOD('/')), headers(2, GOOD('/big.bin')), headers(3, GOOD('/sub/'), 0), encodeGoaway(0, 0, 'bye'));
  let r1 = await c.response(1), r2 = await c.response(2, 10_000);
  await c.waitFor((y) => y.ended, 4000);
  check('client GOAWAY(0) after 2 complete + 1 in-progress request: 1 and 2 answered, 3 dropped, close, no error GOAWAY',
    r1 && r1.status === '200' && r2 && r2.body.equals(big) && !c.frames.some((f) => f.stream === 3) && c.ended && noErrGoaway(c));
  c.close();

  c = raw(P_MAIN);
  c.send(headers(1, GOOD('/')), encodeGoaway(0, 1, 'client saw an error'));
  r1 = await c.response(1);
  await c.waitFor((y) => y.ended, 4000);
  check('client error GOAWAY: earlier complete request still answered, then close', r1 && r1.status === '200' && c.ended && noErrGoaway(c));
  c.close();

  c = raw(P_MAIN);
  c.send(headers(1, GOOD('/big.bin')), headers(3, GOOD('/')));
  c.sock.end();
  r1 = await c.response(1, 10_000);
  r2 = await c.response(3);
  await c.waitFor((y) => y.ended, 4000);
  check('client pipelines 2 requests then half-closes -> both answered, then close, no error GOAWAY',
    r1 && r1.body.equals(big) && r2 && r2.body.equals(index) && c.ended && noErrGoaway(c));
  c.close();

  c = raw(P_MAIN);
  c.send(headers(1, GOOD('/')), headers(3, GOOD('/'), 0));
  c.sock.end();
  r1 = await c.response(1);
  await c.waitFor((y) => y.ended, 4000);
  check('client half-closes mid-request -> complete one answered, partial dropped, close', r1 && r1.status === '200' && !c.frames.some((f) => f.stream === 3) && c.ended);
  c.close();

  console.log('bcurl -> scripted fake server');
  // Besides the exit code, check the GOAWAY bcurl sends before closing (rev 2 §3: mandatory on
  // an error or timeout, last_stream_id always 0 from a client; optional when simply done, and
  // bcurl does send GOAWAY(NO_ERROR) then). For a server error GOAWAY bcurl answers NO_ERROR.
  const fakeCase = async (name, script, want, extraArgs = [], urlPath = '/', pred = () => true, goawayCode = want === 2 ? 1 : 0) => {
    fakeScript = script;
    fakeRx = [];
    const t0 = Date.now();
    const res = await bcurl([...extraArgs, `127.0.0.1:${P_FAKE}${urlPath}`]);
    res.ms = Date.now() - t0;
    await sleep(150);
    const gs = fakeRx.filter((f) => f.type === T_GOAWAY).map((f) => decodeGoaway(f.payload));
    const gOk = gs.length === 1 && gs[0].lastStreamId === 0 && gs[0].errorCode === goawayCode;
    check(`${name} -> exit ${want}, client GOAWAY(${goawayCode}, last=0)`, res.code === want && pred(res) && gOk,
      `code=${res.code} goaways=${JSON.stringify(gs)} ${res.stderr.trim().split('\n').pop()}`);
  };
  const P = PREFACE;
  await fakeCase('bad server preface (HTTP/1.1 reply)', (so) => so.end('HTTP/1.1 400 Bad Request\r\n\r\n'), 2);
  await fakeCase('response on wrong stream', (so, st) => so.end(Buffer.concat([P, respHeaders(st + 1, 200, [], F_END_STREAM)])), 2);
  await fakeCase('content-length larger than DATA', (so, st) => so.end(Buffer.concat([P, respHeaders(st, 200, [['content-length', '10']]), encodeFrame(T_DATA, F_END_STREAM, st, Buffer.from('12345'))])), 2);
  await fakeCase('content-length smaller than DATA', (so, st) => so.end(Buffer.concat([P, respHeaders(st, 200, [['content-length', '2']]), encodeFrame(T_DATA, F_END_STREAM, st, Buffer.from('12345'))])), 2);
  await fakeCase('bad preface octets then a stall (give up at first differing octet)', (so) => so.write('HTTP'), 2, [], '/', (res) => res.ms < 3000);
  await fakeCase('error GOAWAY', (so) => so.end(Buffer.concat([P, encodeGoaway(0, 2, 'boom')])), 2, [], '/', undefined, 0);
  await fakeCase('unknown GOAWAY code 77', (so) => so.end(Buffer.concat([P, encodeGoaway(0, 77, '')])), 2, [], '/', undefined, 0);
  await fakeCase('GOAWAY(NO_ERROR) below our stream', (so) => so.end(Buffer.concat([P, encodeGoaway(0, 0, 'idle')])), 2, [], '/', undefined, 0);
  await fakeCase('GOAWAY on stream 3', (so, st) => so.end(Buffer.concat([P, encodeFrame(T_GOAWAY, 0, 3, Buffer.alloc(8))])), 2);
  await fakeCase('GOAWAY shorter than 8 octets', (so) => so.end(Buffer.concat([P, encodeFrame(T_GOAWAY, 0, 0, Buffer.alloc(3))])), 2);
  await fakeCase('two content-length fields, both equal to DATA total', (so, st) => so.end(Buffer.concat([P, respHeaders(st, 200, [['content-length', '3'], ['content-length', '003']]), encodeFrame(T_DATA, F_END_STREAM, st, Buffer.from('abc'))])), 0, [], '/', (res) => res.stdout.toString() === 'abc');
  await fakeCase('two content-length fields that disagree', (so, st) => so.end(Buffer.concat([P, respHeaders(st, 200, [['content-length', '3'], ['content-length', '4']]), encodeFrame(T_DATA, F_END_STREAM, st, Buffer.from('abc'))])), 2);
  await fakeCase('content-length not digits', (so, st) => so.end(Buffer.concat([P, respHeaders(st, 200, [['content-length', '+3']]), encodeFrame(T_DATA, F_END_STREAM, st, Buffer.from('abc'))])), 2);
  await fakeCase('malformed header block (index 11)', (so, st) => so.end(Buffer.concat([P, rawHeaders(st, Buffer.concat([field(4, '200'), field(11, 'x')]))])), 2);
  await fakeCase(':status not 3 digits', (so, st) => so.end(Buffer.concat([P, respHeaders(st, '20', [], F_END_STREAM)])), 2);
  await fakeCase('request pseudo-header in response', (so, st) => so.end(Buffer.concat([P, headers(st, [[':status', '200'], [':path', '/']], F_END_STREAM)])), 2);
  await fakeCase('EOF mid-response', (so, st) => so.end(Buffer.concat([P, respHeaders(st, 200)])), 2);
  await fakeCase('EOF mid-frame', (so, st) => so.end(Buffer.concat([P, respHeaders(st, 200, [], F_END_STREAM).subarray(0, 10)])), 2);
  await fakeCase('DATA before HEADERS', (so, st) => so.end(Buffer.concat([P, encodeFrame(T_DATA, F_END_STREAM, st, Buffer.from('x'))])), 2);
  await fakeCase('DATA in HEAD response', (so, st) => so.end(Buffer.concat([P, respHeaders(st, 200, [['content-length', '1']]), encodeFrame(T_DATA, F_END_STREAM, st, Buffer.from('x'))])), 2, ['-I']);
  await fakeCase('HEADERS frame on stream 0', (so) => so.end(Buffer.concat([P, respHeaders(0, 200, [], F_END_STREAM)])), 2);
  await fakeCase('500 response', (so, st) => so.end(Buffer.concat([P, respHeaders(st, 500, [], F_END_STREAM)])), 5);
  await fakeCase('grease/unknown frames around a response, literal content-length, empty DATA', (so, st) => {
    so.write(P);
    so.write(encodeFrame(0xff, 0xff, 0, Buffer.from('g')));
    so.write(rawHeaders(st, Buffer.concat([field(4, '200'), field('content-length', '5'), field('x-extra', 'y')]), 0));
    so.write(encodeFrame(0x77, 0, st, Buffer.from('unknown')));
    so.write(encodeFrame(T_DATA, 0, st, Buffer.from('he')));
    so.write(encodeFrame(T_DATA, 0, st, Buffer.alloc(0)));
    so.write(encodeFrame(0xf0, 0, st, Buffer.alloc(0)));
    so.end(encodeFrame(T_DATA, F_END_STREAM, st, Buffer.from('llo')));
  }, 0, [], '/', (res) => res.stdout.toString() === 'hello');
  await fakeCase('HEAD response with content-length but no DATA', (so, st) => so.end(Buffer.concat([P, respHeaders(st, 200, [['content-length', '999']], F_END_STREAM)])), 0, ['-I'], '/', (res) => /content-length: 999/.test(res.stdout.toString()));
  await fakeCase('bcurl sent request headers correctly', (so, st, fields) => {
    const got = fields.map((f) => `${f.name}=${f.value}`).join('|');
    so.end(Buffer.concat([P, respHeaders(st, got === ':method=GET|:path=/a?b|:authority=127.0.0.1:9113|user-agent=bcurl.mjs/1|accept=*/*' && st === 1 ? 200 : 400, [], F_END_STREAM)]));
  }, 0, [], '/a?b');

  console.log('timeouts (runs in background, ~72 s)');
  for (const [name, ok, detail] of await timeoutsP) check(name, ok, detail);

  fake.close();
  killAll();
  await sleep(100);
  console.log(`\n${passed} passed, ${failures.length} failed`);
  if (failures.length) { console.log('failures:\n  ' + failures.join('\n  ')); process.exitCode = 1; }
  process.exit();
}

main().catch((e) => { console.error(e); killAll(); process.exit(1); });
