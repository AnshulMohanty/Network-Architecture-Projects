// bhttp.mjs - shared BHTTP/1 wire helpers (framing, header blocks, hexdump).
// Written from SPEC.md only. Node built-ins only.

export const PREFACE = Buffer.from('BHTTP/1\n', 'latin1');       // §1
export const FRAME_HEADER_LEN = 8;                                  // §2
export const MAX_PAYLOAD = 0xffff;                                  // §2 (16-bit Length)
export const MAX_DATA_CHUNK = 16384;                                // §5 (server DATA size)

export const T_DATA = 0x00;
export const T_HEADERS = 0x01;
export const T_GOAWAY = 0x02;
export const F_END_STREAM = 0x01;

export const E_NO_ERROR = 0;
export const E_PROTOCOL_ERROR = 1;
export const E_INTERNAL_ERROR = 2;
export const E_TIMEOUT = 3;
const ERROR_NAMES = ['NO_ERROR', 'PROTOCOL_ERROR', 'INTERNAL_ERROR', 'TIMEOUT'];
export function errorName(code) {
  return ERROR_NAMES[code] ?? `UNKNOWN_ERROR(${code})`;
}

// §4 static table (index 1..10)
export const STATIC_TABLE = [
  null, ':method', ':path', ':authority', ':status', 'content-type',
  'content-length', 'date', 'server', 'user-agent', 'accept',
];
const STATIC_INDEX = new Map();
for (let i = 1; i < STATIC_TABLE.length; i++) STATIC_INDEX.set(STATIC_TABLE[i], i);

// §4 allowed literal-name octets: a-z 0-9 ! # $ % & ' * + - . ^ _ ` | ~
const NAME_OK = new Uint8Array(256);
for (const ch of "abcdefghijklmnopqrstuvwxyz0123456789!#$%&'*+-.^_`|~") NAME_OK[ch.charCodeAt(0)] = 1;
// §4 ":method (a token)". The spec calls literal names a "lowercase token", so a token is taken
// to be the same set plus A-Z (RFC 9110 tchar), non-empty.
const TOKEN_OK = Uint8Array.from(NAME_OK);
for (let c = 0x41; c <= 0x5a; c++) TOKEN_OK[c] = 1;
export function isToken(buf) {
  if (buf.length === 0) return false;
  for (const c of buf) if (!TOKEN_OK[c]) return false;
  return true;
}

export function isValidLiteralName(name) {
  if (typeof name !== 'string' || name.length === 0) return false;
  for (let i = 0; i < name.length; i++) {
    const c = name.charCodeAt(i);
    if (c > 255 || !NAME_OK[c]) return false;
  }
  return true;
}

export function isValidValue(buf) {
  for (const c of buf) if (c === 0x00 || c === 0x0a || c === 0x0d) return false;
  return true;
}

export function frameTypeName(type) {
  switch (type) {
    case T_DATA: return 'DATA';
    case T_HEADERS: return 'HEADERS';
    case T_GOAWAY: return 'GOAWAY';
    default:
      return type >= 0xf0 ? `UNKNOWN(0x${hex2(type)},reserved/grease)` : `UNKNOWN(0x${hex2(type)})`;
  }
}

function hex2(n) { return n.toString(16).padStart(2, '0'); }

export function encodeFrame(type, flags, stream, payload = Buffer.alloc(0)) {
  if (payload.length > MAX_PAYLOAD) throw new RangeError(`frame payload ${payload.length} > 65535`);
  const b = Buffer.allocUnsafe(FRAME_HEADER_LEN + payload.length);
  b.writeUInt16BE(payload.length, 0);
  b[2] = type & 0xff;
  b[3] = flags & 0xff;
  b.writeUInt32BE(stream >>> 0, 4);
  payload.copy(b, FRAME_HEADER_LEN);
  return b;
}

export function encodeGoaway(lastStreamId, errorCode, debug = '') {
  let dbg = Buffer.from(String(debug), 'utf8');
  if (dbg.length > MAX_PAYLOAD - 8) dbg = dbg.subarray(0, MAX_PAYLOAD - 8);
  const p = Buffer.allocUnsafe(8 + dbg.length);
  p.writeUInt32BE(lastStreamId >>> 0, 0);
  p.writeUInt32BE(errorCode >>> 0, 4);
  dbg.copy(p, 8);
  return encodeFrame(T_GOAWAY, 0, 0, p);
}

export function decodeGoaway(payload) {
  if (payload.length < 8) return null;
  return {
    lastStreamId: payload.readUInt32BE(0),
    errorCode: payload.readUInt32BE(4),
    debug: payload.subarray(8).toString('utf8'),
  };
}

