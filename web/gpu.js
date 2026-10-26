// The billiard-ball machine on the GPU (WebGPU), for the static site. Same
// hardware data and the same results as web/engine.js (which it falls back to),
// checked by tests/test_gpu.py.
//
// One compute workgroup of 256 threads runs the whole machine: every moving
// ball is a slot (two per thread), and a step is the Margolus rule applied to
// each ball's 2x2 block, all balls at once. The balls' cells are kept in a hash
// set in workgroup memory (rebuilt every step), so a step is three barriers and
// a few on-chip lookups; the mirrors are two read-only bitmaps in GPU memory
// (rows of constant x+y and of constant x-y) that a ball reads only when its
// direction changes, to find the step at which its front reaches a wall and the
// wall cells of that block. Feeds, reads and pictures are served inside the
// shader at their steps; a frame is a handful of dispatches of K steps each in
// one command buffer, then one readback.
'use strict';
const SLOTS = 512, STEPS_PER_DISPATCH = 2048, PADG = 2;

const WGSL = /* wgsl */`
struct Params {
  U: i32, U2: i32, lo: i32, hi: i32, S: i32, RWU: i32, RWV: i32, P: i32,
  par0: i32, nfeeds: i32, nreads: i32, npics: i32, left: i32, right: i32, reset: i32, K: i32,
  picT: vec4<i32>, picK: vec4<i32>, picOff: vec4<i32>,
  SWU: i32, SWV: i32, pad0: i32, pad1: i32,
};
@group(0) @binding(0) var<uniform> u: Params;
@group(0) @binding(1) var<storage, read> wu: array<u32>;         // walls by row x+y, bit x-y-lo+PAD
@group(0) @binding(2) var<storage, read> wv: array<u32>;         // walls by row x-y-lo+PAD, bit x+y
@group(0) @binding(3) var<storage, read> sched: array<vec4<i32>>; // feeds: t, x, y, gate (0 always, 1 left, 2 right, 3 reset)
@group(0) @binding(4) var<storage, read> reads: array<vec4<i32>>; // t, x, y, output index
@group(0) @binding(5) var<storage, read_write> st: array<i32>;    // persistent: per slot x, y, wallk, wallbits; then cur_t, fi, ri
@group(0) @binding(6) var<storage, read_write> outb: array<u32>;  // [0] errors [1] cur_t [2] alive; [16..] read bits; [1024..] pic counts; [2048..] pic cells
@group(0) @binding(7) var<storage, read> su: array<u32>;         // summary of wu: bit w of row u2 = word w of that row is not zero (SWU words a row)
@group(0) @binding(8) var<storage, read> sv: array<u32>;         // summary of wv likewise (SWV words a row)

const SLOTS = ${SLOTS}u;
const HASH = 1024u;
const EMPTY = 0xffffffffu;
const DEAD = -2147483647 - 1;
const INF = 0x3fffffff;
const PAD = ${PADG};
const OUT_READS = 16u;
const OUT_COUNTS = 1024u;
const OUT_CELLS = 2048u;

var<workgroup> px: array<i32, SLOTS>;
var<workgroup> py: array<i32, SLOTS>;
var<workgroup> wk: array<i32, SLOTS>;   // steps until the block in front holds a wall (-1: unknown), DEAD if free
var<workgroup> wb: array<u32, SLOTS>;   // the wall cells of that block, bit c = corner c
var<workgroup> hk: array<atomic<u32>, HASH>;
var<workgroup> freeS: array<u32, SLOTS>;
var<workgroup> nfree: atomic<u32>;
var<workgroup> cnt: atomic<u32>;
var<workgroup> err: atomic<u32>;
var<workgroup> fi: i32;
var<workgroup> ri: i32;
var<workgroup> nextF: i32;   // time of feed fi (INF past the end): no polling of global memory every step
var<workgroup> nextR: i32;
var<workgroup> t_wg: i32;

fn wrapi(a: i32, m: i32) -> i32 { var r = a; loop { if (r >= 0) { break; } r += m; } loop { if (r < m) { break; } r -= m; } return r; }

fn cellkey(x: i32, y: i32) -> u32 {  // x + y canonical
  return u32((x + y) * u.S + (x - y - u.lo + PAD));
}

fn hhash(k: u32) -> u32 { return (k * 2654435761u) >> 22u; }

fn hinsert(k: u32) {
  var h = hhash(k);
  for (var i = 0u; i < HASH; i++) {
    let r = atomicCompareExchangeWeak(&hk[h], EMPTY, k);
    if (r.exchanged || r.old_value == k) { return; }
    h = (h + 1u) & (HASH - 1u);
  }
  atomicOr(&err, 4u);
}

fn hhas(k: u32) -> bool {
  var h = hhash(k);
  for (var i = 0u; i < HASH; i++) {
    let v = atomicLoad(&hk[h]);
    if (v == k) { return true; }
    if (v == EMPTY) { return false; }
    h = (h + 1u) & (HASH - 1u);
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

// A scan along three rows of a bitmap, from bit p in direction dir (+1/-1):
// the first set bit q, and the words around it (rows x {i-1, i, i+1}) so the
// wall cells of the block the ball will then be in come without another
// round trip to memory. Words are fetched in pairs as the scan goes, so the
// whole scan is one memory latency per word visited.
struct Scan { q: i32, i: i32, w: array<u32, 9> }  // w[3r + 0..2] = row r at words i-1, i, i+1

// The scan: the word holding p (masked), then the summaries to find the next
// word with any bit in any of the three rows (a dependent load per summary word,
// each covering 32 words), then that word. A ball whose column is empty for
// thousands of cells (a falling messenger) thus costs ~3 memory latencies, not
// hundreds. rs0..2: row offsets in the summary; nws: summary words a row.
fn scanV(r0: i32, r1: i32, r2: i32, rs0: i32, rs1: i32, rs2: i32, p: i32, dir: i32, nw: i32, nws: i32) -> Scan {
  var sc: Scan;
  sc.q = -1;
  var i = p >> 5;
  if (i < 0 || i >= nw) { return sc; }
  var ia = i - dir; if (ia < 0) { ia += nw; } if (ia >= nw) { ia -= nw; }
  var ib = i + dir; if (ib < 0) { ib += nw; } if (ib >= nw) { ib -= nw; }
  // the word holding p and its neighbours, in one round trip (the common case ends here)
  var a0 = wv[u32(r0 + ia)]; var a1 = wv[u32(r1 + ia)]; var a2 = wv[u32(r2 + ia)];
  var b0 = wv[u32(r0 + i)]; var b1 = wv[u32(r1 + i)]; var b2 = wv[u32(r2 + i)];
  var c0 = wv[u32(r0 + ib)]; var c1 = wv[u32(r1 + ib)]; var c2 = wv[u32(r2 + ib)];
  var m = b0 | b1 | b2;
  if (dir > 0) { m &= 0xffffffffu << u32(p & 31); } else { m &= 0xffffffffu >> u32(31 - (p & 31)); }
  if (m == 0u) {  // the summaries say which word next holds anything in any of the three rows
    var i2 = i + dir;
    if (i2 < 0 || i2 >= nw) { return sc; }
    var j = i2 >> 5;
    var sm = sv[u32(rs0 + j)] | sv[u32(rs1 + j)] | sv[u32(rs2 + j)];
    if (dir > 0) { sm &= 0xffffffffu << u32(i2 & 31); } else { sm &= 0xffffffffu >> u32(31 - (i2 & 31)); }
    loop {
      if (sm != 0u) { break; }
      j += dir;
      if (j < 0 || j >= nws) { return sc; }
      sm = sv[u32(rs0 + j)] | sv[u32(rs1 + j)] | sv[u32(rs2 + j)];
    }
    if (dir > 0) { i = j * 32 + i32(ctz(sm)); } else { i = j * 32 + 31 - i32(countLeadingZeros(sm)); }
    if (i >= nw) { return sc; }
  ia = i - dir; if (ia < 0) { ia += nw; } if (ia >= nw) { ia -= nw; }
  ib = i + dir; if (ib < 0) { ib += nw; } if (ib >= nw) { ib -= nw; }
    a0 = wv[u32(r0 + ia)]; a1 = wv[u32(r1 + ia)]; a2 = wv[u32(r2 + ia)];
    b0 = wv[u32(r0 + i)]; b1 = wv[u32(r1 + i)]; b2 = wv[u32(r2 + i)];
    c0 = wv[u32(r0 + ib)]; c1 = wv[u32(r1 + ib)]; c2 = wv[u32(r2 + ib)];
    m = b0 | b1 | b2;
  }
  sc.i = i;
  if (dir > 0) { sc.q = i * 32 + i32(ctz(m)); } else { sc.q = i * 32 + 31 - i32(countLeadingZeros(m)); }
  if (dir > 0) { sc.w[0] = a0; sc.w[1] = b0; sc.w[2] = c0; sc.w[3] = a1; sc.w[4] = b1; sc.w[5] = c1; sc.w[6] = a2; sc.w[7] = b2; sc.w[8] = c2; }
  else { sc.w[0] = c0; sc.w[1] = b0; sc.w[2] = a0; sc.w[3] = c1; sc.w[4] = b1; sc.w[5] = a1; sc.w[6] = c2; sc.w[7] = b2; sc.w[8] = a2; }
  return sc;
}

fn scanU(r0: i32, r1: i32, r2: i32, rs0: i32, rs1: i32, rs2: i32, p: i32, dir: i32, nw: i32, nws: i32) -> Scan {
  var sc: Scan;
  sc.q = -1;
  var i = p >> 5;
  if (i < 0 || i >= nw) { return sc; }
  var ia = clamp(i - dir, 0, nw - 1);
  var ib = clamp(i + dir, 0, nw - 1);
  // the word holding p and its neighbours, in one round trip (the common case ends here)
  var a0 = wu[u32(r0 + ia)]; var a1 = wu[u32(r1 + ia)]; var a2 = wu[u32(r2 + ia)];
  var b0 = wu[u32(r0 + i)]; var b1 = wu[u32(r1 + i)]; var b2 = wu[u32(r2 + i)];
  var c0 = wu[u32(r0 + ib)]; var c1 = wu[u32(r1 + ib)]; var c2 = wu[u32(r2 + ib)];
  var m = b0 | b1 | b2;
  if (dir > 0) { m &= 0xffffffffu << u32(p & 31); } else { m &= 0xffffffffu >> u32(31 - (p & 31)); }
  if (m == 0u) {  // the summaries say which word next holds anything in any of the three rows
    var i2 = i + dir;
    if (i2 < 0 || i2 >= nw) { return sc; }
    var j = i2 >> 5;
    var sm = su[u32(rs0 + j)] | su[u32(rs1 + j)] | su[u32(rs2 + j)];
    if (dir > 0) { sm &= 0xffffffffu << u32(i2 & 31); } else { sm &= 0xffffffffu >> u32(31 - (i2 & 31)); }
    loop {
      if (sm != 0u) { break; }
      j += dir;
      if (j < 0 || j >= nws) { return sc; }
      sm = su[u32(rs0 + j)] | su[u32(rs1 + j)] | su[u32(rs2 + j)];
    }
    if (dir > 0) { i = j * 32 + i32(ctz(sm)); } else { i = j * 32 + 31 - i32(countLeadingZeros(sm)); }
    if (i >= nw) { return sc; }
  ia = clamp(i - dir, 0, nw - 1);
  ib = clamp(i + dir, 0, nw - 1);
    a0 = wu[u32(r0 + ia)]; a1 = wu[u32(r1 + ia)]; a2 = wu[u32(r2 + ia)];
    b0 = wu[u32(r0 + i)]; b1 = wu[u32(r1 + i)]; b2 = wu[u32(r2 + i)];
    c0 = wu[u32(r0 + ib)]; c1 = wu[u32(r1 + ib)]; c2 = wu[u32(r2 + ib)];
    m = b0 | b1 | b2;
  }
  sc.i = i;
  if (dir > 0) { sc.q = i * 32 + i32(ctz(m)); } else { sc.q = i * 32 + 31 - i32(countLeadingZeros(m)); }
  if (dir > 0) { sc.w[0] = a0; sc.w[1] = b0; sc.w[2] = c0; sc.w[3] = a1; sc.w[4] = b1; sc.w[5] = c1; sc.w[6] = a2; sc.w[7] = b2; sc.w[8] = c2; }
  else { sc.w[0] = c0; sc.w[1] = b0; sc.w[2] = a0; sc.w[3] = c1; sc.w[4] = b1; sc.w[5] = a1; sc.w[6] = c2; sc.w[7] = b2; sc.w[8] = a2; }
  return sc;
}

// bit pos of row r (0..2) of a scan, if it lies in the words fetched, else -1
fn scanBit(sc: ptr<function, Scan>, r: i32, pos: i32, nw: i32) -> i32 {
  var wi = pos >> 5;
  let d = wi - (*sc).i;
  var k = -1;
  if (d == 0) { k = 1; } else if (d == 1 || d == 1 - nw) { k = 2; } else if (d == -1 || d == nw - 1) { k = 0; }
  if (k < 0) { return -1; }
  return i32(((*sc).w[3 * r + k] >> u32(pos & 31)) & 1u);
}

struct Front { k: i32, bits: u32 }

// the wall cells (bit c = corner c) of the block the ball is in after k lone steps
fn blockWalls(x: i32, y: i32, dx: i32, dy: i32, k: i32, par: i32) -> u32 {
  let xk = x + dx * k; let yk = y + dy * k;
  let o = (par + k) & 1;
  let x0 = xk - ((xk - o) & 1); let y0 = yk - ((yk - o) & 1);
  return iswall(x0, y0) | (iswall(x0 + 1, y0) << 1u) | (iswall(x0, y0 + 1) << 2u) | (iswall(x0 + 1, y0 + 1) << 3u);
}

// steps until the block in front of a ball at (x, y) moving (dx, dy) holds a
// wall (or it leaves the band), with that block's wall cells
fn front(x: i32, y: i32, dx: i32, dy: i32, par: i32) -> Front {
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
      sc = scanV(r0, r1, r2, s0, s1, s2, u2 + 1, 1, nw, u.SWV);
      if (sc.q >= u.U2) { sc.q = -1; }
      if (sc.q < 0) { sc = scanV(r0, r1, r2, s0, s1, s2, 0, 1, nw, u.SWV); }
      if (sc.q < 0 || sc.q >= u.U2) { return f; }
      k = (wrapi(sc.q - u2, u.U2) - 1) >> 1;
    } else {
      sc = scanV(r0, r1, r2, s0, s1, s2, u2 - 1, -1, nw, u.SWV);
      if (sc.q < 0) { sc = scanV(r0, r1, r2, s0, s1, s2, u.U2 - 1, -1, nw, u.SWV); }
      if (sc.q < 0) { return f; }
      k = (wrapi(u2 - sc.q, u.U2) - 1) >> 1;
    }
    f.k = k;
    // the block's cells lie in rows vv-1..vv+1 at u2 positions within 2 of the hit
    let xk = x + dx * k; let yk = y + dy * k; let o = (par + k) & 1;
    let x0 = xk - ((xk - o) & 1); let y0 = yk - ((yk - o) & 1);
    for (var c = 0; c < 4; c++) {
      let cx = x0 + (c & 1); let cy = y0 + (c >> 1);
      let cv = cx - cy;
      if (cv < u.lo || cv > u.hi) { continue; }
      let b = scanBit(&sc, cv - (x - y) + 1, wrapi(cx + cy, u.U2), nw);
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
    sc = scanU(ua * R, u2 * R, ub * R, ua * RS, u2 * RS, ub * RS, vv + 1, 1, R, RS);
    if (sc.q >= 0) { k = (sc.q - vv - 1) >> 1; }
  } else {  // SW
    ex = ((x - y) - u.lo) >> 1;
    sc = scanU(ua * R, u2 * R, ub * R, ua * RS, u2 * RS, ub * RS, vv - 1, -1, R, RS);
    if (sc.q >= 0) { k = (vv - sc.q - 1) >> 1; }
  }
  if (ex < k) { f.k = ex; f.bits = blockWalls(x, y, dx, dy, ex, par); return f; }  // leaves the band first (rare: at the edge)
  f.k = k;
  // the block's cells lie in rows u2-1..u2+1 at vv positions within 2 of the hit
  let xk = x + dx * k; let yk = y + dy * k; let o = (par + k) & 1;
  let x0 = xk - ((xk - o) & 1); let y0 = yk - ((yk - o) & 1);
  for (var c = 0; c < 4; c++) {
    let cx = x0 + (c & 1); let cy = y0 + (c >> 1);
    let cv = cx - cy;
    if (cv < u.lo || cv > u.hi) { continue; }
    let b = scanBit(&sc, wrapi(cx + cy, u.U2) - u2 + 1, cv - u.lo + PAD, R);
    var bit = 0u;
    if (b < 0) { bit = iswall(cx, cy); } else { bit = u32(b); }
    f.bits |= bit << u32(c);
  }
  return f;
}

fn freeSlot(s: u32) { let n = atomicAdd(&nfree, 1u); freeS[n] = s; }

@compute @workgroup_size(256)
fn main(@builtin(local_invocation_id) lid: vec3<u32>) {
  let tid = lid.x;
  let S = u.S; let U2 = u.U2;
  // load the persistent state
  for (var j = 0u; j < 2u; j++) {
    let s = tid + 256u * j;
    px[s] = st[4u * s]; py[s] = st[4u * s + 1u]; wk[s] = st[4u * s + 2u]; wb[s] = u32(st[4u * s + 3u]);
  }
  for (var h = tid; h < HASH; h += 256u) { atomicStore(&hk[h], EMPTY); }
  if (tid == 0u) {
    atomicStore(&nfree, 0u); atomicStore(&err, 0u);
    fi = st[4u * SLOTS + 1u]; ri = st[4u * SLOTS + 2u]; t_wg = st[4u * SLOTS];
    if (t_wg >= u.P) { t_wg = 0; fi = 0; ri = 0; }  // a new frame
    nextF = INF; if (fi < u.nfeeds) { nextF = sched[fi].x; }
    nextR = INF; if (ri < u.nreads) { nextR = reads[ri].x; }
  }
  var t = workgroupUniformLoad(&t_wg);  // (a barrier too) the loop below must be uniform for its barriers
  for (var j = 0u; j < 2u; j++) {
    let s = tid + 256u * j;
    if (wk[s] == DEAD) { freeSlot(s); } else { hinsert(cellkey(px[s], py[s])); }
  }
  workgroupBarrier();
  let tEnd = min(u.P, t + u.K);
  var alive = 0u;
  loop {
    if (t >= tEnd) { break; }
    let par = (u.par0 + t) & 1;
    // 1. feeds before this step (one thread; the hash holds this step's cells)
    if (tid == 0u) {
      atomicStore(&cnt, 0u);
      loop {
        if (nextF != t) { break; }
        let f = sched[fi]; fi++;
        nextF = INF; if (fi < u.nfeeds) { nextF = sched[fi].x; }
        let g = f.w;
        if ((g == 1 && u.left == 0) || (g == 2 && u.right == 0) || (g == 3 && u.reset == 0)) { continue; }
        var x = f.y; var y = f.z;
        let v = x - y;
        if (v < u.lo || v > u.hi) { continue; }
        var s2 = x + y; loop { if (s2 >= 0) { break; } x += u.U; y += u.U; s2 += U2; }
        loop { if (s2 < U2) { break; } x -= u.U; y -= u.U; s2 -= U2; }
        if (iswall(x, y) == 1u) { continue; }
        let k = cellkey(x, y);
        if (hhas(k)) { continue; }
        let n = atomicLoad(&nfree);
        if (n == 0u) { atomicOr(&err, 2u); continue; }
        atomicStore(&nfree, n - 1u);
        let s = freeS[n - 1u];
        px[s] = x; py[s] = y; wk[s] = -1; wb[s] = 0u;
        hinsert(k);
      }
    }
    workgroupBarrier();
    // 2. balls whose direction changed (or new ones): find their next wall
    for (var j = 0u; j < 2u; j++) {
      let s = tid + 256u * j;
      if (wk[s] == -1) {
        let x = px[s]; let y = py[s];
        let dx = 1 - 2 * ((x - par) & 1); let dy = 1 - 2 * ((y - par) & 1);
        let fr = front(x, y, dx, dy, par);
        wk[s] = fr.k; wb[s] = fr.bits;
      }
    }
    // 3. reads and pictures at this step
    if (tid == 0u) {
      loop {
        if (nextR != t) { break; }
        let r = reads[ri]; ri++;
        nextR = INF; if (ri < u.nreads) { nextR = reads[ri].x; }
        var x = r.y; var y = r.z;
        var s2 = x + y; loop { if (s2 >= 0) { break; } x += u.U; y += u.U; s2 += U2; }
        loop { if (s2 < U2) { break; } x -= u.U; y -= u.U; s2 -= U2; }
        var bit = 0u;
        if (hhas(cellkey(x, y))) { bit = 1u; }
        outb[OUT_READS + u32(r.w)] = bit;
      }
    }
    var rec = -1;
    for (var j = 0; j < u.npics; j++) {
      let T = u.picT[j]; let K = u.picK[j];
      if (t > T - K && t <= T) { rec = u.picOff[j] + t - (T - K + 1); }
    }
    if (rec >= 0) {
      for (var j = 0u; j < 2u; j++) {
        let s = tid + 256u * j;
        if (wk[s] != DEAD) {
          let n = atomicAdd(&cnt, 1u);
          let x = px[s]; let y = py[s];
          outb[OUT_CELLS + u32(rec) * SLOTS + n] = (u32((x + y) >> 1) << 16u) | u32(x - y - u.lo);
        }
      }
    }
    workgroupBarrier();
    if (rec >= 0 && tid == 0u) { outb[OUT_COUNTS + u32(rec)] = atomicLoad(&cnt); }
    // 4. the rule, per ball, from the hash and its known wall block
    var nx: array<i32, 2>; var ny: array<i32, 2>; var nk: array<i32, 2>;
    for (var j = 0u; j < 2u; j++) {
      let s = tid + 256u * j;
      nk[j] = DEAD;
      if (wk[s] == DEAD) { continue; }
      let x = px[s]; let y = py[s];
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
        var s2 = cx + cy; if (s2 < 0) { cx += u.U; cy += u.U; } else if (s2 >= U2) { cx -= u.U; cy -= u.U; }
        if (cx - cy >= u.lo && cx - cy <= u.hi && hhas(cellkey(cx, cy))) { f |= 1u << u32(c); }
      }
      let n = countOneBits(f);
      var to = me;
      if (n == 1u) { to = 3 - me; }
      else if (n == 2u && f == 9u) { to = select(2, 1, me == 0); }   // diagonal {0, 3} -> {1, 2}
      else if (n == 2u && f == 6u) { to = select(3, 0, me == 1); }   // {1, 2} -> {0, 3}
      if (n == 2u && (f == 9u || f == 6u) && (walls & f) != 0u) { atomicOr(&err, 1u); }  // a wall would move
      var xn = x0 + (to & 1); var yn = y0 + (to >> 1);
      let s2 = xn + yn; if (s2 < 0) { xn += u.U; yn += u.U; } else if (s2 >= U2) { xn -= u.U; yn -= u.U; }
      nx[j] = xn; ny[j] = yn;
      if (xn - yn < u.lo || xn - yn > u.hi) { nk[j] = DEAD; continue; }   // out of the band: a sink
      if (n == 1u) { nk[j] = wk[s] - 1; } else { nk[j] = -1; }           // direction changed: scan again
    }
    workgroupBarrier();
    // 5. write the new cells, rebuild the hash
    for (var h = tid; h < HASH; h += 256u) { atomicStore(&hk[h], EMPTY); }
    workgroupBarrier();
    alive = 0u;
    for (var j = 0u; j < 2u; j++) {
      let s = tid + 256u * j;
      if (wk[s] == DEAD) { continue; }
      if (nk[j] == DEAD) { wk[s] = DEAD; freeSlot(s); continue; }
      px[s] = nx[j]; py[s] = ny[j]; wk[s] = nk[j];
      hinsert(cellkey(nx[j], ny[j]));
      alive++;
    }
    t++;
    workgroupBarrier();
  }
  // store the state
  for (var j = 0u; j < 2u; j++) {
    let s = tid + 256u * j;
    st[4u * s] = px[s]; st[4u * s + 1u] = py[s]; st[4u * s + 2u] = wk[s]; st[4u * s + 3u] = i32(wb[s]);
  }
  if (tid == 0u) {
    st[4u * SLOTS] = t; st[4u * SLOTS + 1u] = fi; st[4u * SLOTS + 2u] = ri;
    outb[0] = atomicLoad(&err); outb[1] = u32(t);
  }
  let a = SLOTS - atomicLoad(&nfree);  // after the barrier above every free slot has been counted
  if (tid == 0u) { outb[2] = a; }
}
`;

