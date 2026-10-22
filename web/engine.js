// The billiard-ball machine in the browser, for the static (GitHub Pages)
// version: the Margolus rule of csim/bbm.c and the frame runner of
// fastsim.Runner, on the compiled hardware that web.py --export writes
// (machine.json + wall squares). It adds the key and power balls at their
// ports, steps the automaton and reads the output lanes; no game rules.
// Runs in a Web Worker posting the same 'frame'/'pics' messages as web.py's
// event stream, or under node for the tests (module.exports).
'use strict';
const PAD = 2;
const bit = (a, i) => (a[i >>> 5] >>> (i & 31)) & 1;

class World {
  constructor(U2, lo, hi) {
    this.U2 = U2; this.lo = lo; this.hi = hi; this.V2 = hi - lo + 1;
    this.S = this.V2 + 2 * PAD; this.N = U2 * this.S;
    // occ: walls and moving balls (a ball never sits on a wall cell), so a lone
    // ball's block is checked with 4 reads; wall is looked at only when needed
    this.wall = new Int32Array((this.N >>> 5) + 2);
    this.occ = new Int32Array((this.N >>> 5) + 2);
    this.n = 0; this.t = 0; this.broken = false; this.cap = 0;
    this.grow(4096);
  }

  grow(cap) {
    const re = a => { const b = new Int32Array(cap); if (a) b.set(a.subarray(0, Math.min(a.length, cap))); return b; };
    this.p = re(this.p); this.pu = re(this.pu); this.pv = re(this.pv);
    this.np = new Int32Array(cap); this.nu = new Int32Array(cap); this.nv = new Int32Array(cap);
    this.cap = cap;
  }

  index(x, y) {  // cell index, or -1 outside the band
    const v = x - y;
    if (v < this.lo || v > this.hi) return -1;
    const u = ((x + y) % this.U2 + this.U2) % this.U2;
    return u * this.S + (v - this.lo + PAD);
  }

  addWall(i) { this.wall[i >>> 5] |= 1 << (i & 31); this.occ[i >>> 5] |= 1 << (i & 31); }

  add(i) {
    if (i < 0 || bit(this.occ, i)) return;
    if (this.n + 1 > this.cap) this.grow(2 * this.cap);
    this.occ[i >>> 5] |= 1 << (i & 31);
    this.p[this.n] = i; this.pu[this.n] = Math.floor(i / this.S); this.pv[this.n] = i % this.S; this.n++;
  }

