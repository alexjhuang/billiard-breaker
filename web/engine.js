// The billiard-ball machine in the browser, for the static (GitHub Pages)
// version: the event-driven Margolus simulator of csim/bbm.c and the frame
// runner of fastsim.Runner, on the compiled hardware that web.py --export
// writes (machine.json + wall squares). It adds the key and power balls at
// their ports, runs the automaton and reads the output lanes; no game rules.
// Runs in a Web Worker posting the same 'frame'/'pics' messages as web.py's
// event stream, or under node for the tests (module.exports).
'use strict';
const PAD = 2, INF = 0x7fffffff;  // times and positions are int32: every frame start they are re-based (World.rebase)
const DX = [1, -1, 1, -1], DY = [1, 1, -1, -1];            // d = rx + 2*ry: SE SW NE NW
const KIND = Int8Array.of(0, 1, 4, 2, 1, 5, 0, 3, 5, 2, 3, 4);  // KIND[3d + j]: the keys each direction is indexed by
const SLOT = Int8Array.of(0, 1, -1, -1, 2, -1, -1, 1, 0, -1, -1, 2, 0, -1, -1, 1, -1, 2, -1, -1, 0, 1, 2, -1);  // SLOT[6d + kind]: KIND inverted
const wrap = (a, m) => { while (a < 0) a += m; while (a >= m) a -= m; return a; };
const ctz = m => 31 - Math.clz32(m & -m);
const par = t => t & 1;

// Event-driven, like csim/bbm.c: a particle is a straight line (position at
// its last event, direction) until its 2x2 block holds something else. Walls
// are found by scanning three rows of a bitmap along the flight (two
// orientations: rows of constant x - y for SE/NW movers, of constant x + y for
// NE/SW), partners through an index on the coordinate two directions share.
class World {
  constructor(U2, lo, hi) {
    this.U = U2 / 2; this.U2 = U2; this.lo = lo; this.hi = hi; this.V2 = hi - lo + 1; this.S = this.V2 + 2 * PAD;
    this.RWU = (this.S >> 5) + 2; this.RWV = (U2 >> 5) + 2;
    this.wu = new Int32Array(U2 * this.RWU);      // walls by row u2 = x + y, bit vv = x - y - lo + PAD
    this.wv = new Int32Array(this.S * this.RWV);  // walls by row vv, bit u2
    const U = this.U;
    this.nb = Int32Array.of(U / 2 + 1, U / 2 + 1, U / 2 + 1, U / 2 + 1, (this.V2 >> 1) + 3, U + 1);
    this.hoff = new Int32Array(7); this.boff = new Int32Array(7);  // offsets of each kind's buckets in head / nbm
    for (let k = 0; k < 6; k++) { this.hoff[k + 1] = this.hoff[k] + this.nb[k]; this.boff[k + 1] = this.boff[k] + (this.nb[k] >> 5) + 1; }
    this.head = new Int32Array(this.hoff[6]).fill(-1); this.nbm = new Int32Array(this.boff[6]);
    this.top = 0; this.cap = 0; this.n = 0; this.freeHead = -1;
    this.hn = 0; this.hcap = 4096; this.ht = new Int32Array(this.hcap); this.hid = new Int32Array(this.hcap); this.hver = new Int32Array(this.hcap);
    this.grp = new Int32Array(64); this.tmp = new Int32Array(64); this.bxs = new Int32Array(64); this.bys = new Int32Array(64); this.t = 0; this.broken = false;
    this.key = new Int32Array(3); this.wl = new Int32Array(4); this.mv = new Int32Array(4); this.to = new Int32Array(4);
    this.origin = 0;  // absolute step of this world's time 0
    this.grow(1024);
  }