// Incremental frame parser. push() bytes, then call next() until it returns null.
export class FrameReader {
  constructor({ expectPreface = true } = {}) {
    this.buf = Buffer.alloc(0);
    this.needPreface = expectPreface;
  }
  push(chunk) {
    this.buf = this.buf.length ? Buffer.concat([this.buf, chunk]) : chunk;
  }
  get buffered() { return this.buf.length; }
  // Returns {kind:'preface', raw} | {kind:'frame', type, flags, stream, payload, raw} | null
  next() {
    if (this.needPreface) {
      // §1: a receiver MAY give up at the first octet that differs.
      const n = Math.min(this.buf.length, PREFACE.length);
      if (!this.buf.subarray(0, n).equals(PREFACE.subarray(0, n))) {
        const raw = this.buf.subarray(0, n);
        this.buf = this.buf.subarray(n);
        this.needPreface = false;
        return { kind: 'preface', raw, ok: false };
      }
      if (this.buf.length < PREFACE.length) return null;
      const raw = this.buf.subarray(0, PREFACE.length);
      this.buf = this.buf.subarray(PREFACE.length);
      this.needPreface = false;
      return { kind: 'preface', raw, ok: raw.equals(PREFACE) };
    }
    if (this.buf.length < FRAME_HEADER_LEN) return null;
    const len = this.buf.readUInt16BE(0);
    if (this.buf.length < FRAME_HEADER_LEN + len) return null;
    const raw = this.buf.subarray(0, FRAME_HEADER_LEN + len);
    this.buf = this.buf.subarray(FRAME_HEADER_LEN + len);
    return {
      kind: 'frame',
      type: raw[2],
      flags: raw[3],
      stream: raw.readUInt32BE(4),
      payload: raw.subarray(FRAME_HEADER_LEN),
      raw,
    };
  }
}

// §4 header block encoder. fields: array of [name, value(string|Buffer)].
export function encodeHeaderBlock(fields) {
  const parts = [];
  for (const [name, value] of fields) {
    const v = Buffer.isBuffer(value) ? value : Buffer.from(String(value), 'utf8');
    if (v.length > 0xffff) throw new RangeError(`header value too long for ${name}`);
    const idx = STATIC_INDEX.get(name);
    if (idx !== undefined) {
      const h = Buffer.allocUnsafe(1);
      h[0] = idx;
      parts.push(h);
    } else {
      if (!isValidLiteralName(name)) throw new Error(`invalid literal header name ${JSON.stringify(name)}`);
      const n = Buffer.from(name, 'latin1');
      const h = Buffer.allocUnsafe(3);
      h[0] = 0;
      h.writeUInt16BE(n.length, 1);
      parts.push(h, n);
    }
    const vl = Buffer.allocUnsafe(2);
    vl.writeUInt16BE(v.length, 0);
    parts.push(vl, v);
  }
  const block = Buffer.concat(parts);
  if (block.length > MAX_PAYLOAD) throw new RangeError('header block does not fit in one HEADERS frame');
  return block;
}

// §4 header block decoder with the generic validity rules (not the request/response rules).
// Returns { fields: [{name, value:Buffer, index}], error: string|null }.
// On error, `fields` holds the fields decoded before the problem.
export function decodeHeaderBlock(p) {
  const fields = [];
  let pos = 0;
  let seenRegular = false;
  const fail = (msg) => ({ fields, error: msg });
  while (pos < p.length) {
    const at = pos;
    const idx = p[pos++];
    let name;
    if (idx > 10) return fail(`field at offset ${at}: index ${idx} is above 10`);
    if (idx === 0) {
      if (pos + 2 > p.length) return fail(`field at offset ${at}: name_len runs past the end of the block`);
      const nl = p.readUInt16BE(pos); pos += 2;
      if (nl === 0) return fail(`field at offset ${at}: empty literal name`);
      if (pos + nl > p.length) return fail(`field at offset ${at}: literal name runs past the end of the block`);
      const nb = p.subarray(pos, pos + nl); pos += nl;
      if (nb[0] === 0x3a) return fail(`field at offset ${at}: literal name starts with ":"`);
      for (const c of nb) {
        if (!NAME_OK[c]) return fail(`field at offset ${at}: literal name contains invalid octet 0x${hex2(c)}`);
      }
      name = nb.toString('latin1');
    } else {
      name = STATIC_TABLE[idx];
    }
    if (pos + 2 > p.length) return fail(`field at offset ${at} (${name}): value_len runs past the end of the block`);
    const vl = p.readUInt16BE(pos); pos += 2;
    if (pos + vl > p.length) return fail(`field at offset ${at} (${name}): value runs past the end of the block`);
    const value = p.subarray(pos, pos + vl); pos += vl;
    if (!isValidValue(value)) return fail(`field ${name}: value contains NUL, LF or CR`);
    const pseudo = name.charCodeAt(0) === 0x3a;
    if (pseudo && seenRegular) return fail(`pseudo-header ${name} after a regular header`);
    if (!pseudo) seenRegular = true;
    fields.push({ name, value, index: idx });
  }
  return { fields, error: null };
}

