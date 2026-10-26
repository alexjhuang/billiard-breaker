// The billiard-ball machine on the GPU (WebGPU), for the static site. Same
// hardware data and the same results as web/engine.js (which it falls back to),
// checked by tests/test_gpu.py.
//
// The band is cut across (by x-y) into T tiles of equal ball counts, each a
// compute workgroup of 64 threads (3 balls each) holding its balls and a light-cone margin
// (G = 2D + 4 rows, nothing moves faster than 2 rows a step). A dispatch is one
// epoch of D steps: every tile reads the ball list the previous epoch wrote,
// sorts it by x-y to find its range, takes the balls in its window, steps them
// D times (the Margolus rule on each ball's 2x2 block, all balls at once, the
// cells in three rotating hash sets in workgroup memory: two barriers a step),
// and writes back the balls that ended in its own range: exact, as in
// csim/bbm.c's tiles. The mirrors are two read-only bitmaps (rows of constant
// x+y and of constant x-y) with one-bit-per-word summaries; a ball reads them
// only when its direction changes, and the words are fetched a step early so
// the latency overlaps the barriers. Feeds, reads and pictures are served in
// the shader at their steps; a frame is P / D dispatches in one command buffer
// (each with its own uniform offset) and one readback. With timestamp-query
// the GPU time of a frame is measured too.
'use strict';
const PADG = 2, NREC = 64, SLOTS = 192, HASH = 256, LMAX = 1024, WG = 64, SPT = SLOTS / WG;  // SPT slots per thread

