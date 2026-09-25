#!/usr/bin/env node
// rawtest.mjs - tiny raw-socket probe: a malformed HEADERS block must get a 400 on its stream,
// the connection must stay open, and a following valid request must succeed.
// usage: node rawtest.mjs [host] [port]      (defaults 127.0.0.1 9111). Exit 0 = pass.

import net from 'node:net';
import { PREFACE, FrameReader, T_HEADERS, T_DATA, T_GOAWAY, F_END_STREAM, encodeFrame, encodeHeaderBlock, decodeHeaderBlock, decodeGoaway } from './bhttp.mjs';

const host = process.argv[2] ?? '127.0.0.1';
const port = +(process.argv[3] ?? 9111);

// field: index 1 :method GET, 2 :path /, 3 :authority x, then index 11 (malformed per §4)
const bad = Buffer.from([1, 0, 3, 0x47, 0x45, 0x54, 2, 0, 1, 0x2f, 3, 0, 1, 0x78, 11, 0, 0]);
const good = encodeHeaderBlock([[':method', 'GET'], [':path', '/'], [':authority', `${host}:${port}`]]);

const sock = net.connect({ host, port });
const reader = new FrameReader();
const resp = new Map(); // stream -> {status, body, done}
let step = 0;
const timer = setTimeout(() => finish(false, 'timed out'), 5000);

function finish(ok, msg) {
  clearTimeout(timer);
  console.log(`${ok ? 'PASS' : 'FAIL'}: ${msg}`);
  process.exitCode = ok ? 0 : 1;
  sock.destroy();
}

sock.on('connect', () => sock.write(Buffer.concat([PREFACE, encodeFrame(T_HEADERS, F_END_STREAM, 1, bad)])));
sock.on('data', (d) => {
  reader.push(d);
  let it;
  while ((it = reader.next())) {
    if (it.kind === 'preface') { if (!it.ok) return finish(false, 'bad server preface'); continue; }
    if (it.type === T_GOAWAY) return finish(false, `server sent GOAWAY ${JSON.stringify(decodeGoaway(it.payload))}`);
    if (it.type !== T_HEADERS && it.type !== T_DATA) continue;
    let r = resp.get(it.stream);
    if (!r) resp.set(it.stream, (r = { status: null, body: [] }));
    if (it.type === T_HEADERS) r.status = decodeHeaderBlock(it.payload).fields.find((f) => f.name === ':status')?.value.toString();
    else r.body.push(Buffer.from(it.payload));
    if (!(it.flags & F_END_STREAM)) continue;
    const body = Buffer.concat(r.body).toString().trim();
    console.log(`stream ${it.stream}: status ${r.status} body ${JSON.stringify(body.slice(0, 80))}`);
    if (step === 0) {
      if (it.stream !== 1 || r.status !== '400') return finish(false, 'expected 400 on stream 1');
      step = 1;
      sock.write(encodeFrame(T_HEADERS, F_END_STREAM, 3, good));
    } else {
      return finish(it.stream === 3 && r.status === '200', 'malformed HEADERS -> 400, connection kept, next request -> ' + r.status);
    }
  }
});
sock.on('end', () => finish(false, 'server closed the connection'));
sock.on('error', (e) => finish(false, e.message));