  grow(cap) {
    const re = (a, T, fill) => { const b = new T(cap); if (fill !== undefined) b.fill(fill); if (a) b.set(a); return b; };
    this.x = re(this.x, Int32Array); this.y = re(this.y, Int32Array); this.t0 = re(this.t0, Int32Array); this.ev = re(this.ev, Int32Array);
    this.d = re(this.d, Int8Array); this.alive = re(this.alive, Uint8Array); this.ver = re(this.ver, Int32Array); this.mark = re(this.mark, Int32Array, -1);
    const re3 = a => {  // three per-slot arrays in one: index j * cap + i
      const b = new Int32Array(3 * cap);
      if (a) for (let j = 0; j < 3; j++) b.set(a.subarray(j * this.cap, (j + 1) * this.cap), j * cap);
      return b;
    };
    this.nxt = re3(this.nxt); this.prv = re3(this.prv); this.bk = re3(this.bk);
    this.cap = cap;
  }

  addWall(u2, vv) {
    this.wu[u2 * this.RWU + (vv >> 5)] |= 1 << (vv & 31);
    this.wv[vv * this.RWV + (u2 >> 5)] |= 1 << (u2 & 31);
  }

  isWall(x, y) {
    const v = x - y;
    if (v < this.lo || v > this.hi) return 0;
    const vv = v - this.lo + PAD;
    return (this.wu[wrap(x + y, this.U2) * this.RWU + (vv >> 5)] >>> (vv & 31)) & 1;
  }

  isWallV(x, y) {  // the same, from the other bitmap (hot after a SE/NW scan)
    const v = x - y;
    if (v < this.lo || v > this.hi) return 0;
    const u2 = wrap(x + y, this.U2);
    return (this.wv[(v - this.lo + PAD) * this.RWV + (u2 >> 5)] >>> (u2 & 31)) & 1;
  }

  static scanFwd(a, o0, o1, o2, p, nw) {  // first set bit >= p in the OR of three rows, or -1
    let i = p >> 5;
    if (i >= nw) return -1;
    let m = (a[o0 + i] | a[o1 + i] | a[o2 + i]) & (-1 << (p & 31));
    while (!m) { if (++i >= nw) return -1; m = a[o0 + i] | a[o1 + i] | a[o2 + i]; }
    return i * 32 + ctz(m);
  }

  static scanBwd(a, o0, o1, o2, p) {  // last set bit <= p, or -1
    if (p < 0) return -1;
    let i = p >> 5;
    let m = (a[o0 + i] | a[o1 + i] | a[o2 + i]) & (-1 >>> (31 - (p & 31)));
    while (!m) { if (--i < 0) return -1; m = a[o0 + i] | a[o1 + i] | a[o2 + i]; }
    return i * 32 + 31 - Math.clz32(m);
  }

  wallSteps(i) {  // steps until particle i's block first holds a wall, or it steps out of the band
    const x = this.x[i], y = this.y[i], u2 = wrap(x + y, this.U2), vv = x - y - this.lo + PAD, d = this.d[i], U2 = this.U2;
    if (d === 0 || d === 3) {
      const o0 = (vv - 1) * this.RWV, o1 = o0 + this.RWV, o2 = o1 + this.RWV, nw = (U2 + 31) >> 5, wv = this.wv;
      let q;
      if (d === 0) {
        q = World.scanFwd(wv, o0, o1, o2, u2 + 1, nw);
        if (q >= U2) q = -1;
        if (q < 0) q = World.scanFwd(wv, o0, o1, o2, 0, nw);
        if (q < 0 || q >= U2) return INF;
        return (wrap(q - u2, U2) - 1) >> 1;
      }
      q = World.scanBwd(wv, o0, o1, o2, u2 - 1);
      if (q < 0) q = World.scanBwd(wv, o0, o1, o2, U2 - 1);
      if (q < 0) return INF;
      return (wrap(u2 - q, U2) - 1) >> 1;
    }
    const ua = u2 ? u2 - 1 : U2 - 1, ub = u2 + 1 === U2 ? 0 : u2 + 1, R = this.RWU, wu = this.wu;
    if (d === 2) {
      const exit = (this.hi - (x - y)) >> 1, q = World.scanFwd(wu, ua * R, u2 * R, ub * R, vv + 1, R);
      return q < 0 ? exit : Math.min(exit, (q - vv - 1) >> 1);
    }
    const exit = ((x - y) - this.lo) >> 1, q = World.scanBwd(wu, ua * R, u2 * R, ub * R, vv - 1);
    return q < 0 ? exit : Math.min(exit, (vv - q - 1) >> 1);
  }