const wgsl = (SVOFF, HBINS) => /* wgsl */`
struct Params {
  U: i32, U2: i32, lo: i32, hi: i32, S: i32, RWU: i32, RWV: i32, P: i32,
  par0: i32, nfeeds: i32, nreads: i32, npics: i32, left: i32, right: i32, reset: i32, T: i32,
  picT: vec4<i32>, picK: vec4<i32>, picOff: vec4<i32>,
  SWU: i32, SWV: i32, spin: i32, pad1: i32,
};
struct Epoch { t0: i32, t1: i32, inBase: i32, outBase: i32 }   // one per dispatch (dynamic offset)
@group(0) @binding(0) var<uniform> u: Params;
@group(0) @binding(1) var<storage, read> wu: array<u32>;         // walls by row x+y, bit x-y-lo+PAD
@group(0) @binding(2) var<storage, read> wv: array<u32>;         // walls by row x-y-lo+PAD, bit x+y
@group(0) @binding(3) var<storage, read> sched: array<vec4<i32>>; // feeds: t, x, y, gate (0 always, 1 left, 2 right, 3 reset)
@group(0) @binding(4) var<storage, read> reads: array<vec4<i32>>; // t, x, y, output index
@group(0) @binding(5) var<storage, read_write> lst: array<i32>;   // ball lists, two halves: [base + k] = tile k's count, then records (x, y, wallk, wallbits) at base + T + 4 (k SLOTS + i)
@group(0) @binding(6) var<storage, read_write> outb: array<atomic<u32>>;  // [0] errors; [16..] read bits; [1024..] balls per picture step; [2048..] pic cells
@group(0) @binding(7) var<storage, read> smry: array<u32>;         // summaries: bit w of row u2 = word w of wu's row is not zero (SWU words a row); then wv's from SVOFF (SWV words a row)
@group(0) @binding(8) var<uniform> ep: Epoch;

const SLOTS = ${SLOTS}u;
const HASH = ${HASH}u;
const LMAX = ${LMAX}u;
const EMPTY = 0xffffffffu;
const DEAD = -2147483647 - 1;
const INF = 0x3fffffff;
const PAD = ${PADG};
const OUT_READS = 16u;
const OUT_COUNTS = 1024u;
const OUT_CELLS = 2048u;
const SVOFF = ${SVOFF};
const HB = 32;            // histogram bin: rows of x - y
const HBINS = ${HBINS}u;

var<workgroup> px: array<i32, SLOTS>;
var<workgroup> py: array<i32, SLOTS>;
var<workgroup> wk: array<i32, SLOTS>;   // steps until the block in front holds a wall (-1: unknown), DEAD if free
var<workgroup> wb: array<u32, SLOTS>;   // the wall cells of that block, bit c = corner c
var<workgroup> hk: array<atomic<u32>, ${3 * HASH}>;  // three cell sets: step t reads t % 3, writes (t + 1) % 3, clears (t + 2) % 3
var<workgroup> freeS: array<u32, SLOTS>;
var<workgroup> nfree: atomic<u32>;
var<workgroup> nslots: atomic<u32>;
var<workgroup> nkept: atomic<u32>;
var<workgroup> err: atomic<u32>;
var<workgroup> fi: i32;
var<workgroup> ri: i32;
var<workgroup> nextF: i32;   // time of feed fi (INF past the end): no polling of global memory every step
var<workgroup> nextR: i32;
var<workgroup> lv: array<i32, LMAX>;       // x - y of every ball in the list
var<workgroup> hist: array<atomic<u32>, HBINS>;   // balls per HB rows of x - y, then its prefix sum
var<workgroup> segOff: array<i32, 65>;     // prefix of the segment counts (T <= 64)
var<workgroup> nTot: i32;
var<workgroup> own_lo: i32;
var<workgroup> own_hi: i32;   // own range [own_lo, own_hi)
var<workgroup> win_lo: i32;
var<workgroup> win_hi: i32;   // window [win_lo, win_hi]

fn wrapi(a: i32, m: i32) -> i32 { var r = a; loop { if (r >= 0) { break; } r += m; } loop { if (r < m) { break; } r -= m; } return r; }

fn cellkey(x: i32, y: i32) -> u32 {  // x + y canonical
  return u32((x + y) * u.S + (x - y - u.lo + PAD));
}

fn hhash(k: u32) -> u32 { return (k * 2654435761u) % HASH; }

fn hinsert(tb: u32, k: u32) {
  var h = hhash(k);
  for (var i = 0u; i < HASH; i++) {
    let r = atomicCompareExchangeWeak(&hk[tb + h], EMPTY, k);
    if (r.exchanged || r.old_value == k) { return; }
    h = (h + 1u) % HASH;
  }
  atomicOr(&err, 4u);
}

fn hhas(tb: u32, k: u32) -> bool {
  var h = hhash(k);
  for (var i = 0u; i < HASH; i++) {
    let v = atomicLoad(&hk[tb + h]);
    if (v == k) { return true; }
    if (v == EMPTY) { return false; }
    h = (h + 1u) % HASH;
  }
  return false;
}

fn iswall(x: i32, y: i32) -> u32 {  // any representative
  let v = x - y;
  if (v < u.lo || v > u.hi) { return 0u; }
  let u2 = wrapi(x + y, u.U2);
  let vv = v - u.lo + PAD;
  return (wu[u32(u2 * u.RWU + (vv >> 5))] >> u32(vv & 31)) & 1u;
}

fn ctz(m: u32) -> u32 { return countTrailingZeros(m); }

// Three rows of a bitmap around word i: w[3r + 0..2] = row r at words i-1, i, i+1
// (in scan direction), tagged by the middle row offset so a prefetch can be matched.
struct Scan { q: i32, i: i32, tag: i32, w: array<u32, 9> }

fn fetchV(r0: i32, r1: i32, r2: i32, i: i32, dir: i32, nw: i32) -> Scan {
  var sc: Scan;
  var ia = i - dir; if (ia < 0) { ia += nw; } if (ia >= nw) { ia -= nw; }
  var ib = i + dir; if (ib < 0) { ib += nw; } if (ib >= nw) { ib -= nw; }
  sc.w[0] = wv[u32(r0 + ia)]; sc.w[1] = wv[u32(r0 + i)]; sc.w[2] = wv[u32(r0 + ib)];
  sc.w[3] = wv[u32(r1 + ia)]; sc.w[4] = wv[u32(r1 + i)]; sc.w[5] = wv[u32(r1 + ib)];
  sc.w[6] = wv[u32(r2 + ia)]; sc.w[7] = wv[u32(r2 + i)]; sc.w[8] = wv[u32(r2 + ib)];
  sc.i = i; sc.tag = r1 * 2 + (dir + 1) / 2; sc.q = -1;
  return sc;
}
fn fetchU(r0: i32, r1: i32, r2: i32, i: i32, dir: i32, nw: i32) -> Scan {
  var sc: Scan;
  let ia = clamp(i - dir, 0, nw - 1);
  let ib = clamp(i + dir, 0, nw - 1);
  sc.w[0] = wu[u32(r0 + ia)]; sc.w[1] = wu[u32(r0 + i)]; sc.w[2] = wu[u32(r0 + ib)];
  sc.w[3] = wu[u32(r1 + ia)]; sc.w[4] = wu[u32(r1 + i)]; sc.w[5] = wu[u32(r1 + ib)];
  sc.w[6] = wu[u32(r2 + ia)]; sc.w[7] = wu[u32(r2 + i)]; sc.w[8] = wu[u32(r2 + ib)];
  sc.i = i; sc.tag = r1 * 2 + (dir + 1) / 2 + 1; sc.q = -1;
  return sc;
}

// The scan from bit p in direction dir: the word holding p and its neighbours
// (prefetched when pre matches), else the summaries to find the next word with
// any bit in any of the three rows, then that word. A ball whose column is
// empty for thousands of cells (a falling messenger) thus costs ~3 memory
// round trips, not hundreds; a lane's rescan usually none at all.
fn scanV(r0: i32, r1: i32, r2: i32, rs0: i32, rs1: i32, rs2: i32, p: i32, dir: i32, nw: i32, nws: i32, pre: ptr<function, Scan>) -> Scan {
  var sc: Scan;
  sc.q = -1;
  var i = p >> 5;
  if (i < 0 || i >= nw) { return sc; }
  if ((*pre).i == i && (*pre).tag == r1 * 2 + (dir + 1) / 2) { sc = *pre; } else { sc = fetchV(r0, r1, r2, i, dir, nw); }
  var m = sc.w[1] | sc.w[4] | sc.w[7];
  if (dir > 0) { m &= 0xffffffffu << u32(p & 31); } else { m &= 0xffffffffu >> u32(31 - (p & 31)); }
  if (m == 0u) {
    var i2 = i + dir;
    if (i2 < 0 || i2 >= nw) { sc.q = -1; return sc; }
    var j = i2 >> 5;
    var sm = smry[u32(SVOFF + rs0 + j)] | smry[u32(SVOFF + rs1 + j)] | smry[u32(SVOFF + rs2 + j)];
    if (dir > 0) { sm &= 0xffffffffu << u32(i2 & 31); } else { sm &= 0xffffffffu >> u32(31 - (i2 & 31)); }
    loop {
      if (sm != 0u) { break; }
      j += dir;
      if (j < 0 || j >= nws) { sc.q = -1; return sc; }
      sm = smry[u32(SVOFF + rs0 + j)] | smry[u32(SVOFF + rs1 + j)] | smry[u32(SVOFF + rs2 + j)];
    }
    if (dir > 0) { i = j * 32 + i32(ctz(sm)); } else { i = j * 32 + 31 - i32(countLeadingZeros(sm)); }
    if (i >= nw) { sc.q = -1; return sc; }
    sc = fetchV(r0, r1, r2, i, dir, nw);
    m = sc.w[1] | sc.w[4] | sc.w[7];
  }
  if (dir > 0) { sc.q = i * 32 + i32(ctz(m)); } else { sc.q = i * 32 + 31 - i32(countLeadingZeros(m)); }
  return sc;
}

fn scanU(r0: i32, r1: i32, r2: i32, rs0: i32, rs1: i32, rs2: i32, p: i32, dir: i32, nw: i32, nws: i32, pre: ptr<function, Scan>) -> Scan {
  var sc: Scan;
  sc.q = -1;
  var i = p >> 5;
  if (i < 0 || i >= nw) { return sc; }
  if ((*pre).i == i && (*pre).tag == r1 * 2 + (dir + 1) / 2 + 1) { sc = *pre; } else { sc = fetchU(r0, r1, r2, i, dir, nw); }
  var m = sc.w[1] | sc.w[4] | sc.w[7];
  if (dir > 0) { m &= 0xffffffffu << u32(p & 31); } else { m &= 0xffffffffu >> u32(31 - (p & 31)); }
  if (m == 0u) {
    var i2 = i + dir;
    if (i2 < 0 || i2 >= nw) { sc.q = -1; return sc; }
    var j = i2 >> 5;
    var sm = smry[u32(rs0 + j)] | smry[u32(rs1 + j)] | smry[u32(rs2 + j)];
    if (dir > 0) { sm &= 0xffffffffu << u32(i2 & 31); } else { sm &= 0xffffffffu >> u32(31 - (i2 & 31)); }
    loop {
      if (sm != 0u) { break; }
      j += dir;
      if (j < 0 || j >= nws) { sc.q = -1; return sc; }
      sm = smry[u32(rs0 + j)] | smry[u32(rs1 + j)] | smry[u32(rs2 + j)];
    }
    if (dir > 0) { i = j * 32 + i32(ctz(sm)); } else { i = j * 32 + 31 - i32(countLeadingZeros(sm)); }
    if (i >= nw) { sc.q = -1; return sc; }
    sc = fetchU(r0, r1, r2, i, dir, nw);
    m = sc.w[1] | sc.w[4] | sc.w[7];
  }
  if (dir > 0) { sc.q = i * 32 + i32(ctz(m)); } else { sc.q = i * 32 + 31 - i32(countLeadingZeros(m)); }
  return sc;
}

// bit pos of row r (0..2) of a scan, if it lies in the words fetched, else -1
fn scanBit(sc: ptr<function, Scan>, r: i32, pos: i32, nw: i32, dir: i32) -> i32 {
  let wi = pos >> 5;
  let d = (wi - (*sc).i) * dir;
  var k = -1;
  if (d == 0) { k = 1; } else if (d == 1 || d == 1 - nw) { k = 2; } else if (d == -1 || d == nw - 1) { k = 0; }
  if (k < 0) { return -1; }
  return i32(((*sc).w[3 * r + k] >> u32(pos & 31)) & 1u);
}

struct Front { k: i32, bits: u32 }

fn blockWalls(x: i32, y: i32, dx: i32, dy: i32, k: i32, par: i32) -> u32 {  // the slow way, for the band's edge
  let xk = x + dx * k; let yk = y + dy * k;
  let o = (par + k) & 1;
  let x0 = xk - ((xk - o) & 1); let y0 = yk - ((yk - o) & 1);
  return iswall(x0, y0) | (iswall(x0 + 1, y0) << 1u) | (iswall(x0, y0 + 1) << 2u) | (iswall(x0 + 1, y0 + 1) << 3u);
}

// the words a ball at (x, y) moving (dx, dy) will scan first: fetched early so the latency is hidden
fn prefetch(x: i32, y: i32, dx: i32, dy: i32) -> Scan {
  let u2 = wrapi(x + y, u.U2);
  let vv = x - y - u.lo + PAD;
  if (dx == dy) {
    let r0 = (vv - 1) * u.RWV;
    let nw = (u.U2 + 31) >> 5;
    var p = u2 + dx; if (p < 0) { p += u.U2; } if (p >= u.U2) { p -= u.U2; }
    return fetchV(r0, r0 + u.RWV, r0 + 2 * u.RWV, p >> 5, dx, nw);
  }
  var ua = u2 - 1; if (ua < 0) { ua = u.U2 - 1; }
  var ub = u2 + 1; if (ub == u.U2) { ub = 0; }
  let R = u.RWU;
  let p = vv - dy;   // NE (dy < 0) looks at rising v
  if (p < 0 || p >= u.S) { var none: Scan; none.i = -1; return none; }
  return fetchU(ua * R, u2 * R, ub * R, p >> 5, -dy, R);
}

// steps until the block in front of a ball at (x, y) moving (dx, dy) holds a
// wall (or it leaves the band), with that block's wall cells
fn front(x: i32, y: i32, dx: i32, dy: i32, par: i32, pre: ptr<function, Scan>) -> Front {
  var f: Front;
  f.k = INF; f.bits = 0u;
  let u2 = wrapi(x + y, u.U2);
  let vv = x - y - u.lo + PAD;
  var sc: Scan;
  var k = INF;
  if (dx == dy) {  // SE / NW: along a row of constant v, round the loop
    let r0 = (vv - 1) * u.RWV; let r1 = r0 + u.RWV; let r2 = r1 + u.RWV;
    let s0 = (vv - 1) * u.SWV; let s1 = s0 + u.SWV; let s2 = s1 + u.SWV;
    let nw = (u.U2 + 31) >> 5;
    if (dx > 0) {
      sc = scanV(r0, r1, r2, s0, s1, s2, u2 + 1, 1, nw, u.SWV, pre);
      if (sc.q >= u.U2) { sc.q = -1; }
      if (sc.q < 0) { sc = scanV(r0, r1, r2, s0, s1, s2, 0, 1, nw, u.SWV, pre); }
      if (sc.q < 0 || sc.q >= u.U2) { return f; }
      k = (wrapi(sc.q - u2, u.U2) - 1) >> 1;
    } else {
      sc = scanV(r0, r1, r2, s0, s1, s2, u2 - 1, -1, nw, u.SWV, pre);
      if (sc.q < 0) { sc = scanV(r0, r1, r2, s0, s1, s2, u.U2 - 1, -1, nw, u.SWV, pre); }
      if (sc.q < 0) { return f; }
      k = (wrapi(u2 - sc.q, u.U2) - 1) >> 1;
    }
    f.k = k;
    let xk = x + dx * k; let yk = y + dy * k; let o = (par + k) & 1;
    let x0 = xk - ((xk - o) & 1); let y0 = yk - ((yk - o) & 1);
    for (var c = 0; c < 4; c++) {
      let cx = x0 + (c & 1); let cy = y0 + (c >> 1);
      let cv = cx - cy;
      if (cv < u.lo || cv > u.hi) { continue; }
      let b = scanBit(&sc, cv - (x - y) + 1, wrapi(cx + cy, u.U2), nw, dx);
      var bit = 0u;
      if (b < 0) { bit = iswall(cx, cy); } else { bit = u32(b); }
      f.bits |= bit << u32(c);
    }
    return f;
  }
  var ua = u2 - 1; if (ua < 0) { ua = u.U2 - 1; }
  var ub = u2 + 1; if (ub == u.U2) { ub = 0; }
  let R = u.RWU; let RS = u.SWU;
  var ex = 0;
  if (dy < 0) {  // NE: v rises
    ex = (u.hi - (x - y)) >> 1;
    sc = scanU(ua * R, u2 * R, ub * R, ua * RS, u2 * RS, ub * RS, vv + 1, 1, R, RS, pre);
    if (sc.q >= 0) { k = (sc.q - vv - 1) >> 1; }
  } else {  // SW
    ex = ((x - y) - u.lo) >> 1;
    sc = scanU(ua * R, u2 * R, ub * R, ua * RS, u2 * RS, ub * RS, vv - 1, -1, R, RS, pre);
    if (sc.q >= 0) { k = (vv - sc.q - 1) >> 1; }
  }
  if (ex < k) { f.k = ex; f.bits = blockWalls(x, y, dx, dy, ex, par); return f; }  // leaves the band first (rare: at the edge)
  f.k = k;
  let xk = x + dx * k; let yk = y + dy * k; let o = (par + k) & 1;
  let x0 = xk - ((xk - o) & 1); let y0 = yk - ((yk - o) & 1);
  for (var c = 0; c < 4; c++) {
    let cx = x0 + (c & 1); let cy = y0 + (c >> 1);
    let cv = cx - cy;
    if (cv < u.lo || cv > u.hi) { continue; }
    let b = scanBit(&sc, wrapi(cx + cy, u.U2) - u2 + 1, cv - u.lo + PAD, R, -dy);
    var bit = 0u;
    if (b < 0) { bit = iswall(cx, cy); } else { bit = u32(b); }
    f.bits |= bit << u32(c);
  }
  return f;
}

fn freeSlot(s: u32) { let n = atomicAdd(&nfree, 1u); freeS[n] = s; }

fn vnow(s: u32, t: i32, par: i32) -> i32 { return px[s] - py[s]; }   // x - y of slot s (its position is kept at the current step)

// one slot's step: record it, apply the rule from table cur, write it into nxt, prefetch its next scan
fn stepSlot(s: u32, t: i32, par: i32, cur: u32, nxt: u32, rec: i32, pf: ptr<function, Scan>) {
  if (s >= SLOTS || wk[s] == DEAD) { return; }
  let x = px[s]; let y = py[s];
  let own = (x - y >= own_lo) && (x - y < own_hi);
  if (rec >= 0 && own) {
    let n = atomicAdd(&outb[OUT_COUNTS + u32(rec)], 1u);
    if (n < 4u * SLOTS) { atomicStore(&outb[OUT_CELLS + u32(rec) * 4u * SLOTS + n], (u32((x + y) >> 1) << 16u) | u32(x - y - u.lo)); }
  }
  let rx = (x - par) & 1; let ry = (y - par) & 1;
  let me = rx + 2 * ry;
  let x0 = x - rx; let y0 = y - ry;
  var walls = 0u;
  if (wk[s] == 0) { walls = wb[s]; }
  var f = 0u;
  for (var c = 0; c < 4; c++) {
    if (c == me) { f |= 1u << u32(c); continue; }
    if (((walls >> u32(c)) & 1u) == 1u) { f |= 1u << u32(c); continue; }
    var cx = x0 + (c & 1); var cy = y0 + (c >> 1);
    let s2 = cx + cy; if (s2 < 0) { cx += u.U; cy += u.U; } else if (s2 >= u.U2) { cx -= u.U; cy -= u.U; }
    if (cx - cy >= u.lo && cx - cy <= u.hi && hhas(cur, cellkey(cx, cy))) { f |= 1u << u32(c); }
  }
  let n = countOneBits(f);
  var to = me;
  if (n == 1u) { to = 3 - me; }
  else if (n == 2u && f == 9u) { to = select(2, 1, me == 0); }   // diagonal {0, 3} -> {1, 2}
  else if (n == 2u && f == 6u) { to = select(3, 0, me == 1); }   // {1, 2} -> {0, 3}
  if (own && n == 2u && (f == 9u || f == 6u) && (walls & f) != 0u) { atomicOr(&err, 1u); }  // a wall would move (trusted in the own range)
  var xn = x0 + (to & 1); var yn = y0 + (to >> 1);
  let s2 = xn + yn; if (s2 < 0) { xn += u.U; yn += u.U; } else if (s2 >= u.U2) { xn -= u.U; yn -= u.U; }
  if (xn - yn < u.lo || xn - yn > u.hi) { wk[s] = DEAD; freeSlot(s); return; }   // out of the band: a sink
  px[s] = xn; py[s] = yn;
  hinsert(nxt, cellkey(xn, yn));
  if (n == 1u) { wk[s] = wk[s] - 1; (*pf).i = -1; return; }
  wk[s] = -1;   // direction changed: scan again next step; fetch its words now
  let p1 = par ^ 1;
  let dx = 1 - 2 * ((xn - p1) & 1); let dy = 1 - 2 * ((yn - p1) & 1);
  *pf = prefetch(xn, yn, dx, dy);
}

fn rescan(s: u32, par: i32, pf: ptr<function, Scan>) {
  if (s >= SLOTS || wk[s] != -1) { return; }
  let x = px[s]; let y = py[s];
  let dx = 1 - 2 * ((x - par) & 1); let dy = 1 - 2 * ((y - par) & 1);
  let fr = front(x, y, dx, dy, par, pf);
  wk[s] = fr.k; wb[s] = fr.bits;
}

// first index in a time-sorted (t, ...) list with t >= tt
fn lowerBound(n: i32, tt: i32, isFeed: bool) -> i32 {
  var a = 0; var b = n;
  loop {
    if (a >= b) { break; }
    let m = (a + b) >> 1;
    var tm = 0;
    if (isFeed) { tm = sched[m].x; } else { tm = reads[m].x; }
    if (tm < tt) { a = m + 1; } else { b = m; }
  }
  return a;
}

@compute @workgroup_size(${WG})
fn main(@builtin(local_invocation_id) lid: vec3<u32>, @builtin(workgroup_id) wid: vec3<u32>) {
  let tid = lid.x;
  let T = u.T;
  if (i32(wid.x) >= T) {  // a spinner workgroup (diagnostic): busy work to keep the GPU's clocks up
    var acc = wid.x + lid.x;
    for (var i = 0; i < u.spin; i++) { acc = acc * 1664525u + 1013904223u; }
    if (acc == 7u) { atomicStore(&outb[8], acc); }
    return;
  }
  let k = i32(wid.x);
  let U2 = u.U2;
  let t0 = ep.t0; let t1 = ep.t1; let G = 2 * (t1 - t0) + 4;
  // 1. the list: segment offsets, every ball's x - y at t0
  for (var h = tid; h < 3u * HASH; h += ${WG}u) { atomicStore(&hk[h], EMPTY); }
  for (var h = tid; h < HBINS; h += ${WG}u) { atomicStore(&hist[h], 0u); }
  if (tid == 0u) {
    var acc = 0;
    for (var q = 0; q < T; q++) { segOff[q] = acc; acc += lst[ep.inBase + q]; }
    segOff[T] = acc; nTot = acc;
    atomicStore(&nslots, 0u); atomicStore(&nfree, 0u); atomicStore(&nkept, 0u); atomicStore(&err, 0u);
  }
  let N = workgroupUniformLoad(&nTot);   // (a barrier too) uniform, so the early return below keeps the barriers uniform
  if (N > i32(LMAX)) { if (tid == 0u) { atomicOr(&outb[0], 8u); } return; }
  var q = 0;
  for (var idx = i32(tid); idx < N; idx += ${WG}) {
    loop { if (idx < segOff[q + 1]) { break; } q++; }   // segments are in order, idx grows
    let rec = ep.inBase + T + 4 * (q * i32(SLOTS) + idx - segOff[q]);
    let v = lst[rec] - lst[rec + 1];
    lv[idx] = v;
    atomicAdd(&hist[u32((v - u.lo) / HB)], 1u);
  }
  workgroupBarrier();
  // 2. cuts of (nearly) equal counts from the histogram's prefix sum; my range and window
  if (tid == 0u) {
    var acc = 0u;
    for (var b = 0u; b < HBINS; b++) { acc += atomicLoad(&hist[b]); atomicStore(&hist[b], acc); }
  }
  workgroupBarrier();
  if (tid == 0u) {
    var lo = u.lo; var hi = u.hi + 1;
    if (N > 0) {   // cut k: the first bin boundary with at least k * N / T balls below it
      var kk = k; var b = 0u;
      if (k > 0) { loop { if (b >= HBINS || atomicLoad(&hist[b]) * u32(T) >= u32(k * N)) { break; } b++; } lo = u.lo + i32(b + 1u) * HB; }
      if (k < T - 1) { loop { if (b >= HBINS || atomicLoad(&hist[b]) * u32(T) >= u32((k + 1) * N)) { break; } b++; } hi = u.lo + i32(b + 1u) * HB; }
      lo = min(lo, u.hi + 1); hi = min(hi, u.hi + 1);
    } else if (k > 0) { lo = u.hi + 1; }   // an empty machine: tile 0 owns everything
    own_lo = lo; own_hi = hi;
    win_lo = max(u.lo, lo - G); win_hi = min(u.hi, hi - 1 + G);
    fi = lowerBound(u.nfeeds, t0, true); ri = lowerBound(u.nreads, t0, false);
    nextF = INF; if (fi < u.nfeeds) { nextF = sched[fi].x; }
    nextR = INF; if (ri < u.nreads) { nextR = reads[ri].x; }
  }
  workgroupBarrier();
  // 3. my window's balls into slots, their cells into table t0 % 3
  let olo = own_lo; let ohi = own_hi; let wlo = win_lo; let whi = win_hi;
  q = 0;
  for (var idx = i32(tid); idx < N; idx += ${WG}) {
    let v = lv[idx];
    loop { if (idx < segOff[q + 1]) { break; } q++; }
    if (v < wlo || v > whi) { continue; }
    let s = atomicAdd(&nslots, 1u);
    if (s >= SLOTS) { atomicOr(&err, 2u); continue; }
    let rec = ep.inBase + T + 4 * (q * i32(SLOTS) + idx - segOff[q]);
    px[s] = lst[rec]; py[s] = lst[rec + 1]; wk[s] = lst[rec + 2]; wb[s] = u32(lst[rec + 3]);
  }
  workgroupBarrier();
  let ns = atomicLoad(&nslots);
  let cur0 = u32(t0 % 3) * HASH;
  for (var j = 0u; j < ${SPT}u; j++) {
    let s = tid + ${WG}u * j;
    if (s >= ns) { wk[s] = DEAD; freeSlot(s); } else { hinsert(cur0, cellkey(px[s], py[s])); }
  }
  workgroupBarrier();
  // 4. the epoch
  var t = t0;
  var pfs: array<Scan, ${SPT}>;
  for (var j = 0; j < ${SPT}; j++) { pfs[j].i = -1; }
  loop {
    if (t >= t1) { break; }
    let par = (u.par0 + t) & 1;
    let cur = u32(t % 3) * HASH; let nxt = u32((t + 1) % 3) * HASH; let clr = u32((t + 2) % 3) * HASH;
    // A. clear the table for step t + 2; feeds and reads (one thread); rescans of balls that turned
    for (var h = tid; h < HASH; h += ${WG}u) { atomicStore(&hk[clr + h], EMPTY); }
    if (tid == 0u) {
      loop {
        if (nextF != t) { break; }
        let f = sched[fi]; fi++;
        nextF = INF; if (fi < u.nfeeds) { nextF = sched[fi].x; }
        let g = f.w;
        if ((g == 1 && u.left == 0) || (g == 2 && u.right == 0) || (g == 3 && u.reset == 0)) { continue; }
        var x = f.y; var y = f.z;
        let v = x - y;
        if (v < wlo || v > whi) { continue; }   // not in my window
        var s2 = x + y; loop { if (s2 >= 0) { break; } x += u.U; y += u.U; s2 += U2; }
        loop { if (s2 < U2) { break; } x -= u.U; y -= u.U; s2 -= U2; }
        if (iswall(x, y) == 1u) { continue; }
        let kk = cellkey(x, y);
        if (hhas(cur, kk)) { continue; }
        let n = atomicLoad(&nfree);
        if (n == 0u) { atomicOr(&err, 2u); continue; }
        atomicStore(&nfree, n - 1u);
        let s = freeS[n - 1u];
        px[s] = x; py[s] = y;
        let dx = 1 - 2 * ((x - par) & 1); let dy = 1 - 2 * ((y - par) & 1);
        var none: Scan; none.i = -1;
        let fr = front(x, y, dx, dy, par, &none);
        wb[s] = fr.bits; wk[s] = fr.k;   // (wk last: its owner may be looking)
        hinsert(cur, kk);
      }
      loop {
        if (nextR != t) { break; }
        let r = reads[ri]; ri++;
        nextR = INF; if (ri < u.nreads) { nextR = reads[ri].x; }
        var x = r.y; var y = r.z;
        let v = x - y;
        if (v < olo || v >= ohi) { continue; }   // another tile's cell
        var s2 = x + y; loop { if (s2 >= 0) { break; } x += u.U; y += u.U; s2 += U2; }
        loop { if (s2 < U2) { break; } x -= u.U; y -= u.U; s2 -= U2; }
        var bit = 0u;
        if (hhas(cur, cellkey(x, y))) { bit = 1u; }
        atomicStore(&outb[OUT_READS + u32(r.w)], bit);
      }
    }
    for (var j = 0; j < ${SPT}; j++) { rescan(tid + ${WG}u * u32(j), par, &pfs[j]); }
    workgroupBarrier();
    // B. the rule, every ball at once, from table cur into table nxt
    var rec = -1;
    for (var j = 0; j < u.npics; j++) {
      let TT = u.picT[j]; let K = u.picK[j];
      if (t > TT - K && t <= TT) { rec = u.picOff[j] + t - (TT - K + 1); }
    }
    for (var j = 0; j < ${SPT}; j++) { stepSlot(tid + ${WG}u * u32(j), t, par, cur, nxt, rec, &pfs[j]); }
    t++;
    workgroupBarrier();
  }
  // 5. the balls that ended in my range, for the next epoch
  for (var j = 0u; j < ${SPT}u; j++) {
    let s = tid + ${WG}u * j;
    if (s >= SLOTS || wk[s] == DEAD) { continue; }
    let v = px[s] - py[s];
    if (v < olo || v >= ohi) { continue; }
    let n = atomicAdd(&nkept, 1u);
    let rec = ep.outBase + T + 4 * (k * i32(SLOTS) + i32(n));
    lst[rec] = px[s]; lst[rec + 1] = py[s]; lst[rec + 2] = wk[s]; lst[rec + 3] = i32(wb[s]);
  }
  workgroupBarrier();
  if (tid == 0u) {
    lst[ep.outBase + k] = i32(atomicLoad(&nkept));
    let e = atomicLoad(&err);
    if (e != 0u) { atomicOr(&outb[0], e); }
  }
}
`;