class GpuMachine {
  // meta: web.py machine_info; squares: its wall squares. Call init() (async) before frame().
  constructor(meta, squares) { this.meta = meta; this.squares = squares; this.P = meta.P; this.n = 0; }

  async init() {
    if (typeof navigator === 'undefined' || !navigator.gpu) throw new Error('no WebGPU');
    const adapter = await navigator.gpu.requestAdapter({ powerPreference: 'high-performance' }) || await navigator.gpu.requestAdapter();
    if (!adapter) throw new Error('no WebGPU adapter');
    const m = this.meta, U2 = m.U2, lo = m.lo, hi = m.hi, V2 = hi - lo + 1, S = V2 + 2 * PADG;
    const RWU = (S >> 5) + 2, RWV = (U2 >> 5) + 2, nU = U2 * RWU * 4, nV = S * RWV * 4;
    const need = Math.max(nU, nV);
    if (adapter.limits.maxStorageBufferBindingSize < need || adapter.limits.maxBufferSize < need) throw new Error(`walls need ${(need >> 20)} MB buffers`);
    const required = {};
    for (const k of ['maxStorageBufferBindingSize', 'maxBufferSize']) if (adapter.limits[k] > 134217728) required[k] = adapter.limits[k];
    this.device = await adapter.requestDevice({ requiredLimits: required });
    const d = this.device;
    this.info = adapter.info ? `${adapter.info.vendor || ''} ${adapter.info.architecture || ''}`.trim() : 'gpu';
    // walls straight into the GPU buffers
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
    const bsu = d.createBuffer({ size: U2 * SWU * 4, usage: GPUBufferUsage.STORAGE, mappedAtCreation: true });
    const bsv = d.createBuffer({ size: S * SWV * 4, usage: GPUBufferUsage.STORAGE, mappedAtCreation: true });
    const su = new Int32Array(bsu.getMappedRange()), sv = new Int32Array(bsv.getMappedRange());
    for (let r = 0; r < U2; r++) for (let w = 0; w < RWU; w++) if (wu[r * RWU + w]) su[r * SWU + (w >> 5)] |= 1 << (w & 31);
    for (let r = 0; r < S; r++) for (let w = 0; w < RWV; w++) if (wv[r * RWV + w]) sv[r * SWV + (w >> 5)] |= 1 << (w & 31);
    bu.unmap(); bv.unmap(); bsu.unmap(); bsv.unmap();
    const sched = m.sched;  // sorted by t
    const fb = d.createBuffer({ size: Math.max(16, 16 * sched.length), usage: GPUBufferUsage.STORAGE, mappedAtCreation: true });
    new Int32Array(fb.getMappedRange()).set(sched.flat()); fb.unmap();
    const rs = [];
    this.readNames = [];
    for (const [name, [t, cells]] of Object.entries(m.reads)) for (const [x, y] of cells) rs.push([t, x, y, name]);
    rs.sort((a, b) => a[0] - b[0]);
    const rb = d.createBuffer({ size: Math.max(16, 16 * rs.length), usage: GPUBufferUsage.STORAGE, mappedAtCreation: true });
    new Int32Array(rb.getMappedRange()).set(rs.flatMap((e, i) => [e[0], e[1], e[2], i])); rb.unmap();
    this.readNames = rs.map(e => e[3]);
    this.nreads = rs.length;
    const stb = d.createBuffer({ size: 4 * (4 * SLOTS + 16), usage: GPUBufferUsage.STORAGE, mappedAtCreation: true });
    const sti = new Int32Array(stb.getMappedRange());
    for (let s = 0; s < SLOTS; s++) sti[4 * s + 2] = -2147483648;  // every slot free
    sti[4 * SLOTS] = m.P;  // first dispatch starts a frame
    stb.unmap();
    this.outSize = 4 * (2048 + 64 * SLOTS);
    this.outb = d.createBuffer({ size: this.outSize, usage: GPUBufferUsage.STORAGE | GPUBufferUsage.COPY_SRC });
    this.rbuf = d.createBuffer({ size: this.outSize, usage: GPUBufferUsage.COPY_DST | GPUBufferUsage.MAP_READ });
    this.ub = d.createBuffer({ size: 128, usage: GPUBufferUsage.UNIFORM | GPUBufferUsage.COPY_DST });
    this.params = new Int32Array(32);
    this.params.set([U2 / 2, U2, lo, hi, S, RWU, RWV, m.P, m.t_lo & 1, sched.length, rs.length, 0, 0, 0, 0, STEPS_PER_DISPATCH]);
    this.params[28] = SWU; this.params[29] = SWV;
    const mod = d.createShaderModule({ code: WGSL });
    const ci = await mod.getCompilationInfo();
    const bad = ci.messages.filter(x => x.type === 'error');
    if (bad.length) throw new Error('shader: ' + bad.map(x => `${x.lineNum}:${x.linePos} ${x.message}`).join('; '));
    this.pipe = d.createComputePipeline({ layout: 'auto', compute: { module: mod, entryPoint: 'main' } });
    this.bg = d.createBindGroup({ layout: this.pipe.getBindGroupLayout(0), entries: [
      { binding: 0, resource: { buffer: this.ub } }, { binding: 1, resource: { buffer: bu } }, { binding: 2, resource: { buffer: bv } },
      { binding: 3, resource: { buffer: fb } }, { binding: 4, resource: { buffer: rb } }, { binding: 5, resource: { buffer: stb } },
      { binding: 6, resource: { buffer: this.outb } }, { binding: 7, resource: { buffer: bsu } }, { binding: 8, resource: { buffer: bsv } }] });
    return this;
  }