  hpush(t, id) {
    if (this.hn === this.hcap) {
      this.hcap *= 2;
      const ht = new Int32Array(this.hcap), hid = new Int32Array(this.hcap), hver = new Int32Array(this.hcap);
      ht.set(this.ht); hid.set(this.hid); hver.set(this.hver); this.ht = ht; this.hid = hid; this.hver = hver;
    }
    const ht = this.ht, hid = this.hid, hver = this.hver;
    let k = this.hn++;
    while (k > 0) {
      const p = (k - 1) >> 1;
      if (ht[p] <= t) break;
      ht[k] = ht[p]; hid[k] = hid[p]; hver[k] = hver[p]; k = p;
    }
    ht[k] = t; hid[k] = id; hver[k] = this.ver[id];
  }

  hpop() {
    const n = --this.hn, ht = this.ht, hid = this.hid, hver = this.hver;
    if (!n) return;
    const t = ht[n], id = hid[n], ver = hver[n];
    let k = 0;
    while (true) {
      let c = 2 * k + 1;
      if (c >= n) break;
      if (c + 1 < n && ht[c + 1] < ht[c]) c++;
      if (ht[c] >= t) break;
      ht[k] = ht[c]; hid[k] = hid[c]; hver[k] = hver[c]; k = c;
    }
    ht[k] = t; hid[k] = id; hver[k] = ver;
  }

  keysOf(i, key) {
    const x = this.x[i], y = this.y[i], U = this.U, tm = wrap(this.t0[i] % U, U), k0 = 3 * this.d[i];
    for (let j = 0; j < 3; j++) {
      switch (KIND[k0 + j]) {
        case 0: key[j] = wrap(x - tm, U); break;
        case 1: key[j] = wrap(y - tm, U); break;
        case 2: key[j] = wrap(x + tm, U); break;
        case 3: key[j] = wrap(y + tm, U); break;
        case 4: key[j] = x - y - this.lo; break;
        default: key[j] = wrap(x + y, this.U2);
      }
    }
  }

  indexAdd(i, key) {
    const head = this.head, nbm = this.nbm, nxt = this.nxt, prv = this.prv, bk = this.bk, cap = this.cap, k0 = 3 * this.d[i];
    for (let j = 0; j < 3; j++) {
      const kind = KIND[k0 + j], b = key[j] >> 1, hb = this.hoff[kind] + b, h = head[hb], o = j * cap;
      bk[o + i] = b; prv[o + i] = -1; nxt[o + i] = h;
      if (h >= 0) prv[o + h] = i;
      head[hb] = i;
      nbm[this.boff[kind] + (b >> 5)] |= 1 << (b & 31);
    }
  }

  indexDel(i) {
    const head = this.head, nxt = this.nxt, prv = this.prv, cap = this.cap, k0 = 3 * this.d[i];
    for (let j = 0; j < 3; j++) {
      const kind = KIND[k0 + j], o = j * cap, p = prv[o + i], n = nxt[o + i], b = this.bk[o + i];
      if (p >= 0) nxt[o + p] = n;
      else { head[this.hoff[kind] + b] = n; if (n < 0) this.nbm[this.boff[kind] + (b >> 5)] &= ~(1 << (b & 31)); }
      if (n >= 0) prv[o + n] = p;
    }
  }

  static sameBlock(t, xa, ya, xb, yb) {
    const o = par(t);
    return ((xa - o) >> 1) === ((xb - o) >> 1) && ((ya - o) >> 1) === ((yb - o) >> 1);
  }

  // tau + s if i (at xi, yi, moving dxi, dyi at tau) and j (shifted by -k laps) share a block at step tau + s
  static tryStep(tau, s, k, U, xi, yi, dxi, dyi, xj, yj, dxj, dyj) {
    if (s < 0) return INF;
    return World.sameBlock(tau + s, xi + dxi * s, yi + dyi * s, xj + dxj * s - k * U, yj + dyj * s - k * U) ? tau + s : INF;
  }