  step() {
    const o = this.t & 1, S = this.S, DX = S + 1, DY = S - 1, N = this.N, U2 = this.U2, lo = this.lo;
    const occ = this.occ, wall = this.wall;
    if (this.cap < 4 * this.n + 4) this.grow(4 * this.n + 4);
    const p = this.p, pu = this.pu, pv = this.pv, np = this.np, nu = this.nu, nv = this.nv;
    let m = 0;
    for (let k = 0, n = this.n; k < n; k++) {
      const i = p[k];
      if (!((occ[i >>> 5] >>> (i & 31)) & 1)) continue;  // its block was already handled
      const u2 = pu[k], vv = pv[k], v = vv - PAD + lo;
      const rx = (((u2 + v) >> 1) - o) & 1, ry = (((u2 - v) >> 1) - o) & 1;
      let c0 = i - rx * DX - ry * DY;
      if (c0 < 0) c0 += N;
      let c1 = c0 + DX, c2 = c0 + DY, c3 = c1 + DY;
      if (c1 >= N) c1 -= N;
      if (c2 >= N) c2 -= N;
      if (c3 >= N) c3 -= N;
      const f0 = (occ[c0 >>> 5] >>> (c0 & 31)) & 1, f1 = (occ[c1 >>> 5] >>> (c1 & 31)) & 1;
      const f2 = (occ[c2 >>> 5] >>> (c2 & 31)) & 1, f3 = (occ[c3 >>> 5] >>> (c3 & 31)) & 1;
      if (f0 + f1 + f2 + f3 === 1) {  // lone particle: to the opposite corner of its block
        occ[i >>> 5] &= ~(1 << (i & 31));
        const dx = 1 - 2 * rx, dy = 1 - 2 * ry;
        let j = i + dx * DX + dy * DY, cu = u2 + dx + dy;
        if (j < 0) j += N; else if (j >= N) j -= N;
        if (cu < 0) cu += U2; else if (cu >= U2) cu -= U2;
        np[m] = j; nu[m] = cu; nv[m] = vv + dx - dy; m++;
        continue;
      }
      const w0 = (wall[c0 >>> 5] >>> (c0 & 31)) & 1, w1 = (wall[c1 >>> 5] >>> (c1 & 31)) & 1;
      const w2 = (wall[c2 >>> 5] >>> (c2 & 31)) & 1, w3 = (wall[c3 >>> 5] >>> (c3 & 31)) & 1;
      const m0 = f0 & ~w0, m1 = f1 & ~w1, m2 = f2 & ~w2, m3 = f3 & ~w3;
      let g0 = f0, g1 = f1, g2 = f2, g3 = f3;  // 2 on a diagonal: the other diagonal; else unchanged
      if (f0 + f1 + f2 + f3 === 2) {
        if (f0 && f3) { g0 = 0; g3 = 0; g1 = 1; g2 = 1; }
        else if (f1 && f2) { g1 = 0; g2 = 0; g0 = 1; g3 = 1; }
      }
      if (m0) occ[c0 >>> 5] &= ~(1 << (c0 & 31));
      if (m1) occ[c1 >>> 5] &= ~(1 << (c1 & 31));
      if (m2) occ[c2 >>> 5] &= ~(1 << (c2 & 31));
      if (m3) occ[c3 >>> 5] &= ~(1 << (c3 & 31));
      if ((w0 && !g0) || (w1 && !g1) || (w2 && !g2) || (w3 && !g3)) this.broken = true;
      let tu = u2 - rx - ry;  // the block's top-left corner, in (x + y, x - y + PAD - lo)
      const tv = vv - rx + ry;
      if (tu < 0) tu += U2;
      const tu1 = tu + 1 >= U2 ? tu + 1 - U2 : tu + 1, tu2 = tu + 2 >= U2 ? tu + 2 - U2 : tu + 2;
      if (g0 && !w0) { np[m] = c0; nu[m] = tu; nv[m] = tv; m++; }
      if (g1 && !w1) { np[m] = c1; nu[m] = tu1; nv[m] = tv + 1; m++; }
      if (g2 && !w2) { np[m] = c2; nu[m] = tu1; nv[m] = tv - 1; m++; }
      if (g3 && !w3) { np[m] = c3; nu[m] = tu2; nv[m] = tv; m++; }
    }
    let kept = 0;  // write the new positions; drop those in the padding (sinks)
    const vmax = PAD + this.V2;
    for (let k = 0; k < m; k++) {
      const q = np[k];
      if (nv[k] < PAD || nv[k] >= vmax) continue;
      occ[q >>> 5] |= 1 << (q & 31);
      np[kept] = q; nu[kept] = nu[k]; nv[kept] = nv[k]; kept++;
    }
    this.p = np; this.np = p; this.pu = nu; this.nu = pu; this.pv = nv; this.nv = pv;
    this.n = kept; this.t++;
  }

  cells(out, off) {  // append (x + y, x - y - lo) of every moving cell as uint16 pairs
    for (let k = 0; k < this.n; k++) { out[off++] = this.pu[k]; out[off++] = this.pv[k] - PAD; }
    return off;
  }
}

class Machine {
  // meta: web.py machine_info; squares: its (gap in x + y, x - y - lo) uint16 pairs
  constructor(meta, squares) {
    const w = this.w = new World(meta.U2, meta.lo, meta.hi);
    this.meta = meta; this.P = meta.P;
    for (let j = 0, u = 0; j < squares.length; j += 2) {  // each wall square: 4 cells from its centre
      u += squares[j];
      const v = squares[j + 1] + meta.lo, a = u - 1;  // top-left cell has x + y = u - 1
      for (const [du, dv] of [[0, 0], [1, 1], [1, -1], [2, 0]]) {
        const uu = (a + du + meta.U2) % meta.U2;
        w.addWall(uu * w.S + (v + dv - meta.lo + PAD));
      }
    }
    const sc = meta.sched;
    this.ft = Int32Array.from(sc, e => e[0]); this.fi = Int32Array.from(sc, e => w.index(e[1], e[2]));
    this.fg = Int8Array.from(sc, e => e[3]);
    const rs = [];
    for (const [name, [t, cells]] of Object.entries(meta.reads))
      for (const [x, y] of cells) rs.push([t, w.index(x, y), name]);
    rs.sort((a, b) => a[0] - b[0]);
    this.rt = Int32Array.from(rs, e => e[0]); this.ri = Int32Array.from(rs, e => e[1]); this.rn = rs.map(e => e[2]);
    this.n = 0; w.t = meta.t_lo;
  }

