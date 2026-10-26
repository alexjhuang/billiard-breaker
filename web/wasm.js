// The C engine (csim/bbm.c) compiled to WebAssembly (web/bbm.wasm, built by
// csim/build_wasm.sh): the same machine as engine.js's Machine, about twice as
// fast. Same interface, but construction is async: await new WasmMachine(meta,
// squares, bytes).init(). Loads in the worker (importScripts) and in node.
'use strict';

class WasmMachine {
  constructor(meta, squares, wasm) { this.meta = meta; this.squares = squares; this.wasm = wasm; this.P = meta.P; this.n = 0; }

  async init() {
    const { instance } = await WebAssembly.instantiate(this.wasm, {});
    const E = this.E = instance.exports, meta = this.meta, lo = meta.lo;
    if (E._initialize) E._initialize();
    this.w = E.world_new(BigInt(meta.U2 / 2), BigInt(lo), BigInt(meta.hi));
    const sq = this.squares, n = 2 * sq.length, p = E.malloc(16 * n);   // 4 wall cells per square
    const a = new BigInt64Array(E.memory.buffer, p, 2 * n);
    for (let j = 0, u = 0, k = 0; j < sq.length; j += 2) {
      u += sq[j];
      const c = u - 1, v0 = sq[j + 1] + lo;   // top-left cell has x + y = u - 1
      for (const [du, dv] of [[0, 0], [1, 1], [1, -1], [2, 0]]) {
        const u2 = c + du, v = v0 + dv;
        a[k++] = BigInt((u2 + v) / 2); a[k++] = BigInt((u2 - v) / 2);
      }
    }
    E.world_add(this.w, BigInt(n), p, 1); E.free(p);
    const sc = meta.sched;
    this.ft = Int32Array.from(sc, e => e[0]); this.fg = Int8Array.from(sc, e => e[3]);
    this.fx = BigInt64Array.from(sc, e => BigInt(e[1])); this.fy = BigInt64Array.from(sc, e => BigInt(e[2]));
    const rs = [];
    for (const [name, [t, cells]] of Object.entries(meta.reads)) for (const [x, y] of cells) rs.push([t, x, y, name]);
    rs.sort((a, b) => a[0] - b[0]);
    this.rt = Int32Array.from(rs, e => e[0]); this.rn = rs.map(e => e[3]);
    this.rx = BigInt64Array.from(rs, e => BigInt(e[1])); this.ry = BigInt64Array.from(rs, e => BigInt(e[2]));
    this.pf = E.malloc(24 * sc.length); this.pr = E.malloc(24 * rs.length); this.po = E.malloc(rs.length + 1);
    E.world_set_time(this.w, BigInt(meta.t_lo));
    return this;
  }

  // Run frame n with inputs {left, right, reset}; traces as Machine.frame.
  frame(inp, traces = []) {
    const E = this.E, w = this.w, meta = this.meta, lo = meta.lo, start = meta.t_lo + this.n * this.P, end = start + this.P;
    const gate = [1, inp.left, inp.right, inp.reset], nr = this.rt.length;
    let nf = 0;
    const F = new BigInt64Array(E.memory.buffer, this.pf, 3 * this.ft.length);
    for (let i = 0; i < this.ft.length; i++)
      if (gate[this.fg[i]]) { F[3 * nf] = BigInt(this.ft[i] + start); F[3 * nf + 1] = this.fx[i]; F[3 * nf + 2] = this.fy[i]; nf++; }
    const R = new BigInt64Array(E.memory.buffer, this.pr, 3 * nr);
    for (let i = 0; i < nr; i++) { R[3 * i] = BigInt(this.rt[i] + start); R[3 * i + 1] = this.rx[i]; R[3 * i + 2] = this.ry[i]; }
    const run = T => E.world_run(w, BigInt(T), BigInt(nf), this.pf, BigInt(nr), this.pr, this.po);
    const pics = [];
    for (const [T, K] of [...traces].sort((a, b) => a[0] - b[0])) {
      run(start + T - K);
      const k = Math.min(K, start + T - Number(E.world_time(w)));   // steps left to record
      const cap = Math.max(1, k) * (Number(E.world_count(w)) + nf + 64), puv = E.malloc(8 * cap), pc = E.malloc(8 * Math.max(1, k));
      const m = Number(E.world_run_trace(w, BigInt(start + T), BigInt(nf), this.pf, BigInt(nr), this.pr, this.po, puv, BigInt(cap), pc));
      const uv = new Int32Array(E.memory.buffer, puv, 2 * m), raw = new Uint16Array(2 * m);
      for (let i = 0; i < m; i++) { raw[2 * i] = uv[2 * i] >> 1; raw[2 * i + 1] = uv[2 * i + 1] - lo; }   // ((x + y) / 2, x - y - lo)
      const counts = Array.from(new BigInt64Array(E.memory.buffer, pc, Math.max(0, k)), Number);
      if (counts.reduce((s, c) => s + c, 0) !== m) throw new Error('trace buffer too small');
      E.free(puv); E.free(pc);
      pics.push({ ph: T, counts, raw });
    }
    run(end);
    if (E.world_broken(w)) throw new Error('a wall moved: the machine is broken');
    const out = new Uint8Array(E.memory.buffer, this.po, nr), counts = {};
    for (let i = 0; i < nr; i++) counts[this.rn[i]] = (counts[this.rn[i]] || 0) + out[i];
    const bits = {};
    for (const [name, c] of Object.entries(counts)) {
      if (c !== 0 && c !== 2) throw new Error(`half a ball at output ${name}, frame ${this.n}`);
      bits[name] = c >> 1;
    }
    const state = {};
    for (const [name, width] of meta.fields) {
      const key = name === 'bricks' ? 'brick' : name;
      let v = 0;
      for (let i = 0; i < width; i++) v += bits[key + i] * 2 ** i;
      state[name] = v;
    }
    this.n++;
    return { state, win: bits.win, lose: bits.lose, pics };
  }
}

if (typeof module !== 'undefined') module.exports = { WasmMachine };