  // earliest step >= both particles' times at which i and j share a block (flying freely until then), or INF
  meet(i, j) {
    const U = this.U, ti = this.t0[i], tj = this.t0[j], tau = Math.max(ti, tj), di = this.d[i], dj = this.d[j];
    const dxi = DX[di], dyi = DY[di], dxj = DX[dj], dyj = DY[dj];
    const xi = this.x[i] + dxi * (tau - ti), yi = this.y[i] + dyi * (tau - ti), xj = this.x[j] + dxj * (tau - tj), yj = this.y[j] + dyj * (tau - tj);
    const Dx = xi - xj, Dy = yi - yj, vx = dxi - dxj, vy = dyi - dyj;
    let best = INF;
    if (vx === 0 && vy === 0) {  // same direction: fixed offset; a shared block now or at the other parity
      let k = 0, r = Dx;
      while (r > U / 2) { r -= U; k--; }
      while (r < -U / 2) { r += U; k++; }
      if (Dx + k * U > 1 || Dx + k * U < -1 || Dy + k * U > 1 || Dy + k * U < -1) return INF;
      best = World.tryStep(tau, 0, k, U, xi, yi, dxi, dyi, xj, yj, dxj, dyj);
      return best !== INF ? best : World.tryStep(tau, 1, k, U, xi, yi, dxi, dyi, xj, yj, dxj, dyj);
    }
    if (vx === 0 || vy === 0) {  // perpendicular: the shared coordinate fixes the wrap, the other the step
      const Df = vx === 0 ? Dx : Dy, Dm = vx === 0 ? Dy : Dx, vm = vx === 0 ? vy : vx;
      let k = 0, r = Df;
      while (r > U / 2) { r -= U; k--; }
      while (r < -U / 2) { r += U; k++; }
      if (Df + k * U > 1 || Df + k * U < -1) return INF;
      for (let wv = -1; wv <= 1; wv++) {
        const num = wv - Dm - k * U;
        if (num & 1) continue;
        const t = World.tryStep(tau, num / vm, k, U, xi, yi, dxi, dyi, xj, yj, dxj, dyj);
        if (t < best) best = t;
      }
      return best;
    }
    for (let wx = -1; wx <= 1; wx++) {  // head on: the first lap at which x lines up, then y must too
      const num = wx - Dx;
      if (num & 1) continue;
      let k = 0, r = num;
      if (vx > 0) { while (r < 0) { r += U; k--; } while (r >= U) { r -= U; k++; } }
      else { while (r > 0) { r -= U; k++; } while (r <= -U) { r += U; k--; } }
      for (let lap = 0; lap <= (U % 4 ? 1 : 0); lap++, k += vx > 0 ? -1 : 1) {
        const s = (num - k * U) / vx, yy = Dy + k * U + vy * s;
        if (yy <= 1 && yy >= -1) { const t = World.tryStep(tau, s, k, U, xi, yi, dxi, dyi, xj, yj, dxj, dyj); if (t < best) best = t; }
      }
    }
    return best;
  }

  schedule(i) {  // next event of i from its (x, y, t0, d); lowers partners' events as needed
    const ws = this.wallSteps(i), key = this.key, d = this.d[i], dd = this.d, nxt = this.nxt, cap = this.cap, ev = this.ev;
    let best = ws === INF ? INF : this.t0[i] + ws;
    this.keysOf(i, key);
    for (let j = 0; j < 3; j++) {
      const kind = KIND[3 * d + j], nb = this.nb[kind], range = kind >= 4 ? 2 : 1, ho = this.hoff[kind], bo = this.boff[kind];
      let prevb = -1;
      for (let dk = -range; dk <= range; dk++) {
        let kk = key[j] + dk;
        if (kind === 4) { if (kk < 0 || kk >= this.V2) continue; }
        else kk = wrap(kk, kind === 5 ? this.U2 : this.U);
        const b = kk >> 1;
        if (b === prevb || b >= nb) continue;
        prevb = b;
        if (!((this.nbm[bo + (b >> 5)] >>> (b & 31)) & 1)) continue;
        for (let c = this.head[ho + b]; c >= 0; c = nxt[SLOT[6 * dd[c] + kind] * cap + c]) {
          if (c === i || (kind < 4 && dd[c] === d)) continue;
          const t = this.meet(i, c);
          if (t === INF) continue;
          if (t < best) best = t;
          if (t < ev[c]) { ev[c] = t; this.hpush(t, c); }
        }
      }
    }
    this.indexAdd(i, key);
    this.ev[i] = best;
    if (best !== INF) this.hpush(best, i);
  }