  // Run frame n with inputs {left, right, reset}. traces: [[T, K], ...] (T
  // relative to the frame's start, sorted, K <= T): the cells after each of
  // the K steps up to T are returned like web.py's pictures.
  frame(inp, traces = []) {
    const w = this.w, start = this.meta.t_lo + this.n * this.P, end = start + this.P, gate = [1, inp.left, inp.right, inp.reset];
    const counts = {}, pics = traces.map(([T, K]) => ({ ph: T, counts: [], raw: new Uint16Array(2 * K * (w.n + 2000)), off: 0, K }));
    let fi = 0, ri = 0, ti = 0;
    const nf = this.ft.length, nr = this.rt.length;
    for (let t = start; t < end; t++) {
      for (; fi < nf && this.ft[fi] + start === t; fi++) if (gate[this.fg[fi]]) w.add(this.fi[fi]);
      for (; ri < nr && this.rt[ri] + start === t; ri++) counts[this.rn[ri]] = (counts[this.rn[ri]] || 0) + bit(w.occ, this.ri[ri]);
      w.step();
      for (let j = ti; j < pics.length && pics[j].ph - pics[j].K < t + 1 - start; j++) {
        const q = pics[j];
        if (t + 1 - start > q.ph) { ti = j + 1; continue; }
        if (q.off + 2 * w.n > q.raw.length) { const b = new Uint16Array(2 * q.raw.length + 2 * w.n); b.set(q.raw); q.raw = b; }
        q.counts.push(w.n); q.off = w.cells(q.raw, q.off);
      }
    }
    if (w.broken) throw new Error('a wall moved: the machine is broken');
    const bits = {};
    for (const [name, c] of Object.entries(counts)) {
      if (c !== 0 && c !== 2) throw new Error(`half a ball at output ${name}, frame ${this.n}`);
      bits[name] = c >> 1;
    }
    const state = {};
    for (const [name, width] of this.meta.fields) {
      const key = name === 'bricks' ? 'brick' : name;
      let v = 0;
      for (let i = 0; i < width; i++) v += bits[key + i] * 2 ** i;
      state[name] = v;
    }
    this.n++;
    return { state, win: bits.win, lose: bits.lose, pics: pics.map(q => ({ ph: q.ph, counts: q.counts, raw: q.raw.subarray(0, q.off) })) };
  }
}

// ---------------------------------------------------------------- Web Worker: the frame loop of web.py
if (typeof WorkerGlobalScope !== 'undefined' && self instanceof WorkerGlobalScope) {
  const PICS = 3, CLIP = 16, CLIP_EVERY = 0.3, FPS_CAP = 20;
  let M = null, keys = { left: 0, right: 0, reset: 1 }, sweep = 8, phase = 0, fps = 0, period = 0, lastClip = -1;
  let tPrev = null, tNext = 0, paused = false, timer = null;
  const now = () => performance.now() / 1000;
  const pictureTimes = () => {  // same schedule as web.py Machine.picture_times
    const P = M.P, step = !sweep ? P / PICS : Math.max(1, P / (PICS * (fps || FPS_CAP) * sweep));
    const clip = now() - lastClip >= CLIP_EVERY;
    if (clip) lastClip = now();
    const times = new Map();
    for (let j = 0; j < PICS; j++) {
      const ph = Math.min(Math.max(Math.floor(phase + j * step) % P, CLIP), P - 1);
      times.set(ph, Math.max(times.get(ph) || 1, clip && j === 0 ? CLIP : 1));
    }
    phase = (phase + PICS * step) % P;
    return [...times].sort((a, b) => a[0] - b[0]);
  };
  const loop = () => {
    try {
      const t0 = now();
      tNext = Math.max(tNext + 1 / FPS_CAP, t0);
      const inp = { ...keys }, n = M.n;
      keys.reset = 0;
      const r = M.frame(inp, pictureTimes()), ms = 1000 * (now() - t0);
      if (tPrev !== null) { period = fps ? .8 * period + .2 * (t0 - tPrev) : t0 - tPrev; fps = 1 / period; }
      tPrev = t0;
      postMessage({ type: 'frame', n, state: r.state, win: r.win, lose: r.lose, inp, ms: Math.round(ms * 10) / 10, fps: Math.round(fps * 100) / 100 });
      postMessage({ type: 'pics', n, P: M.P, sweep, pics: r.pics }, r.pics.map(q => q.raw.buffer));
      timer = paused ? null : setTimeout(loop, Math.max(0, 1000 * (tNext - now())));
    } catch (e) {
      postMessage({ type: 'fault', error: String(e) });
    }
  };
  onmessage = e => {
    const d = e.data;
    if (d.type === 'init') { M = new Machine(d.meta, d.squares); tNext = now(); loop(); }
    else if (d.type === 'control') {
      for (const k of ['left', 'right']) if (k in d) keys[k] = d[k] ? 1 : 0;
      if (d.reset) keys.reset = 1;
      if (typeof d.sweep === 'number') sweep = Math.max(0, Math.min(600, d.sweep));
      if ('pause' in d) {  // hidden tab: stop computing
        paused = !!d.pause;
        if (!paused && !timer) { tNext = now(); tPrev = null; loop(); }
      }
    }
  };
}
if (typeof module !== 'undefined') module.exports = { World, Machine };