function countNames(fields) {
  const c = Object.create(null);
  for (const f of fields) c[f.name] = (c[f.name] ?? 0) + 1;
  return c;
}

// §4 request rules. Returns error string or null.
export function validateRequestFields(fields) {
  const c = countNames(fields);
  if (c[':status']) return 'request carries :status';
  for (const n of [':method', ':path', ':authority']) {
    if ((c[n] ?? 0) !== 1) return `request must have exactly one ${n} (has ${c[n] ?? 0})`;
  }
  const method = fields.find((f) => f.name === ':method').value;
  if (!isToken(method)) return `:method ${JSON.stringify(printable(method))} is not a token`;
  const path = fields.find((f) => f.name === ':path').value;
  if (path.length === 0 || path[0] !== 0x2f) return ':path does not start with "/"';
  return null;
}

// §4 response rules. Returns error string or null.
export function validateResponseFields(fields) {
  const c = countNames(fields);
  for (const n of [':method', ':path', ':authority']) {
    if (c[n]) return `response carries request pseudo-header ${n}`;
  }
  if ((c[':status'] ?? 0) !== 1) return `response must have exactly one :status (has ${c[':status'] ?? 0})`;
  const st = fields.find((f) => f.name === ':status').value.toString('latin1');
  if (!/^[0-9]{3}$/.test(st)) return `:status ${JSON.stringify(st)} is not three ASCII digits`;
  return null;
}

// §3: if content-length is present it MUST be decimal digits equal to the total DATA length.
// Names may repeat (§4), so every content-length field must satisfy that; two different values
// can never both hold and are rejected at once.
// Returns {value:number|null, error:string|null}. value is Infinity for absurdly long numbers.
export function parseContentLength(fields) {
  const cl = fields.filter((f) => f.name === 'content-length');
  if (cl.length === 0) return { value: null, error: null };
  let value = null;
  for (const f of cl) {
    const s = f.value.toString('latin1');
    if (!/^[0-9]+$/.test(s)) return { value: null, error: `content-length ${JSON.stringify(printable(f.value))} is not decimal digits` };
    const t = s.replace(/^0+(?=.)/, '');
    const n = t.length > 15 ? Infinity : Number(t);
    if (value !== null && n !== value) return { value: null, error: 'content-length fields disagree' };
    value = n;
  }
  return { value, error: null };
}

export function getField(fields, name) {
  const f = fields.find((x) => x.name === name);
  return f ? f.value : null;
}

// Printable rendering of an octet string (for logs and -v).
export function printable(buf) {
  let s = '';
  for (const c of buf) {
    if (c >= 0x20 && c < 0x7f && c !== 0x5c) s += String.fromCharCode(c);
    else if (c === 0x5c) s += '\\\\';
    else s += '\\x' + hex2(c);
  }
  return s;
}

// Classic hexdump: offset, 16 hex octets (gap after 8), ASCII column.
export function hexdump(buf, baseOffset = 0, indent = '    ') {
  const lines = [];
  for (let i = 0; i < buf.length; i += 16) {
    const row = buf.subarray(i, i + 16);
    let hex = '';
    let asc = '';
    for (let j = 0; j < 16; j++) {
      if (j === 8) hex += ' ';
      if (j < row.length) {
        hex += hex2(row[j]) + ' ';
        asc += row[j] >= 0x20 && row[j] < 0x7f ? String.fromCharCode(row[j]) : '.';
      } else {
        hex += '   ';
      }
    }
    lines.push(`${indent}${(baseOffset + i).toString(16).padStart(8, '0')}  ${hex} |${asc}|`);
  }
  if (buf.length === 0) lines.push(`${indent}(empty)`);
  return lines.join('\n');
}

export function flagsText(type, flags) {
  const names = [];
  if ((type === T_DATA || type === T_HEADERS) && (flags & F_END_STREAM)) names.push('END_STREAM');
  const s = `flags=0x${hex2(flags)}`;
  return names.length ? `${s}(${names.join('|')})` : s;
}