  setDir(i) {
    const o = par(this.t0[i]);
    this.d[i] = ((this.x[i] - o) & 1) + 2 * ((this.y[i] - o) & 1);
  }

  canon(i) {
    while (this.x[i] + this.y[i] < 0) { this.x[i] += this.U; this.y[i] += this.U; }
    while (this.x[i] + this.y[i] >= this.U2) { this.x[i] -= this.U; this.y[i] -= this.U; }
  }

  occupied(x, y, t) {  // is cell (x, y) occupied at step t (all events before t done)?
    const v = x - y, u2 = wrap(x + y, this.U2);
    for (let i = 0; i < this.top; i++) {
      if (!this.alive[i]) continue;
      const dt = t - this.t0[i], xi = this.x[i] + DX[this.d[i]] * dt, yi = this.y[i] + DY[this.d[i]] * dt;
      if (xi - yi === v && wrap(xi + yi, this.U2) === u2) return 1;
    }
    return 0;
  }

  add(x, y, t) {  // a particle at (x, y) before step t (t = this.t for fresh ones)
    const v = x - y;
    if (v < this.lo || v > this.hi || this.isWall(x, y) || this.occupied(x, y, t)) return;
    let i;
    if (this.freeHead >= 0) { i = this.freeHead; this.freeHead = this.nxt[i]; }
    else { if (this.top === this.cap) this.grow(2 * this.cap); i = this.top++; }
    this.x[i] = x; this.y[i] = y; this.t0[i] = t; this.alive[i] = 1; this.n++;
    this.canon(i); this.setDir(i);
    this.schedule(i);
  }

  processBlock(T, ids, m) {
    const o = par(T), i0 = ids[0], vmaj = this.d[i0] === 0 || this.d[i0] === 3;
    let x0 = this.x[i0] + DX[this.d[i0]] * (T - this.t0[i0]), y0 = this.y[i0] + DY[this.d[i0]] * (T - this.t0[i0]);
    x0 -= (x0 - o) & 1; y0 -= (y0 - o) & 1;
    while (x0 + y0 < 0) { x0 += this.U; y0 += this.U; }
    while (x0 + y0 >= this.U2) { x0 -= this.U; y0 -= this.U; }
    const wl = this.wl, mv = this.mv, to = this.to;
    for (let c = 0; c < 4; c++) {
      wl[c] = vmaj ? this.isWallV(x0 + (c & 1), y0 + (c >> 1)) : this.isWall(x0 + (c & 1), y0 + (c >> 1));
      mv[c] = -1; to[c] = c;
    }
    for (let q = 0; q < m; q++) {
      const i = ids[q], dt = T - this.t0[i], x = this.x[i] + DX[this.d[i]] * dt, y = this.y[i] + DY[this.d[i]] * dt;
      mv[((x - o) & 1) + 2 * ((y - o) & 1)] = i;
    }
    const f0 = wl[0] | (mv[0] >= 0), f1 = wl[1] | (mv[1] >= 0), f2 = wl[2] | (mv[2] >= 0), f3 = wl[3] | (mv[3] >= 0), cnt = f0 + f1 + f2 + f3;
    if (cnt === 1) { to[0] = 3; to[1] = 2; to[2] = 1; to[3] = 0; }
    else if (cnt === 2 && f0 && f3) { to[0] = 1; to[3] = 2; }
    else if (cnt === 2 && f1 && f2) { to[1] = 0; to[2] = 3; }
    for (let c = 0; c < 4; c++) if (wl[c] && to[c] !== c) this.broken = true;
    for (let c = 0; c < 4; c++) {
      const i = mv[c];
      if (i < 0) continue;
      this.indexDel(i); this.ver[i]++;
      this.x[i] = x0 + (to[c] & 1); this.y[i] = y0 + (to[c] >> 1); this.t0[i] = T + 1;
      this.canon(i); this.setDir(i);
    }
    for (let c = 0; c < 4; c++) {
      const i = mv[c];
      if (i < 0) continue;
      const v = this.x[i] - this.y[i];
      if (v < this.lo || v > this.hi) { this.alive[i] = 0; this.n--; this.ver[i]++; this.nxt[i] = this.freeHead; this.freeHead = i; }
      else this.schedule(i);
    }
  }