class GpuMachine {
  // meta: web.py machine_info; squares: its wall squares. opts: tiles (default 16), epoch (steps, default 64), spin. Call init() (async) before frame().
  constructor(meta, squares, opts = {}) {
    this.meta = meta; this.squares = squares; this.P = meta.P; this.n = 0;
    this.spin = opts.spin | 0; this.T = Math.min(64, Math.max(1, opts.tiles | 0 || 16)); this.D = Math.max(8, opts.epoch | 0 || 64);
  }

  async init() {
    if (typeof navigator === 'undefined' || !navigator.gpu) throw new Error('no WebGPU');
    const adapter = await navigator.gpu.requestAdapter({ powerPreference: 'high-performance' }) || await navigator.gpu.requestAdapter();
    if (!adapter) throw new Error('no WebGPU adapter');
    const m = this.meta, U2 = m.U2, lo = m.lo, hi = m.hi, V2 = hi - lo + 1, S = V2 + 2 * PADG, T = this.T;
    const RWU = (S >> 5) + 2, RWV = (U2 >> 5) + 2, nU = U2 * RWU * 4, nV = S * RWV * 4;
    const need = Math.max(nU, nV);
    if (adapter.limits.maxStorageBufferBindingSize < need || adapter.limits.maxBufferSize < need) throw new Error(`walls need ${(need >> 20)} MB buffers`);
    const required = {};
    for (const k of ['maxStorageBufferBindingSize', 'maxBufferSize']) if (adapter.limits[k] > 134217728) required[k] = adapter.limits[k];
    const features = [];
    if (adapter.features.has('timestamp-query')) features.push('timestamp-query');
    this.device = await adapter.requestDevice({ requiredLimits: required, requiredFeatures: features });
    const d = this.device;
    this.timed = features.length > 0;
    this.info = (adapter.info ? `${adapter.info.vendor || ''} ${adapter.info.architecture || ''}`.trim() : 'gpu') + ` · ${T} tiles × ${this.D} steps`;
    // walls straight into the GPU buffers, with their summaries
    const bu = d.createBuffer({ size: nU, usage: GPUBufferUsage.STORAGE, mappedAtCreation: true });
    const bv = d.createBuffer({ size: nV, usage: GPUBufferUsage.STORAGE, mappedAtCreation: true });
    const wu = new Int32Array(bu.getMappedRange()), wv = new Int32Array(bv.getMappedRange()), sq = this.squares;
    for (let j = 0, uu = 0; j < sq.length; j += 2) {
      uu += sq[j];
      const vv = sq[j + 1] + PADG, a = uu - 1;
      for (const [du, dv] of [[0, 0], [1, 1], [1, -1], [2, 0]]) {
        const u2 = (a + du + U2) % U2, v = vv + dv;
        wu[u2 * RWU + (v >> 5)] |= 1 << (v & 31); wv[v * RWV + (u2 >> 5)] |= 1 << (u2 & 31);
      }
    }
    const SWU = (RWU + 31) >> 5, SWV = (RWV + 31) >> 5;   // one bit per word of a row
    const SVOFF = U2 * SWU;
    const bsm = d.createBuffer({ size: (SVOFF + S * SWV) * 4, usage: GPUBufferUsage.STORAGE, mappedAtCreation: true });
    const smm = new Int32Array(bsm.getMappedRange());
    for (let r = 0; r < U2; r++) for (let w = 0; w < RWU; w++) if (wu[r * RWU + w]) smm[r * SWU + (w >> 5)] |= 1 << (w & 31);
    for (let r = 0; r < S; r++) for (let w = 0; w < RWV; w++) if (wv[r * RWV + w]) smm[SVOFF + r * SWV + (w >> 5)] |= 1 << (w & 31);
    bu.unmap(); bv.unmap(); bsm.unmap();
    const sched = m.sched;  // sorted by t
    const fb = d.createBuffer({ size: Math.max(16, 16 * sched.length), usage: GPUBufferUsage.STORAGE, mappedAtCreation: true });
    new Int32Array(fb.getMappedRange()).set(sched.flat()); fb.unmap();
    const rs = [];
    for (const [name, [t, cells]] of Object.entries(m.reads)) for (const [x, y] of cells) rs.push([t, x, y, name]);
    rs.sort((a, b) => a[0] - b[0]);
    const rb = d.createBuffer({ size: Math.max(16, 16 * rs.length), usage: GPUBufferUsage.STORAGE, mappedAtCreation: true });
    new Int32Array(rb.getMappedRange()).set(rs.flatMap((e, i) => [e[0], e[1], e[2], i])); rb.unmap();
    this.readNames = rs.map(e => e[3]);
    this.nreads = rs.length;
    // the ball lists: two halves, each T counts then T * SLOTS records; both start empty
    this.half = T + 4 * T * SLOTS;
    this.lst = d.createBuffer({ size: 4 * 2 * this.half, usage: GPUBufferUsage.STORAGE | GPUBufferUsage.COPY_SRC });
    this.outSize = 4 * (2048 + NREC * 4 * SLOTS);
    this.outb = d.createBuffer({ size: this.outSize, usage: GPUBufferUsage.STORAGE | GPUBufferUsage.COPY_SRC | GPUBufferUsage.COPY_DST });
    this.rbuf = d.createBuffer({ size: this.outSize + 4 * T + 16, usage: GPUBufferUsage.COPY_DST | GPUBufferUsage.MAP_READ });
    this.ub = d.createBuffer({ size: 128, usage: GPUBufferUsage.UNIFORM | GPUBufferUsage.COPY_DST });
    this.params = new Int32Array(32);
    this.params.set([U2 / 2, U2, lo, hi, S, RWU, RWV, m.P, m.t_lo & 1, sched.length, rs.length, 0, 0, 0, 0, T]);
    this.params[28] = SWU; this.params[29] = SWV; this.params[30] = this.spin ? 4000000 : 0;
    // one Epoch uniform per dispatch, at the dynamic-offset alignment; the input half alternates
    this.E = Math.ceil(this.P / this.D);
    const align = Math.max(256, d.limits.minUniformBufferOffsetAlignment);
    this.align = align;
    this.epb = [0, 1].map(() => d.createBuffer({ size: align * this.E, usage: GPUBufferUsage.UNIFORM | GPUBufferUsage.COPY_DST }));
    for (const first of [0, 1]) {
      const a = new Int32Array(align / 4 * this.E);
      for (let e = 0; e < this.E; e++) {
        const side = (first + e) & 1;
        a.set([e * this.D, Math.min(this.P, (e + 1) * this.D), side * this.half, (1 - side) * this.half], e * align / 4);
      }
      d.queue.writeBuffer(this.epb[first], 0, a);
    }
    this.side = 0;  // which half the next frame's first epoch reads
    if (this.timed) {
      this.qs = d.createQuerySet({ type: 'timestamp', count: 2 });
      this.qb = d.createBuffer({ size: 16, usage: GPUBufferUsage.QUERY_RESOLVE | GPUBufferUsage.COPY_SRC });
    }
    const HBINS = Math.ceil((hi - lo + 1) / 32) + 1;
    const mod = d.createShaderModule({ code: wgsl(SVOFF, HBINS) });
    const ci = await mod.getCompilationInfo();
    const bad = ci.messages.filter(x => x.type === 'error');
    if (bad.length) throw new Error('shader: ' + bad.map(x => `${x.lineNum}:${x.linePos} ${x.message}`).join('; '));
    const C = GPUShaderStage.COMPUTE, ro = { type: 'read-only-storage' }, rw = { type: 'storage' };
    const bgl = d.createBindGroupLayout({ entries: [
      { binding: 0, visibility: C, buffer: { type: 'uniform' } }, { binding: 1, visibility: C, buffer: ro }, { binding: 2, visibility: C, buffer: ro },
      { binding: 3, visibility: C, buffer: ro }, { binding: 4, visibility: C, buffer: ro }, { binding: 5, visibility: C, buffer: rw },
      { binding: 6, visibility: C, buffer: rw }, { binding: 7, visibility: C, buffer: ro },
      { binding: 8, visibility: C, buffer: { type: 'uniform', hasDynamicOffset: true } }] });
    try {
      this.pipe = await d.createComputePipelineAsync({ layout: d.createPipelineLayout({ bindGroupLayouts: [bgl] }), compute: { module: mod, entryPoint: 'main' } });
    } catch (e) { throw new Error('pipeline: ' + (e.message || e)); }
    this.bg = [0, 1].map(first => d.createBindGroup({ layout: bgl, entries: [
      { binding: 0, resource: { buffer: this.ub } }, { binding: 1, resource: { buffer: bu } }, { binding: 2, resource: { buffer: bv } },
      { binding: 3, resource: { buffer: fb } }, { binding: 4, resource: { buffer: rb } }, { binding: 5, resource: { buffer: this.lst } },
      { binding: 6, resource: { buffer: this.outb } }, { binding: 7, resource: { buffer: bsm } },
      { binding: 8, resource: { buffer: this.epb[first], size: 16 } }] }));
    this.zeros = new Uint32Array(NREC + 16);
    return this;
  }

