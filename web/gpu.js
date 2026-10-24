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
};
@group(0) @binding(0) var<uniform> u: Params;
@group(0) @binding(1) var<storage, read> wu: array<u32>;         // walls by row x+y, bit x-y-lo+PAD
@group(0) @binding(2) var<storage, read> wv: array<u32>;         // walls by row x-y-lo+PAD, bit x+y
@group(0) @binding(3) var<storage, read> sched: array<vec4<i32>>; // feeds: t, x, y, gate (0 always, 1 left, 2 right, 3 reset)
@group(0) @binding(4) var<storage, read> reads: array<vec4<i32>>; // t, x, y, output index
@group(0) @binding(5) var<storage, read_write> st: array<i32>;    // persistent: per slot x, y, wallk, wallbits; then cur_t, fi, ri
@group(0) @binding(6) var<storage, read_write> outb: array<u32>;  // [0] errors [1] cur_t [2] alive; [16..] read bits; [1024..] pic counts; [2048..] pic cells

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

// first set bit at position >= p in three rows of wv (nw words), or -1
fn scanFwdV(r0: i32, r1: i32, r2: i32, p: i32, nw: i32) -> i32 {
  var i = p >> 5;
  if (i >= nw) { return -1; }
  var m = (wv[u32(r0 + i)] | wv[u32(r1 + i)] | wv[u32(r2 + i)]) & (0xffffffffu << u32(p & 31));
  loop { if (m != 0u) { break; } i++; if (i >= nw) { return -1; } m = wv[u32(r0 + i)] | wv[u32(r1 + i)] | wv[u32(r2 + i)]; }
  return i * 32 + i32(ctz(m));
}
fn scanBwdV(r0: i32, r1: i32, r2: i32, p: i32) -> i32 {
  if (p < 0) { return -1; }
  var i = p >> 5;
  var m = (wv[u32(r0 + i)] | wv[u32(r1 + i)] | wv[u32(r2 + i)]) & (0xffffffffu >> u32(31 - (p & 31)));
  loop { if (m != 0u) { break; } i--; if (i < 0) { return -1; } m = wv[u32(r0 + i)] | wv[u32(r1 + i)] | wv[u32(r2 + i)]; }
  return i * 32 + 31 - i32(countLeadingZeros(m));
}
fn scanFwdU(r0: i32, r1: i32, r2: i32, p: i32, nw: i32) -> i32 {
  var i = p >> 5;
  if (i >= nw) { return -1; }
  var m = (wu[u32(r0 + i)] | wu[u32(r1 + i)] | wu[u32(r2 + i)]) & (0xffffffffu << u32(p & 31));
  loop { if (m != 0u) { break; } i++; if (i >= nw) { return -1; } m = wu[u32(r0 + i)] | wu[u32(r1 + i)] | wu[u32(r2 + i)]; }
  return i * 32 + i32(ctz(m));
}
fn scanBwdU(r0: i32, r1: i32, r2: i32, p: i32) -> i32 {
  if (p < 0) { return -1; }
  var i = p >> 5;
  var m = (wu[u32(r0 + i)] | wu[u32(r1 + i)] | wu[u32(r2 + i)]) & (0xffffffffu >> u32(31 - (p & 31)));
  loop { if (m != 0u) { break; } i--; if (i < 0) { return -1; } m = wu[u32(r0 + i)] | wu[u32(r1 + i)] | wu[u32(r2 + i)]; }
  return i * 32 + 31 - i32(countLeadingZeros(m));
}

// steps until the block in front of a ball at (x, y) moving (dx, dy) holds a wall, or it leaves the band
fn wallSteps(x: i32, y: i32, dx: i32, dy: i32) -> i32 {
  let u2 = wrapi(x + y, u.U2);
  let vv = x - y - u.lo + PAD;
  if (dx == dy) {  // SE / NW: along a row of constant v, round the loop
    let r0 = (vv - 1) * u.RWV; let r1 = r0 + u.RWV; let r2 = r1 + u.RWV;
    let nw = (u.U2 + 31) >> 5;
    if (dx > 0) {
      var q = scanFwdV(r0, r1, r2, u2 + 1, nw);
      if (q >= u.U2) { q = -1; }
      if (q < 0) { q = scanFwdV(r0, r1, r2, 0, nw); }
      if (q < 0 || q >= u.U2) { return INF; }
      return (wrapi(q - u2, u.U2) - 1) >> 1;
    }
    var q = scanBwdV(r0, r1, r2, u2 - 1);
    if (q < 0) { q = scanBwdV(r0, r1, r2, u.U2 - 1); }
    if (q < 0) { return INF; }
    return (wrapi(u2 - q, u.U2) - 1) >> 1;
  }
  var ua = u2 - 1; if (ua < 0) { ua = u.U2 - 1; }
  var ub = u2 + 1; if (ub == u.U2) { ub = 0; }
  let R = u.RWU;
  if (dy < 0) {  // NE: v rises
    let ex = (u.hi - (x - y)) >> 1;
    let q = scanFwdU(ua * R, u2 * R, ub * R, vv + 1, R);
    if (q < 0) { return ex; }
    return min(ex, (q - vv - 1) >> 1);
  }
  let ex = ((x - y) - u.lo) >> 1;  // SW
  let q = scanBwdU(ua * R, u2 * R, ub * R, vv - 1);
  if (q < 0) { return ex; }
  return min(ex, (vv - q - 1) >> 1);
}

// the ball flies k more steps; which cells of the block it is then in are walls
fn wallBits(x: i32, y: i32, dx: i32, dy: i32, k: i32, par: i32) -> u32 {
  if (k >= INF) { return 0u; }
  let xk = x + dx * k; let yk = y + dy * k;
  let o = (par + k) & 1;
  let x0 = xk - ((xk - o) & 1); let y0 = yk - ((yk - o) & 1);
  return iswall(x0, y0) | (iswall(x0 + 1, y0) << 1u) | (iswall(x0, y0 + 1) << 2u) | (iswall(x0 + 1, y0 + 1) << 3u);
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
        if (fi >= u.nfeeds || sched[fi].x != t) { break; }
        let f = sched[fi]; fi++;
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
        let k = wallSteps(x, y, dx, dy);
        wk[s] = k; wb[s] = wallBits(x, y, dx, dy, k, par);
      }
    }
    // 3. reads and pictures at this step
    if (tid == 0u) {
      loop {
        if (ri >= u.nreads || reads[ri].x != t) { break; }
        let r = reads[ri]; ri++;
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
    bu.unmap(); bv.unmap();
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
    const mod = d.createShaderModule({ code: WGSL });
    const ci = await mod.getCompilationInfo();
    const bad = ci.messages.filter(x => x.type === 'error');
    if (bad.length) throw new Error('shader: ' + bad.map(x => `${x.lineNum}:${x.linePos} ${x.message}`).join('; '));
    this.pipe = d.createComputePipeline({ layout: 'auto', compute: { module: mod, entryPoint: 'main' } });
    this.bg = d.createBindGroup({ layout: this.pipe.getBindGroupLayout(0), entries: [
      { binding: 0, resource: { buffer: this.ub } }, { binding: 1, resource: { buffer: bu } }, { binding: 2, resource: { buffer: bv } },
      { binding: 3, resource: { buffer: fb } }, { binding: 4, resource: { buffer: rb } }, { binding: 5, resource: { buffer: stb } },
      { binding: 6, resource: { buffer: this.outb } }] });
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