  runTo(tEnd) {  // run all events at steps < tEnd
    const grp = this.grp, tmp = this.tmp, bx = this.bxs, by = this.bys, X = this.x, Y = this.y, T0 = this.t0, D = this.d, U = this.U, U2 = this.U2;
    while (this.hn && this.ht[0] < tEnd) {
      const T = this.ht[0], o = par(T);
      let m = 0;
      while (this.hn && this.ht[0] === T) {  // every live event at this step
        const id = this.hid[0], ver = this.hver[0];
        this.hpop();
        if (!this.alive[id] || ver !== this.ver[id] || this.mark[id] === T) continue;
        this.mark[id] = T;
        if (m === grp.length) { this.grp = new Int32Array(2 * m); this.grp.set(grp); this.bxs = new Int32Array(2 * m); this.bys = new Int32Array(2 * m); this.tmp = new Int32Array(2 * m); return this.runToResume(tEnd, T, m, grp); }
        grp[m] = id;
        let xa = X[id] + DX[D[id]] * (T - T0[id]), ya = Y[id] + DY[D[id]] * (T - T0[id]);  // its block's corner
        xa -= (xa - o) & 1; ya -= (ya - o) & 1;
        while (xa + ya < 0) { xa += U; ya += U; }
        while (xa + ya >= U2) { xa -= U; ya -= U; }
        bx[m] = xa; by[m] = ya; m++;
      }
      for (let q = 0; q < m; q++) {  // group by block
        if (grp[q] < 0) continue;
        let n = 0;
        tmp[n++] = grp[q]; grp[q] = -1;
        for (let r = q + 1; r < m; r++)
          if (grp[r] >= 0 && bx[r] === bx[q] && by[r] === by[q]) { tmp[n++] = grp[r]; grp[r] = -1; }
        this.processBlock(T, tmp, n);
      }
    }
    this.t = tEnd;
  }

  runToResume(tEnd, T, m, grp) {  // the scratch arrays just grew mid-step: put the popped ids back and go on
    for (let q = 0; q < m; q++) { this.mark[grp[q]] = -1; this.hpush(T, grp[q]); }
    return this.runTo(tEnd);
  }

  // Make absolute step T the new time 0 (or 1, keeping the step parity), move
  // every particle's reference point there and re-index (keys depend on t);
  // the lines and events are unchanged. Returns T in the new time. This keeps
  // every time and position a small integer.
  rebase(T) {
    const c = T - this.origin, sh = c - (c & 1), key = this.key;
    this.origin += sh;
    for (let i = 0; i < this.top; i++) {
      if (!this.alive[i]) continue;
      const dt = c - this.t0[i];
      this.indexDel(i);
      this.x[i] += DX[this.d[i]] * dt; this.y[i] += DY[this.d[i]] * dt; this.t0[i] = c - sh;
      this.canon(i);
      if (this.ev[i] !== INF) this.ev[i] -= sh;
      this.mark[i] = -1;
      this.keysOf(i, key); this.indexAdd(i, key);
    }
    for (let k = 0; k < this.hn; k++) this.ht[k] -= sh;
    this.t -= sh;
    return c - sh;
  }