  // Same contract as engine.js Machine.frame, but async.
  async frame(inp, traces = []) {
    const d = this.device, p = this.params, m = this.meta;
    if (traces.length > 4) throw new Error('at most 4 pictures per frame');
    p[12] = inp.left ? 1 : 0; p[13] = inp.right ? 1 : 0; p[14] = inp.reset ? 1 : 0; p[11] = traces.length;
    let off = 0;
    traces.forEach(([T, K], j) => { p[16 + j] = T; p[20 + j] = K; p[24 + j] = off; off += K; });
    if (off > 64) throw new Error('at most 64 recorded steps per frame');
    d.queue.writeBuffer(this.ub, 0, p);
    const enc = d.createCommandEncoder(), pass = enc.beginComputePass();
    pass.setPipeline(this.pipe); pass.setBindGroup(0, this.bg);
    for (let k = 0; k < Math.ceil(this.P / STEPS_PER_DISPATCH); k++) pass.dispatchWorkgroups(1);
    pass.end();
    enc.copyBufferToBuffer(this.outb, 0, this.rbuf, 0, this.outSize);
    d.queue.submit([enc.finish()]);
    await this.rbuf.mapAsync(GPUMapMode.READ);
    const o = new Uint32Array(this.rbuf.getMappedRange().slice(0));
    this.rbuf.unmap();
    if (o[0] & 1) throw new Error('a wall moved: the machine is broken');
    if (o[0] & 2) throw new Error(`more than ${SLOTS} balls`);
    if (o[0] & 4) throw new Error('cell hash full');
    if (o[1] !== this.P) throw new Error(`frame stopped at step ${o[1]}`);
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
    const pics = traces.map(([T, K], j) => {
      const cnts = [], raw = [];
      for (let s = 0; s < K; s++) {
        const r = p[24 + j] + s, c = o[1024 + r];
        cnts.push(c);
        for (let i = 0; i < c; i++) { const w = o[2048 + r * SLOTS + i]; raw.push(w >>> 16, w & 0xffff); }
      }
      return { ph: T, counts: cnts, raw: Uint16Array.from(raw) };
    });
    this.n++;
    return { state, win: bits.win, lose: bits.lose, pics, alive: o[2] };
  }
}

if (typeof module !== 'undefined') module.exports = { GpuMachine, WGSL };
if (typeof self !== 'undefined') self.GpuMachine = GpuMachine;
