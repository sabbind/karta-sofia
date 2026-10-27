// Independent PMTiles v3 + MVT reader: renders a tile to PNG and prints a feature.
// usage: node verify.mjs file.pmtiles z lon lat out.png
import fs from "fs";
import zlib from "zlib";
import { createRequire } from "module"; const sharp = createRequire(import.meta.url)("/opt/npm-tools/node_modules/sharp");

const [file, zs, lons, lats, outPng] = process.argv.slice(2);
const buf = fs.readFileSync(file);
const z = +zs, lon = +lons, lat = +lats;
const n = 2 ** z;
const tx = Math.floor(((lon + 180) / 360) * n);
const tyF = (1 - Math.log(Math.tan((lat * Math.PI) / 180) + 1 / Math.cos((lat * Math.PI) / 180)) / Math.PI) / 2;
const ty = Math.floor(tyF * n);

// --- header
if (buf.toString("ascii", 0, 7) !== "PMTiles" || buf[7] !== 3) throw "bad magic";
const u64 = (o) => Number(buf.readBigUInt64LE(o));
const H = {
  rootOff: u64(8), rootLen: u64(16), metaOff: u64(24), metaLen: u64(32),
  leafOff: u64(40), leafLen: u64(48), dataOff: u64(56), dataLen: u64(64),
  minZ: buf[100], maxZ: buf[101], tileType: buf[99], tileComp: buf[98], intComp: buf[97],
};
console.log("header", H);
console.log("meta", zlib.gunzipSync(buf.subarray(H.metaOff, H.metaOff + H.metaLen)).toString());

// --- hilbert (Wikipedia xy2d, rot with full n)
function tileId(z, x, y) {
  let acc = 0;
  for (let i = 0; i < z; i++) acc += 4 ** i;
  const n = 2 ** z;
  let d = 0;
  for (let s = n / 2; s > 0; s = Math.floor(s / 2)) {
    const rx = (x & s) > 0 ? 1 : 0, ry = (y & s) > 0 ? 1 : 0;
    d += s * s * ((3 * rx) ^ ry);
    if (ry === 0) {
      if (rx === 1) { x = n - 1 - x; y = n - 1 - y; }
      [x, y] = [y, x];
    }
  }
  return acc + d;
}

function readVar(b, p) {
  let r = 0, s = 1;
  for (;;) { const c = b[p.i++]; r += (c & 0x7f) * s; s *= 128; if (c < 0x80) return r; }
}
function parseDir(raw) {
  const b = zlib.gunzipSync(raw), p = { i: 0 };
  const cnt = readVar(b, p), e = [];
  let last = 0;
  for (let i = 0; i < cnt; i++) { last += readVar(b, p); e.push({ id: last }); }
  for (const x of e) x.run = readVar(b, p);
  for (const x of e) x.len = readVar(b, p);
  for (let i = 0; i < cnt; i++) {
    const v = readVar(b, p);
    e[i].off = v === 0 && i > 0 ? e[i - 1].off + e[i - 1].len : v - 1;
  }
  return e;
}
function find(entries, id) {
  let best = null;
  for (const e of entries) if (e.id <= id) best = e;
  if (!best) return null;
  if (best.run === 0) return { leaf: best };
  if (id - best.id < best.run) return { tile: best };
  return null;
}
let dir = parseDir(buf.subarray(H.rootOff, H.rootOff + H.rootLen));
const id = tileId(z, tx, ty);
let hit = find(dir, id);
if (hit && hit.leaf) {
  const L = hit.leaf;
  dir = parseDir(buf.subarray(H.leafOff + L.off, H.leafOff + L.off + L.len));
  hit = find(dir, id);
}
if (!hit) throw `tile ${z}/${tx}/${ty} not found`;
const t = hit.tile;
const mvt = zlib.gunzipSync(buf.subarray(H.dataOff + t.off, H.dataOff + t.off + t.len));
console.log(`tile ${z}/${tx}/${ty} id ${id}: ${t.len} B gz, ${mvt.length} B raw`);

// --- minimal protobuf
function msg(b) {
  const p = { i: 0 }, out = [];
  while (p.i < b.length) {
    const k = readVar(b, p), f = Math.floor(k / 8), w = k & 7;
    if (w === 0) out.push([f, readVar(b, p)]);
    else if (w === 2) { const l = readVar(b, p); out.push([f, b.subarray(p.i, p.i + l)]); p.i += l; }
    else if (w === 1) { out.push([f, b.readDoubleLE(p.i)]); p.i += 8; }
    else if (w === 5) { out.push([f, b.readFloatLE(p.i)]); p.i += 4; }
    else throw "wt" + w;
  }
  return out;
}
const packedInts = (b) => { const p = { i: 0 }, r = []; while (p.i < b.length) r.push(readVar(b, p)); return r; };
const unzz = (n) => (n >>> 1) ^ -(n & 1);

const layers = msg(mvt).filter(([f]) => f === 3).map(([, b]) => b);
let svgPaths = [];
for (const lb of layers) {
  const L = msg(lb);
  const name = L.find(([f]) => f === 1)[1].toString();
  const extent = (L.find(([f]) => f === 5) || [0, 4096])[1];
  const keys = L.filter(([f]) => f === 3).map(([, b]) => b.toString());
  const vals = L.filter(([f]) => f === 4).map(([, b]) => {
    const [[f, v]] = msg(b);
    return f === 1 ? v.toString() : v;
  });
  const feats = L.filter(([f]) => f === 2).map(([, b]) => msg(b));
  console.log(`layer ${name} extent ${extent} features ${feats.length}`);
  let shown = 0;
  for (const F of feats) {
    const tags = packedInts(F.find(([f]) => f === 2)[1]);
    const props = {};
    for (let i = 0; i < tags.length; i += 2) props[keys[tags[i]]] = vals[tags[i + 1]];
    const g = packedInts(F.find(([f]) => f === 4)[1]);
    let x = 0, y = 0, i = 0, d = "", firstRing = null;
    while (i < g.length) {
      const c = g[i++], cmd = c & 7, cnt = c >> 3;
      if (cmd === 7) { d += "Z"; continue; }
      for (let k = 0; k < cnt; k++) {
        x += unzz(g[i++]); y += unzz(g[i++]);
        d += (cmd === 1 ? "M" : "L") + x + " " + y;
        if (!firstRing) firstRing = [x, y];
      }
    }
    svgPaths.push(d);
    if (shown < 2) {
      const wx = (tx + firstRing[0] / extent) / n, wy = (ty + firstRing[1] / extent) / n;
      const flon = wx * 360 - 180;
      const flat = (Math.atan(Math.sinh(Math.PI * (1 - 2 * wy))) * 180) / Math.PI;
      console.log("feature", JSON.stringify(props), "first vertex", flon.toFixed(6), flat.toFixed(6));
      shown++;
    }
  }
}
const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 4096 4096">
<rect width="4096" height="4096" fill="#f4f1ea"/><path fill-rule="nonzero" fill="#c0563f" stroke="#5a2318" stroke-width="3" d="${svgPaths.join(" ")}"/></svg>`;
await sharp(Buffer.from(svg)).png().toFile(outPng);
console.log("wrote", outPng);