  cells(out, off, t) {  // append ((x + y) / 2, x - y - lo) of every particle at step t as uint16 pairs (web.py's format)
    for (let i = 0; i < this.top; i++) {
      if (!this.alive[i]) continue;
      const dt = t - this.t0[i], x = this.x[i] + DX[this.d[i]] * dt, y = this.y[i] + DY[this.d[i]] * dt;
      out[off++] = wrap(x + y, this.U2) >> 1; out[off++] = x - y - this.lo;
    }
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
      const vv = squares[j + 1] + PAD, a = u - 1;  // top-left cell has x + y = u - 1
      for (const [du, dv] of [[0, 0], [1, 1], [1, -1], [2, 0]]) w.addWall((a + du + meta.U2) % meta.U2, vv + dv);
    }
    const sc = meta.sched;
    this.ft = Int32Array.from(sc, e => e[0]); this.fx = Int32Array.from(sc, e => e[1]); this.fy = Int32Array.from(sc, e => e[2]);
    this.fg = Int8Array.from(sc, e => e[3]);
    const rs = [];
    for (const [name, [t, cells]] of Object.entries(meta.reads))
      for (const [x, y] of cells) rs.push([t, x, y, name]);
    rs.sort((a, b) => a[0] - b[0]);
    this.rt = Int32Array.from(rs, e => e[0]); this.rx = Int32Array.from(rs, e => e[1]); this.ry = Int32Array.from(rs, e => e[2]); this.rn = rs.map(e => e[3]);
    this.n = 0; w.t = meta.t_lo;
  }

  // Run frame n with inputs {left, right, reset}. traces: [[T, K], ...] (T
  // relative to the frame's start, sorted, K <= T): the cells after each of
  // the K steps up to T are returned like web.py's pictures.
  frame(inp, traces = []) {
    const w = this.w, start = w.rebase(this.meta.t_lo + this.n * this.P), end = start + this.P, gate = [1, inp.left, inp.right, inp.reset];
    const counts = {}, pics = traces.map(([T, K]) => ({ ph: T, counts: [], raw: new Uint16Array(2 * K * (w.n + 2000)), off: 0, K }));
    let fi = 0, ri = 0, ti = 0;
    const nf = this.ft.length, nr = this.rt.length;
    while (true) {  // the times anything happens from outside: feeds, reads, recorded steps
      let t = end;
      if (fi < nf && this.ft[fi] + start < t) t = this.ft[fi] + start;
      if (ri < nr && this.rt[ri] + start < t) t = this.rt[ri] + start;
      while (ti < pics.length && pics[ti].ph + start <= w.t) ti++;  // pictures fully recorded
      if (ti < pics.length) t = Math.min(t, Math.max(w.t + 1, pics[ti].ph - pics[ti].K + 1 + start));  // next step to record
      w.runTo(t);
      if (t === end) break;
      for (; fi < nf && this.ft[fi] + start === t; fi++) if (gate[this.fg[fi]]) w.add(this.fx[fi], this.fy[fi], t);
      for (; ri < nr && this.rt[ri] + start === t; ri++) counts[this.rn[ri]] = (counts[this.rn[ri]] || 0) + w.occupied(this.rx[ri], this.ry[ri], t);
      for (let j = ti; j < pics.length && pics[j].ph - pics[j].K + 1 + start <= t; j++) {  // the pictures that record step t
        const q = pics[j];
        if (t > q.ph + start) continue;
        if (q.off + 2 * w.n > q.raw.length) { const b = new Uint16Array(2 * q.raw.length + 2 * w.n); b.set(q.raw); q.raw = b; }
        q.counts.push(w.n); q.off = w.cells(q.raw, q.off, t);
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
// The machine runs as WebAssembly (web/wasm.js) by default, on the GPU (web/gpu.js) with ?gpu, in JavaScript (Machine above) with ?js.
if (typeof WorkerGlobalScope !== 'undefined' && self instanceof WorkerGlobalScope) {
  const PICS = 3, CLIP = 16, CLIP_EVERY = 0.3, FPS_CAP = 60;
  let M = null, engine = '', keys = { left: 0, right: 0, reset: 1 }, sweep = 8, phase = 0, fps = 0, period = 0, lastClip = -1;
  let tPrev = null, tNext = 0, paused = false, timer = null, busy = false;
  // setTimeout(0) is clamped to 4 ms in a worker (a quarter of a frame); a message to ourselves is not, and still lets key messages in
  const soon = new MessageChannel(), queued = () => { soon.port2.postMessage(0); return -1; };
  soon.port1.onmessage = () => { timer = null; loop(); };
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
  const loop = async () => {
    busy = true;
    try {
      const t0 = now();
      tNext = Math.max(tNext + 1 / FPS_CAP, t0);
      const inp = { ...keys }, n = M.n;
      keys.reset = 0;
      const r = await M.frame(inp, pictureTimes()), ms = 1000 * (now() - t0);
      if (tPrev !== null) { period = fps ? .8 * period + .2 * (t0 - tPrev) : t0 - tPrev; fps = 1 / period; }
      tPrev = t0;
      postMessage({ type: 'frame', n, state: r.state, win: r.win, lose: r.lose, inp, ms: Math.round(ms * 10) / 10, fps: Math.round(fps * 100) / 100, engine,
                    gpuMs: r.gpuMs == null ? null : Math.round(r.gpuMs * 10) / 10 });
      postMessage({ type: 'pics', n, P: M.P, sweep, pics: r.pics }, r.pics.map(q => q.raw.buffer));
      const wait = 1000 * (tNext - now());
      timer = paused ? null : wait > 1 ? setTimeout(loop, wait) : queued();
    } catch (e) {
      postMessage({ type: 'fault', error: String(e) });
    }
    busy = false;
  };
  const start = async d => {
    if (d.gpu) {  // opt-in: the GPU engine (web/gpu.js) is experimental
      try {
        importScripts('gpu.js');
        M = await new GpuMachine(d.meta, d.squares, { spin: d.spin, tiles: d.tiles, epoch: d.epoch }).init();
        engine = 'GPU · ' + M.info + (d.spin ? ` · ${d.spin} spinners` : '');
      } catch (e) { M = null; engine = 'CPU (no WebGPU: ' + String(e).replace(/^Error: /, '') + ')'; }
    } else if (!d.js) {  // the C engine as WebAssembly, about twice the speed of Machine; threads when the page is cross-origin isolated
      try {
        const WASM_JS = 'wasm.js';
        importScripts(WASM_JS);
        const shared = typeof SharedArrayBuffer !== 'undefined' && self.crossOriginIsolated;
        const threads = shared ? Math.max(1, Math.min(16, d.threads || navigator.hardwareConcurrency || 1)) : 1;
        const bytes = await (await fetch(threads > 1 ? 'bbmt.wasm' : 'bbm.wasm')).arrayBuffer();
        M = await new WasmMachine(d.meta, d.squares, bytes, { threads, url: WASM_JS, epoch: d.epoch || 96 }).init();
        engine = 'CPU (WebAssembly, ' + (threads > 1 ? threads + ' threads)' : shared ? '1 thread)' : '1 thread; no SharedArrayBuffer)');
      } catch (e) { M = null; engine = 'CPU (JavaScript; no WebAssembly: ' + String(e).replace(/^Error: /, '') + ')'; }
    } else engine = 'CPU (JavaScript)';
    if (!M) M = new Machine(d.meta, d.squares);
    tNext = now(); loop();
  };
  onmessage = e => {
    const d = e.data;
    if (d.type === 'init') start(d);
    else if (d.type === 'control') {
      for (const k of ['left', 'right']) if (k in d) keys[k] = d[k] ? 1 : 0;
      if (d.reset) keys.reset = 1;
      if (typeof d.sweep === 'number') sweep = Math.max(0, Math.min(600, d.sweep));
      if ('pause' in d) {  // hidden tab: stop computing
        paused = !!d.pause;
        if (!paused && !timer && !busy) { tNext = now(); tPrev = null; loop(); }
      }
    }
  };
}
if (typeof module !== 'undefined') module.exports = { World, Machine };