  // Same contract as engine.js Machine.frame, but async; also returns gpuMs when timestamps are available.
  async frame(inp, traces = []) {
    const d = this.device, p = this.params, m = this.meta, T = this.T;
    if (traces.length > 4) throw new Error('at most 4 pictures per frame');
    p[12] = inp.left ? 1 : 0; p[13] = inp.right ? 1 : 0; p[14] = inp.reset ? 1 : 0; p[11] = traces.length;
    let off = 0;
    traces.forEach(([TT, K], j) => { p[16 + j] = TT; p[20 + j] = K; p[24 + j] = off; off += K; });
    if (off > NREC) throw new Error(`at most ${NREC} recorded steps per frame`);
    d.queue.writeBuffer(this.ub, 0, p);
    d.queue.writeBuffer(this.outb, 0, this.zeros, 0, 16);            // errors
    d.queue.writeBuffer(this.outb, 4 * 1024, this.zeros, 0, NREC);   // picture counts
    const side = this.side, enc = d.createCommandEncoder();
    const pass = enc.beginComputePass(this.timed ? { timestampWrites: { querySet: this.qs, beginningOfPassWriteIndex: 0, endOfPassWriteIndex: 1 } } : {});
    pass.setPipeline(this.pipe);
    for (let e = 0; e < this.E; e++) { pass.setBindGroup(0, this.bg[side], [e * this.align]); pass.dispatchWorkgroups(T + this.spin); }
    pass.end();
    const outSide = (side + this.E) & 1;
    this.side = outSide;
    enc.copyBufferToBuffer(this.outb, 0, this.rbuf, 0, this.outSize);
    enc.copyBufferToBuffer(this.lst, 4 * outSide * this.half, this.rbuf, this.outSize, 4 * T);
    if (this.timed) { enc.resolveQuerySet(this.qs, 0, 2, this.qb, 0); enc.copyBufferToBuffer(this.qb, 0, this.rbuf, this.outSize + 4 * T, 16); }
    d.queue.submit([enc.finish()]);
    await this.rbuf.mapAsync(GPUMapMode.READ);
    const all = this.rbuf.getMappedRange().slice(0);
    this.rbuf.unmap();
    const o = new Uint32Array(all, 0, this.outSize / 4), kc = new Int32Array(all, this.outSize, T);
    let gpuMs = null;
    if (this.timed) { const q = new BigUint64Array(all, this.outSize + 4 * T, 2); gpuMs = Number(q[1] - q[0]) / 1e6; }
    if (o[0] & 1) throw new Error('a wall moved: the machine is broken');
    if (o[0] & 2) throw new Error(`more than ${SLOTS} balls in a tile's window`);
    if (o[0] & 4) throw new Error('cell hash full');
    if (o[0] & 8) throw new Error(`more than ${LMAX} balls`);
    const counts = {};
    for (let i = 0; i < this.nreads; i++) counts[this.readNames[i]] = (counts[this.readNames[i]] || 0) + o[16 + i];
    const bits = {};
    for (const [name, c] of Object.entries(counts)) {
      if (c !== 0 && c !== 2) throw new Error(`half a ball at output ${name}, frame ${this.n}`);
      bits[name] = c >> 1;
    }
    const state = {};
    for (const [name, width] of m.fields) {
      const key = name === 'bricks' ? 'brick' : name;
      let v = 0;
      for (let i = 0; i < width; i++) v += bits[key + i] * 2 ** i;
      state[name] = v;
    }
    const pics = traces.map(([TT, K], j) => {
      const cnts = [], raw = [];
      for (let s = 0; s < K; s++) {
        const r = p[24 + j] + s, c = Math.min(o[1024 + r], 4 * SLOTS);
        cnts.push(c);
        for (let i = 0; i < c; i++) { const w = o[2048 + r * 4 * SLOTS + i]; raw.push(w >>> 16, w & 0xffff); }
      }
      return { ph: TT, counts: cnts, raw: Uint16Array.from(raw) };
    });
    let alive = 0;
    for (let k = 0; k < T; k++) alive += kc[k];
    this.n++;
    return { state, win: bits.win, lose: bits.lose, pics, alive, gpuMs };
  }
}

if (typeof module !== 'undefined') module.exports = { GpuMachine, wgsl };
if (typeof self !== 'undefined') self.GpuMachine = GpuMachine;
