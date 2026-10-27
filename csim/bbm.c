/* Exact BBMCA on the diagonal cylinder, same rule as cyl.py (which it is
 * tested against), simulated event by event: free flight is not stepped.
 *
 * A lone particle moves one cell diagonally per step until its 2x2 block
 * holds something else, so every particle is kept as a straight line: its
 * position at the time of its last event and its direction. Its next event
 * is the first step at which its block is not lone:
 *  - a wall: the three cells in front of it sweep three adjacent rows of a
 *    bitmap; walls are kept in two orientations (rows of constant x - y for
 *    the SE/NW movers, rows of constant x + y for NE/SW) so that is a
 *    word-wise scan;
 *  - another particle: two particles can only share a block if the
 *    coordinate their motions keep in common is within 1 (x - t for two
 *    movers with dx = +1, y - t for dy = +1, x + t, y + t), or x - y (SE vs
 *    NW) / x + y (NE vs SW) within 2. Particles are indexed by those six
 *    keys, so the candidates are a few bucket lookups and the meeting step
 *    is solved in closed form.
 * Events are popped from a heap, grouped by block, and the Margolus rule is
 * applied to the block (walls included); the particles involved are then
 * rescheduled. Anything that steps outside the band is dropped (the sinks).
 * Reads (is this cell occupied at step t) and feeds (add a particle before
 * step t) are served after all events before t have run, by extrapolation. */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define PAD 2
#define INF INT64_MAX
#define CQ 65536  /* calendar queue span in steps (a power of two) */
#define CQREF 4096 /* the far list is refiled when cbase crosses a multiple of this */
/* entries at or after this step live on the far list; the split moves only at refiles, so a
 * bucket entry is always earlier than every far entry */
#define CQLIM(w) (((w)->cbase & ~(int64_t)(CQREF - 1)) + CQ)
#define NKIND 6

static const int DX[4] = {1, -1, 1, -1}, DY[4] = {1, 1, -1, -1};   /* d = rx + 2*ry: SE SW NE NW */
static const int KIND[4][3] = {{0, 1, 4}, {2, 1, 5}, {0, 3, 5}, {2, 3, 4}};  /* keys each direction is indexed by */
static const int SLOT[4][NKIND] = {{0, 1, -1, -1, 2, -1}, {-1, 1, 0, -1, -1, 2}, {0, -1, -1, 1, -1, 2}, {-1, -1, 0, 1, 2, -1}};  /* KIND inverted */

typedef struct World_ {
    int64_t U, U2, lo, hi, V2, S, RWU, RWV;
    uint64_t *wu;   /* walls, rows of constant u2 = x + y: word u2 * RWU + (vv >> 6), bit vv & 63; vv = x - y - lo + PAD */
    uint64_t *wv;   /* walls, rows of constant vv: word vv * RWV + (u2 >> 6), bit u2 & 63 */
    /* particles: ids are stable, freed ids are reused */
    int32_t top, cap, nalive, free_head;
    int64_t *x, *y, *t0, *ev;
    int8_t *d, *alive;
    int32_t *ver, *mark;
    int32_t *nxt[3], *prv[3], *bk[3];
    int32_t *head[NKIND]; uint64_t *nbm[NKIND]; int64_t nb[NKIND];   /* bucket heads and a bit per non-empty bucket */
    /* calendar queue of (time, id, version): a ring of CQ time buckets of entry lists,
     * plus an overflow list for events further ahead than the ring spans */
    int64_t *ct; int32_t *cid, *cver, *cnext; int32_t cfree, ccap; int64_t cn;
    int32_t *cbuck; int64_t cbase;      /* cbuck[t % CQ]: first entry due at step t (t in [cbase, CQLIM)) */
    int32_t cfar;                       /* entries due at or after CQLIM */
    int32_t *grp; int64_t gcap;
    int64_t t;
    int broken;
    /* tiles (world_run_tiles): private copies sharing the walls; a tile trusts
     * only blocks the window edges cannot have reached yet */
    int tiled, shared_walls; int64_t wlo, whi, tbase;
    struct World_ **tiles; int ntiles;
    struct TP_ *pool;                                    /* the tile threads (world_run_tiles), parked between calls */
    int64_t *kx, *ky; int64_t nk, kcap;   /* a tile's kept particles after an epoch (written) ... */
    int64_t *px, *py; int64_t np, pcap;   /* ... and after the previous one (read by the neighbours): swapped between epochs */
    double ptime; int64_t pballs;          /* a tile's time and own-ball count since the last recut (for the weights of the next) */
    int32_t *tuv; int64_t tn, tcap; int64_t *tcnt, tcc, ntc; int64_t toff;   /* a tile's recorded balls this epoch (world_run_tiles traces) */
    int64_t *gx, *gy; int32_t *gid; uint8_t *gm; int64_t gcap2;   /* a tile's ghosts at the start of an epoch (scratch) */

} World;

static inline int64_t mod(int64_t a, int64_t m) { int64_t r = a % m; return r < 0 ? r + m : r; }
static inline int64_t wrap(int64_t a, int64_t m) { while (a < 0) a += m; while (a >= m) a -= m; return a; }  /* mod for a near [0, m) */
static inline int64_t imin(int64_t a, int64_t b) { return a < b ? a : b; }

World *world_new(int64_t U, int64_t lo, int64_t hi) {
    World *w = calloc(1, sizeof(World));
    w->U = U; w->U2 = 2 * U; w->lo = lo; w->hi = hi; w->V2 = hi - lo + 1;
    w->S = w->V2 + 2 * PAD;
    w->RWU = (w->S + 63) / 64 + 1; w->RWV = (w->U2 + 63) / 64 + 1;
    w->wu = calloc(w->U2 * w->RWU, 8);
    w->wv = calloc(w->S * w->RWV, 8);
    w->nb[0] = w->nb[1] = w->nb[2] = w->nb[3] = U / 2 + 1;
    w->nb[4] = w->V2 / 2 + 3;
    w->nb[5] = U + 1;
    for (int k = 0; k < NKIND; k++) {
        w->head[k] = malloc(w->nb[k] * 4);
        memset(w->head[k], -1, w->nb[k] * 4);
        w->nbm[k] = calloc(w->nb[k] / 64 + 1, 8);
    }
    w->free_head = -1;
    return w;
}

static void pool_free(struct TP_ *M);

void world_free(World *w) {
    for (int k = 0; k < w->ntiles; k++) world_free(w->tiles[k]);
#ifndef BBM_NO_THREADS
    if (w->pool) pool_free(w->pool);
#endif
    free(w->tiles); free(w->kx); free(w->ky); free(w->px); free(w->py); free(w->tuv); free(w->tcnt); free(w->gx); free(w->gy); free(w->gid); free(w->gm);
    if (!w->shared_walls) { free(w->wu); free(w->wv); }
    for (int k = 0; k < NKIND; k++) { free(w->head[k]); free(w->nbm[k]); }
    for (int j = 0; j < 3; j++) { free(w->nxt[j]); free(w->prv[j]); free(w->bk[j]); }
    free(w->x); free(w->y); free(w->t0); free(w->ev); free(w->d); free(w->alive); free(w->ver); free(w->mark);
    free(w->ct); free(w->cid); free(w->cver); free(w->cnext); free(w->cbuck); free(w->grp); free(w);
}

static void grow(World *w, int32_t need) {
    if (need <= w->cap) return;
    int32_t c = w->cap ? w->cap : 1024;
    while (c < need) c *= 2;
    w->x = realloc(w->x, c * 8); w->y = realloc(w->y, c * 8); w->t0 = realloc(w->t0, c * 8); w->ev = realloc(w->ev, c * 8);
    w->d = realloc(w->d, c); w->alive = realloc(w->alive, c);
    w->ver = realloc(w->ver, c * 4); w->mark = realloc(w->mark, c * 4);
    for (int j = 0; j < 3; j++) { w->nxt[j] = realloc(w->nxt[j], c * 4); w->prv[j] = realloc(w->prv[j], c * 4); w->bk[j] = realloc(w->bk[j], c * 4); }
    memset(w->alive + w->cap, 0, c - w->cap);
    memset(w->ver + w->cap, 0, (c - w->cap) * 4);
    memset(w->mark + w->cap, -1, (c - w->cap) * 4);
    w->cap = c;
}

/* ------------------------------------------------------------------ walls */
static inline int is_wall(World *w, int64_t x, int64_t y) {
    int64_t v = x - y;
    if (v < w->lo || v > w->hi) return 0;
    int64_t u2 = wrap(x + y, w->U2), vv = v - w->lo + PAD;
    return (w->wu[u2 * w->RWU + (vv >> 6)] >> (vv & 63)) & 1;
}

static inline int is_wall_v(World *w, int64_t x, int64_t y) {  /* same, from the other bitmap */
    int64_t v = x - y;
    if (v < w->lo || v > w->hi) return 0;
    int64_t u2 = wrap(x + y, w->U2), vv = v - w->lo + PAD;
    return (w->wv[vv * w->RWV + (u2 >> 6)] >> (u2 & 63)) & 1;
}

void world_add(World *w, int64_t n, const int64_t *xy, int wall);

/* first set bit at position >= p in r0|r1|r2 (nw words), or -1 */
static inline int64_t scan_fwd(const uint64_t *r0, const uint64_t *r1, const uint64_t *r2, int64_t p, int64_t nw) {
    int64_t i = p >> 6;
    if (i >= nw) return -1;
    uint64_t m = (r0[i] | r1[i] | r2[i]) & (~0ULL << (p & 63));
    while (!m) { if (++i >= nw) return -1; m = r0[i] | r1[i] | r2[i]; }
    return i * 64 + __builtin_ctzll(m);
}

/* last set bit at position <= p, or -1 */
static inline int64_t scan_bwd(const uint64_t *r0, const uint64_t *r1, const uint64_t *r2, int64_t p) {
    if (p < 0) return -1;
    int64_t i = p >> 6;
    uint64_t m = (r0[i] | r1[i] | r2[i]) & (~0ULL >> (63 - (p & 63)));
    while (!m) { if (--i < 0) return -1; m = r0[i] | r1[i] | r2[i]; }
    return i * 64 + 63 - __builtin_clzll(m);
}

/* steps until particle i's block first holds a wall, or it steps out of the band */
static int64_t wall_steps(World *w, int32_t i) {
    int64_t x = w->x[i], y = w->y[i], u2 = wrap(x + y, w->U2), vv = x - y - w->lo + PAD;
    int d = w->d[i];
    if (d == 0 || d == 3) {  /* SE / NW: along a row of constant v, wrapping round the loop */
        const uint64_t *r0 = w->wv + (vv - 1) * w->RWV, *r1 = r0 + w->RWV, *r2 = r1 + w->RWV;
        int64_t nw = (w->U2 + 63) / 64, q;
        if (d == 0) {
            q = scan_fwd(r0, r1, r2, u2 + 1, nw);
            if (q >= w->U2) q = -1;
            if (q < 0) q = scan_fwd(r0, r1, r2, 0, nw);
            if (q < 0 || q >= w->U2) return INF;
            return (wrap(q - u2, w->U2) - 1) >> 1;
        }
        q = scan_bwd(r0, r1, r2, u2 - 1);
        if (q < 0) q = scan_bwd(r0, r1, r2, w->U2 - 1);
        if (q < 0) return INF;
        return (wrap(u2 - q, w->U2) - 1) >> 1;
    }
    int64_t ua = u2 ? u2 - 1 : w->U2 - 1, ub = u2 + 1 == w->U2 ? 0 : u2 + 1;
    const uint64_t *r0 = w->wu + ua * w->RWU, *r1 = w->wu + u2 * w->RWU, *r2 = w->wu + ub * w->RWU;
    if (d == 2) {  /* NE: v rises; leaves the band after (hi - v) / 2 steps */
        int64_t exit = (w->hi - (x - y)) >> 1, q = scan_fwd(r0, r1, r2, vv + 1, w->RWU);
        return q < 0 ? exit : imin(exit, (q - vv - 1) >> 1);
    }
    int64_t exit = ((x - y) - w->lo) >> 1, q = scan_bwd(r0, r1, r2, vv - 1);  /* SW */
    return q < 0 ? exit : imin(exit, (vv - q - 1) >> 1);
}

/* ------------------------------------------------------------------ calendar queue */
static void cq_init(World *w) {
    if (w->cbuck) return;
    w->cbuck = malloc(CQ * 4); memset(w->cbuck, -1, CQ * 4);
    w->cfar = -1; w->cfree = -1; w->cbase = w->t;
}

static inline int32_t cq_alloc(World *w) {
    if (w->cfree >= 0) { int32_t e = w->cfree; w->cfree = w->cnext[e]; return e; }
    if (w->cn == w->ccap) {
        w->ccap = w->ccap ? 2 * w->ccap : 4096;
        w->ct = realloc(w->ct, w->ccap * 8); w->cid = realloc(w->cid, w->ccap * 4);
        w->cver = realloc(w->cver, w->ccap * 4); w->cnext = realloc(w->cnext, w->ccap * 4);
    }
    return (int32_t)w->cn++;
}

static void hpush(World *w, int64_t t, int32_t id) {
    cq_init(w);
    int32_t e = cq_alloc(w);
    w->ct[e] = t; w->cid[e] = id; w->cver[e] = w->ver[id];
    if (t < CQLIM(w)) { int32_t *b = &w->cbuck[t & (CQ - 1)]; w->cnext[e] = *b; *b = e; }
    else { w->cnext[e] = w->cfar; w->cfar = e; }
}

/* move the ring's base to step t (t >= cbase): buckets passed are empty by construction */
static void cq_advance(World *w, int64_t t) {
    if (t < w->cbase) return;
    if (t - w->cbase >= CQ) {   /* a long idle stretch: refile everything */
        int32_t all = w->cfar; w->cfar = -1;
        for (int64_t k = 0; k < CQ; k++) if (w->cbuck[k] >= 0) {  /* (empty in practice) */
            int32_t e = w->cbuck[k]; w->cbuck[k] = -1;
            while (e >= 0) { int32_t n = w->cnext[e]; w->cnext[e] = all; all = e; e = n; }
        }
        w->cbase = t;
        while (all >= 0) {
            int32_t n = w->cnext[all];
            if (w->ct[all] < CQLIM(w)) { int32_t *b = &w->cbuck[w->ct[all] & (CQ - 1)]; w->cnext[all] = *b; *b = all; }
            else { w->cnext[all] = w->cfar; w->cfar = all; }
            all = n;
        }
        return;
    }
    while (w->cbase < t) {   /* refile far entries that come into the span */
        w->cbase++;
        if (w->cfar >= 0 && (w->cbase & (CQREF - 1)) == 0) {  /* the split moves: refile */
            int32_t e = w->cfar, keep = -1;
            while (e >= 0) {
                int32_t n = w->cnext[e];
                if (w->ct[e] < CQLIM(w)) { int32_t *b = &w->cbuck[w->ct[e] & (CQ - 1)]; w->cnext[e] = *b; *b = e; }
                else { w->cnext[e] = keep; keep = e; }
                e = n;
            }
            w->cfar = keep;
        }
    }
}

/* the earliest step with an entry at or before t_end - 1, or INF (scanning from cbase) */
static int64_t cq_next(World *w, int64_t t_end) {
    cq_init(w);
    for (int64_t t = w->cbase; t < t_end && t < CQLIM(w); t++)
        if (w->cbuck[t & (CQ - 1)] >= 0) return t;
    if (w->cfar >= 0) {   /* nothing in the span: the far list may hold the next one */
        int64_t best = INF; int32_t e = w->cfar;
        while (e >= 0) { if (w->ct[e] < best) best = w->ct[e]; e = w->cnext[e]; }
        if (best < t_end) { cq_advance(w, best); return best; }
    }
    return INF;
}

/* ------------------------------------------------------------------ index */
/* the three keys of particle i (its position is canonical, so one division does) */
static void keys_of(World *w, int32_t i, int64_t key[3]) {
    int64_t x = w->x[i], y = w->y[i], tm = mod(w->t0[i], w->U), U = w->U;
    for (int j = 0; j < 3; j++) {
        switch (KIND[w->d[i]][j]) {
            case 0: key[j] = wrap(x - tm, U); break;
            case 1: key[j] = wrap(y - tm, U); break;
            case 2: key[j] = wrap(x + tm, U); break;
            case 3: key[j] = wrap(y + tm, U); break;
            case 4: key[j] = x - y - w->lo; break;
            default: key[j] = wrap(x + y, w->U2);
        }
    }
}

static void index_add(World *w, int32_t i, const int64_t key[3]) {
    for (int j = 0; j < 3; j++) {
        int kind = KIND[w->d[i]][j];
        int32_t b = (int32_t)(key[j] >> 1), h = w->head[kind][b];
        w->bk[j][i] = b; w->prv[j][i] = -1; w->nxt[j][i] = h;
        if (h >= 0) w->prv[j][h] = i;
        w->head[kind][b] = i;
        w->nbm[kind][b >> 6] |= 1ULL << (b & 63);
    }
}

static void index_del(World *w, int32_t i) {
    for (int j = 0; j < 3; j++) {
        int kind = KIND[w->d[i]][j];
        int32_t p = w->prv[j][i], n = w->nxt[j][i], b = w->bk[j][i];
        if (p >= 0) w->nxt[j][p] = n;
        else { w->head[kind][b] = n; if (n < 0) w->nbm[kind][b >> 6] &= ~(1ULL << (b & 63)); }
        if (n >= 0) w->prv[j][n] = p;
    }
}

/* ------------------------------------------------------------------ meetings */
static inline int same_block(int64_t t, int64_t xa, int64_t ya, int64_t xb, int64_t yb) {
    int64_t o = t & 1;
    return ((xa - o) >> 1) == ((xb - o) >> 1) && ((ya - o) >> 1) == ((yb - o) >> 1);
}

/* earliest step >= both particles' times at which i and j share a block
 * (assuming both fly freely until then), or INF */
static int64_t meet(World *w, int32_t i, int32_t j) {
    const int64_t U = w->U;
    int64_t ti = w->t0[i], tj = w->t0[j], tau = ti > tj ? ti : tj;
    int di = w->d[i], dj = w->d[j];
    int64_t xi = w->x[i] + DX[di] * (tau - ti), yi = w->y[i] + DY[di] * (tau - ti);
    int64_t xj = w->x[j] + DX[dj] * (tau - tj), yj = w->y[j] + DY[dj] * (tau - tj);
    int64_t Dx = xi - xj, Dy = yi - yj;
    int vx = DX[di] - DX[dj], vy = DY[di] - DY[dj];
    int64_t best = INF;
#define TEST(s, k) do { int64_t _s = (s), _k = (k); if (_s >= 0 && tau + _s < best && \
        same_block(tau + _s, xi + DX[di] * _s, yi + DY[di] * _s, xj + DX[dj] * _s - _k * U, yj + DY[dj] * _s - _k * U)) best = tau + _s; } while (0)
    if (vx == 0 && vy == 0) {  /* same direction: fixed offset; a shared block now or at the other parity */
        int64_t k = 0, r = Dx;
        while (r > U / 2) { r -= U; k--; }
        while (r < -U / 2) { r += U; k++; }
        if (Dx + k * U > 1 || Dx + k * U < -1 || Dy + k * U > 1 || Dy + k * U < -1) return INF;
        TEST(0, k); TEST(1, k);
        return best;
    }
    if (vx == 0 || vy == 0) {  /* perpendicular: the shared coordinate fixes the wrap, the other the step */
        int64_t Df = vx == 0 ? Dx : Dy, Dm = vx == 0 ? Dy : Dx;
        int vm = vx == 0 ? vy : vx;
        int64_t k = 0, r = Df;
        while (r > U / 2) { r -= U; k--; }
        while (r < -U / 2) { r += U; k++; }
        if (Df + k * U > 1 || Df + k * U < -1) return INF;
        for (int wv = -1; wv <= 1; wv++) {
            int64_t num = wv - Dm - k * U;
            if (num % vm) continue;
            TEST(num / vm, k);
        }
        return best;
    }
    /* head on (SE vs NW, NE vs SW): the earliest lap at which x lines up, then y must too */
    for (int wx = -1; wx <= 1; wx++) {
        int64_t num = wx - Dx;
        if (num & 1) continue;
        int64_t k = 0, r = num;  /* k with num - kU in [0, U) (vx > 0) or (-U, 0] (vx < 0): the first lap */
        if (vx > 0) { while (r < 0) { r += U; k--; } while (r >= U) { r -= U; k++; } }
        else { while (r > 0) { r -= U; k++; } while (r <= -U) { r += U; k--; } }
        for (int lap = 0; lap <= (U % 4 ? 1 : 0); lap++, k += vx > 0 ? -1 : 1) {
            int64_t s = (num - k * U) / vx, yy = Dy + k * U + vy * s;
            if (yy <= 1 && yy >= -1) TEST(s, k);
        }
    }
    return best;
#undef TEST
}

/* schedule particle i's next event from its (x, y, t0, d); lowers partners' events as needed */
static void schedule(World *w, int32_t i) {
    int64_t ws = wall_steps(w, i), best = ws == INF ? INF : w->t0[i] + ws, key[3];
    int d = w->d[i];
    keys_of(w, i, key);
    for (int j = 0; j < 3; j++) {
        int kind = KIND[d][j];
        int64_t nb = w->nb[kind], range = kind >= 4 ? 2 : 1;
        int64_t prevb = -1;
        for (int64_t dk = -range; dk <= range; dk++) {
            int64_t kk = key[j] + dk;
            if (kind == 4) { if (kk < 0 || kk >= w->V2) continue; }
            else kk = wrap(kk, kind == 5 ? w->U2 : w->U);
            int64_t b = kk >> 1;
            if (b == prevb || b >= nb) continue;
            prevb = b;
            if (!((w->nbm[kind][b >> 6] >> (b & 63)) & 1)) continue;
            for (int32_t c = w->head[kind][b]; c >= 0; c = w->nxt[SLOT[w->d[c]][kind]][c]) {
                if (c == i) continue;
                if (kind < 4 && w->d[c] == d) continue;  /* same direction: covered by key 4/5 */
                int64_t t = meet(w, i, c);
                if (t == INF) continue;
                if (t < best) best = t;
                if (t < w->ev[c]) { w->ev[c] = t; hpush(w, t, c); }
            }
        }
    }
    index_add(w, i, key);
    w->ev[i] = best;
    if (best != INF) hpush(w, best, i);
}

static inline void set_dir(World *w, int32_t i) {
    int64_t o = w->t0[i] & 1;
    w->d[i] = (int)(((w->x[i] - o) & 1) + 2 * ((w->y[i] - o) & 1));
}

static inline void canon(World *w, int64_t *x, int64_t *y) {
    while (*x + *y < 0) { *x += w->U; *y += w->U; }
    while (*x + *y >= w->U2) { *x -= w->U; *y -= w->U; }
}

static int32_t new_particle(World *w, int64_t x, int64_t y, int64_t t) {
    int32_t i;
    if (w->free_head >= 0) { i = w->free_head; w->free_head = w->nxt[0][i]; }
    else { grow(w, w->top + 1); i = w->top++; }
    canon(w, &x, &y);
    w->x[i] = x; w->y[i] = y; w->t0[i] = t; w->alive[i] = 1; w->nalive++;
    set_dir(w, i);
    return i;
}

static void free_particle(World *w, int32_t i) {
    w->alive[i] = 0; w->nalive--; w->ver[i]++;
    w->nxt[0][i] = w->free_head; w->free_head = i;
}

/* is cell (x, y) occupied by a particle at step t (all events before t done)? */
static int occupied(World *w, int64_t x, int64_t y, int64_t t) {
    int64_t v = x - y, u2 = wrap(x + y, w->U2);
    for (int32_t i = 0; i < w->top; i++) {
        if (!w->alive[i]) continue;
        int64_t dt = t - w->t0[i], xi = w->x[i] + DX[w->d[i]] * dt, yi = w->y[i] + DY[w->d[i]] * dt;
        if (xi - yi == v && wrap(xi + yi, w->U2) == u2) return 1;
    }
    return 0;
}

void world_add(World *w, int64_t n, const int64_t *xy, int wall) {
    for (int64_t k = 0; k < n; k++) {
        int64_t x = xy[2 * k], y = xy[2 * k + 1], v = x - y;
        if (v < w->lo || v > w->hi) continue;
        if (wall) {
            int64_t u2 = wrap(x + y, w->U2), vv = v - w->lo + PAD;
            w->wu[u2 * w->RWU + (vv >> 6)] |= 1ULL << (vv & 63);
            w->wv[vv * w->RWV + (u2 >> 6)] |= 1ULL << (u2 & 63);
            continue;
        }
        if (is_wall(w, x, y) || occupied(w, x, y, w->t)) continue;
        schedule(w, new_particle(w, x, y, w->t));
    }
}

/* ------------------------------------------------------------------ events */
static void process_block(World *w, int64_t T, int32_t *ids, int m) {
    int64_t o = T & 1, x0 = w->x[ids[0]] + DX[w->d[ids[0]]] * (T - w->t0[ids[0]]), y0 = w->y[ids[0]] + DY[w->d[ids[0]]] * (T - w->t0[ids[0]]);
    x0 -= (x0 - o) & 1; y0 -= (y0 - o) & 1;
    canon(w, &x0, &y0);
    int wl[4], mv[4] = {-1, -1, -1, -1}, f[4], cnt = 0, vmaj = w->d[ids[0]] == 0 || w->d[ids[0]] == 3;
    for (int c = 0; c < 4; c++)  /* from the bitmap this particle's last scan read: those lines are hot */
        wl[c] = vmaj ? is_wall_v(w, x0 + (c & 1), y0 + (c >> 1)) : is_wall(w, x0 + (c & 1), y0 + (c >> 1));
    for (int q = 0; q < m; q++) {
        int32_t i = ids[q];
        int64_t dt = T - w->t0[i], x = w->x[i] + DX[w->d[i]] * dt, y = w->y[i] + DY[w->d[i]] * dt;
        mv[((x - o) & 1) + 2 * ((y - o) & 1)] = i;
    }
    for (int c = 0; c < 4; c++) { f[c] = wl[c] | (mv[c] >= 0); cnt += f[c]; }
    int to[4] = {0, 1, 2, 3};  /* where the content of each cell goes */
    if (cnt == 1) { for (int c = 0; c < 4; c++) to[c] = 3 - c; }
    else if (cnt == 2 && f[0] && f[3]) { to[0] = 1; to[3] = 2; }
    else if (cnt == 2 && f[1] && f[2]) { to[1] = 0; to[2] = 3; }
    for (int c = 0; c < 4; c++)
        if (wl[c] && to[c] != c) {
            int64_t v = x0 - y0, m = w->tiled ? 2 * (T - w->tbase) + 4 : 0;   /* in a tile: only where the result is exact */
            if (!w->tiled || (v >= w->wlo + m && v <= w->whi - m)) w->broken = 1;
        }
    for (int c = 0; c < 4; c++) {
        int32_t i = mv[c];
        if (i < 0) continue;
        index_del(w, i);
        w->ver[i]++;
        w->x[i] = x0 + (to[c] & 1); w->y[i] = y0 + (to[c] >> 1); w->t0[i] = T + 1;
        canon(w, &w->x[i], &w->y[i]);
        set_dir(w, i);
    }
    for (int c = 0; c < 4; c++) {
        int32_t i = mv[c];
        if (i < 0) continue;
        int64_t v = w->x[i] - w->y[i];
        if (v < w->lo || v > w->hi) free_particle(w, i);
        else schedule(w, i);
    }
}

/* run all events at steps < t_end */
static void run_to(World *w, int64_t t_end) {
    cq_init(w);
    cq_advance(w, w->t);
    for (;;) {
        int64_t T = cq_next(w, t_end);
        if (T == INF) break;
        cq_advance(w, T);
        int m = 0;
        int32_t *b = &w->cbuck[T & (CQ - 1)], e = *b; *b = -1;
        while (e >= 0) {  /* every live event at this step */
            int32_t id = w->cid[e], ver = w->cver[e], n = w->cnext[e];
            w->cnext[e] = w->cfree; w->cfree = e; e = n;
            if (!w->alive[id] || ver != w->ver[id] || w->mark[id] == (int32_t)(T & 0x7fffffff)) continue;
            w->mark[id] = (int32_t)(T & 0x7fffffff);
            if (m == w->gcap) { w->gcap = w->gcap ? 2 * w->gcap : 64; w->grp = realloc(w->grp, w->gcap * 4 * 2); }
            w->grp[m++] = id;
        }
        /* group by block: each particle's block corner at T */
        int64_t o = T & 1;
        int32_t *ids = w->grp, *tmp = w->grp + w->gcap;
        for (int q = 0; q < m; q++) {
            if (ids[q] < 0) continue;
            int32_t i = ids[q];
            int64_t dt = T - w->t0[i], xa = w->x[i] + DX[w->d[i]] * dt, ya = w->y[i] + DY[w->d[i]] * dt;
            xa -= (xa - o) & 1; ya -= (ya - o) & 1; canon(w, &xa, &ya);
            int n = 0;
            tmp[n++] = i; ids[q] = -1;
            for (int r = q + 1; r < m; r++) {
                int32_t j = ids[r];
                if (j < 0) continue;
                int64_t dj = T - w->t0[j], xb = w->x[j] + DX[w->d[j]] * dj, yb = w->y[j] + DY[w->d[j]] * dj;
                xb -= (xb - o) & 1; yb -= (yb - o) & 1; canon(w, &xb, &yb);
                if (xa == xb && ya == yb) { tmp[n++] = j; ids[r] = -1; }
            }
            process_block(w, T, tmp, n);
        }
    }
    w->t = t_end;
}

/* Run from w->t until t_end. feeds: sorted by time, (t, x, y) triples, added
 * before the step at that time. reads: sorted (t, x, y); out[k] = occupied
 * (only reads inside [w->t, t_end) are written, so calls can share `out`).
 * world_run_trace also records the moving cells after every step as
 * (x + y, x - y) pairs: counts[k] after step k, into uv (at most cap pairs). */
int64_t world_run_trace(World *w, int64_t t_end, int64_t nf, const int64_t *feeds,
                        int64_t nr, const int64_t *reads, uint8_t *out,
                        int32_t *uv, int64_t cap, int64_t *counts) {
    int64_t fi = 0, ri = 0, m = 0, k = 0;
    while (fi < nf && feeds[3 * fi] < w->t) fi++;
    while (ri < nr && reads[3 * ri] < w->t) ri++;
    while (w->t < t_end) {
        int64_t t = w->t, next = t_end;
        if (fi < nf && feeds[3 * fi] < next) next = feeds[3 * fi];
        if (ri < nr && reads[3 * ri] < next) next = reads[3 * ri];
        if (counts && next > t + 1) next = t + 1;
        run_to(w, next);
        while (fi < nf && feeds[3 * fi] == next) { world_add(w, 1, feeds + 3 * fi + 1, 0); fi++; }
        while (ri < nr && reads[3 * ri] == next) { out[ri] = (uint8_t)occupied(w, reads[3 * ri + 1], reads[3 * ri + 2], next); ri++; }
        if (counts && next == t + 1) {
            counts[k++] = w->nalive;
            for (int32_t i = 0; i < w->top && m < cap; i++) {
                if (!w->alive[i]) continue;
                int64_t dt = next - w->t0[i], x = w->x[i] + DX[w->d[i]] * dt, y = w->y[i] + DY[w->d[i]] * dt;
                uv[2 * m] = (int32_t)wrap(x + y, w->U2); uv[2 * m + 1] = (int32_t)(x - y); m++;
            }
        }
    }
    return m;
}

void world_run(World *w, int64_t t_end, int64_t nf, const int64_t *feeds,
               int64_t nr, const int64_t *reads, uint8_t *out) {
    world_run_trace(w, t_end, nf, feeds, nr, reads, out, NULL, 0, NULL);
}

int64_t world_count(World *w) { return w->nalive; }
int world_broken(World *w) { return w->broken; }
int64_t world_time(World *w) { return w->t; }
void world_set_time(World *w, int64_t t) { w->t = t; }

/* moving cells as (x, y) in canonical form (x + y in [0, 2U)) */
int64_t world_cells(World *w, int64_t *xy, int64_t cap) {
    int64_t n = 0;
    for (int32_t i = 0; i < w->top && n < cap; i++) {
        if (!w->alive[i]) continue;
        int64_t dt = w->t - w->t0[i], x = w->x[i] + DX[w->d[i]] * dt, y = w->y[i] + DY[w->d[i]] * dt;
        canon(w, &x, &y);
        xy[2 * n] = x; xy[2 * n + 1] = y; n++;
    }
    return w->nalive;
}

/* ------------------------------------------------------------------------
 * Tiled multithreaded run (same result as world_run). Every epoch of D
 * steps the particles are split across the band (by v = x - y) into T
 * ranges with equal counts. Thread k loads the particles within G = 2*D + 4
 * of its range into a private world (sharing the wall bitmaps), runs the
 * epoch's events there, and keeps the particles that end inside its own
 * range. Nothing moves faster than 2 in v per step, so whatever the window
 * gets wrong near its edges cannot reach the range within D steps: the kept
 * particles are exact. Feeds go to every tile whose window holds them; a
 * read is answered by the tile owning its cell. Between epochs the master
 * is just a list of positions; it is rebuilt as a world at the end. */
#ifndef BBM_NO_THREADS   /* the WebAssembly build (web/) has no threads */
#include <pthread.h>
#include <stdatomic.h>

typedef struct TP_ {
    World *w; int T, NT; int64_t t0, t1; uint8_t *out;    /* T threads run NT tiles, handed out dynamically */
    _Alignas(64) atomic_int next;                           /* the next tile to run this epoch */
    const int64_t *feeds, *reads; int64_t fa, fb, ra, rb;   /* this epoch's feeds / reads */
    const int64_t *trs; int64_t ta, tb;                     /* this epoch's steps to record (trs[ta..tb) in (t0, t1]) */
    int64_t *cuts; int first;                               /* T + 1 range ends in v; first epoch of the call */
    int64_t *lcuts;                                         /* the particle list at t0 is the tiles' px/py: tile k's holds v in [lcuts[k], lcuts[k+1]) */
    int recut;                                              /* the ranges changed this epoch: every ball is re-matched */
    _Alignas(64) atomic_int count; _Alignas(64) atomic_int sense; int stop, over, local;   /* over: the call has ended */
    pthread_t *th; pthread_mutex_t mu; pthread_cond_t cv; int64_t gen;   /* the pool; gen counts calls (workers park between them) */
} TP;

static void tbar(TP *M, int *local) {
    *local = !*local;
    if (atomic_fetch_add(&M->count, 1) == M->T - 1) { atomic_store(&M->count, 0); atomic_store(&M->sense, *local); }
    else while (atomic_load_explicit(&M->sense, memory_order_acquire) != *local) ;
}

static World *tile_new(World *m) {
    World *w = calloc(1, sizeof(World));
    w->U = m->U; w->U2 = m->U2; w->lo = m->lo; w->hi = m->hi; w->V2 = m->V2; w->S = m->S; w->RWU = m->RWU; w->RWV = m->RWV;
    w->wu = m->wu; w->wv = m->wv; w->shared_walls = 1; w->tiled = 1;
    memcpy(w->nb, m->nb, sizeof m->nb);
    for (int k = 0; k < NKIND; k++) { w->head[k] = malloc(w->nb[k] * 4); memset(w->head[k], -1, w->nb[k] * 4); w->nbm[k] = calloc(w->nb[k] / 64 + 1, 8); }
    w->free_head = -1;
    return w;
}

/* remove every particle (index and heap included); the world is then empty at time t */
static void world_clear(World *w, int64_t t) {
    for (int32_t i = 0; i < w->top; i++) if (w->alive[i]) { index_del(w, i); w->alive[i] = 0; w->ver[i]++; }
    w->top = 0; w->nalive = 0; w->free_head = -1; w->t = t;
    if (w->cbuck) { memset(w->cbuck, -1, CQ * 4); w->cfar = -1; w->cfree = -1; w->cn = 0; w->cbase = t; }
}

/* every particle's position at time t (canonical); returns the count */
static int64_t world_list(World *w, int64_t t, int64_t **lx, int64_t **ly) {
    *lx = malloc((w->nalive + 1) * 8); *ly = malloc((w->nalive + 1) * 8);
    int64_t n = 0;
    for (int32_t i = 0; i < w->top; i++) {
        if (!w->alive[i]) continue;
        int64_t dt = t - w->t0[i], x = w->x[i] + DX[w->d[i]] * dt, y = w->y[i] + DY[w->d[i]] * dt;
        canon(w, &x, &y);
        (*lx)[n] = x; (*ly)[n] = y; n++;
    }
    return n;
}

/* Move a window edge outward until it splits no falling or rising dimer (two
 * NE/SW particles on one line x + y, within 12 in v): a lone half ball bounces
 * off every mirror and shatters lanes, junk that costs events all epoch long
 * (it can never reach the tile's own range, but it is work). */
static int64_t widen(TP *M, int64_t edge, int outward, int ka, int kb) {
    World *w = M->w;
    int o = (int)(M->t0 & 1);
    for (int iter = 0; iter < 6; iter++) {
        int moved = 0;
        for (int s = ka; s <= kb && !moved; s++) {
            const World *S = w->tiles[s];
            for (int64_t j = 0; j < S->np && !moved; j++) {
                int64_t x = S->px[j], y = S->py[j], v = x - y;
                if (v < edge - 13 || v > edge + 13) continue;
                if (((x - o) & 1) == ((y - o) & 1)) continue;   /* SE or NW: stays on its line */
                int64_t u2 = x + y;
                for (int s2 = ka; s2 <= kb && !moved; s2++) {
                    const World *S2 = w->tiles[s2];
                    for (int64_t q = 0; q < S2->np; q++) {
                        if ((s2 == s && q == j) || S2->px[q] + S2->py[q] != u2) continue;
                        int64_t vq = S2->px[q] - S2->py[q];
                        if (vq < v - 12 || vq > v + 12) continue;
                        int64_t a = v < vq ? v : vq, b = v < vq ? vq : v;
                        if (outward < 0 && a < edge && b >= edge) { edge = a; moved = 1; break; }   /* lower edge: window is [edge, ..] */
                        if (outward > 0 && a <= edge && b > edge) { edge = b; moved = 1; break; }   /* upper edge: window is [.., edge] */
                    }
                }
            }
        }
        if (!moved) break;
    }
    return edge < w->lo ? w->lo : edge > w->hi ? w->hi : edge;
}

/* One epoch of tile k. Its own-range particles are exact and stay in the
 * tile from epoch to epoch (the cuts are fixed for the call); only the ghost
 * zones are replaced, from the merged list. Re-adding a ghost lowers the
 * events of the own particles it can meet, so nothing is missed. */
static inline double now_ns(void) { struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts); return ts.tv_sec * 1e9 + ts.tv_nsec; }

static void tile_epoch(TP *M, int k) {
    World *w = M->w, *t = w->tiles[k]; double c0 = now_ns();
    int64_t G = 2 * (M->t1 - M->t0) + 4, vlo = M->cuts[k], vhi = M->cuts[k + 1];
    /* the list segments (tiles' previous kept lists) that can hold anything within the window (widen moves an edge by at most 6 * 12 + 13) */
    int ka = 0, kb = M->NT - 1;
    while (ka < kb && M->lcuts[ka + 1] <= vlo - G - 100) ka++;
    while (kb > ka && M->lcuts[kb] > vhi + G + 100) kb--;
    t->wlo = widen(M, vlo - G > w->lo ? vlo - G : w->lo, -1, ka, kb);
    t->whi = widen(M, vhi - 1 + G < w->hi ? vhi - 1 + G : w->hi, +1, ka, kb);
    t->tbase = M->t0;
    int64_t ng = 0;
    if (M->first) world_clear(t, M->t0);
    else {   /* last epoch's ghosts (every ball after a recut), where they are now (not at their last event) */
        if (t->gcap2 < t->nalive + 1) {
            t->gcap2 = 2 * (t->nalive + 1);
            t->gx = realloc(t->gx, t->gcap2 * 8); t->gy = realloc(t->gy, t->gcap2 * 8); t->gid = realloc(t->gid, t->gcap2 * 4); t->gm = realloc(t->gm, t->gcap2);
        }
        for (int32_t i = 0; i < t->top; i++) {
            if (!t->alive[i]) continue;
            int64_t dt = M->t0 - t->t0[i], x = t->x[i] + DX[t->d[i]] * dt, y = t->y[i] + DY[t->d[i]] * dt;
            if (!M->recut && x - y >= vlo && x - y < vhi) continue;   /* mine, exact */
            canon(t, &x, &y);
            t->gx[ng] = x; t->gy[ng] = y; t->gid[ng] = i; t->gm[ng] = 0; ng++;
        }
    }
    /* The window's balls from the merged list. A ghost of last epoch that sits
     * exactly where the list has a ball is that ball (direction follows from
     * the cell and the step), so it stays, events and all; the other ghosts
     * go and the unmatched balls come in. Nearly every ghost matches: the
     * inexact ones are only those that met the window's edge. */
    for (int pass = 0; pass < 2; pass++) {   /* pass 0 marks the matches, then the unmatched ghosts go, pass 1 adds the newcomers
                                              * (into the freed slots: the particle arrays stay as short as the live count) */
        if (pass == 1) for (int64_t g = 0; g < ng; g++) if (!t->gm[g]) { index_del(t, t->gid[g]); free_particle(t, t->gid[g]); }
        for (int s = ka; s <= kb; s++) {
            const World *S = w->tiles[s];
            for (int64_t j = 0; j < S->np; j++) {
                int64_t x = S->px[j], y = S->py[j], v = x - y;
                if (v < t->wlo || v > t->whi) continue;
                if (!M->first && !M->recut && v >= vlo && v < vhi) continue;   /* mine already, exact */
                int64_t g = 0;
                for (; g < ng; g++) if (t->gx[g] == x && t->gy[g] == y && t->gm[g] == pass) { t->gm[g] = 1 + pass; break; }
                if (g == ng && pass == 1) schedule(t, new_particle(t, x, y, M->t0));
            }
        }
    }
    int64_t fi = M->fa, ri = M->ra, ti = M->ta;
    t->tn = 0; t->ntc = 0;
    while (t->t < M->t1) {
        int64_t next = M->t1;
        if (fi < M->fb && M->feeds[3 * fi] < next) next = M->feeds[3 * fi];
        if (ri < M->rb && M->reads[3 * ri] < next) next = M->reads[3 * ri];
        if (ti < M->tb && M->trs[ti] < next) next = M->trs[ti];
        run_to(t, next);
        for (; fi < M->fb && M->feeds[3 * fi] == next; fi++) {
            int64_t v = M->feeds[3 * fi + 1] - M->feeds[3 * fi + 2];
            if (v >= t->wlo && v <= t->whi) world_add(t, 1, M->feeds + 3 * fi + 1, 0);
        }
        for (; ri < M->rb && M->reads[3 * ri] == next; ri++) {
            int64_t v = M->reads[3 * ri + 1] - M->reads[3 * ri + 2];
            if (v >= vlo && v < vhi) M->out[ri] = (uint8_t)occupied(t, M->reads[3 * ri + 1], M->reads[3 * ri + 2], next);
        }
        if (ti < M->tb && M->trs[ti] == next) {   /* record my range's balls at this step, fed ones included (exact: the tiles' shares are every ball) */
            ti++;
            if (t->tcap < t->tn + t->nalive) { t->tcap = 2 * (t->tn + t->nalive) + 64; t->tuv = realloc(t->tuv, t->tcap * 8); }
            if (t->tcc < t->ntc + 1) { t->tcc = 2 * (t->ntc + 1) + 8; t->tcnt = realloc(t->tcnt, t->tcc * 8); }
            int64_t c = 0;
            for (int32_t i = 0; i < t->top; i++) {
                if (!t->alive[i]) continue;
                int64_t dt = next - t->t0[i], x = t->x[i] + DX[t->d[i]] * dt, y = t->y[i] + DY[t->d[i]] * dt;
                if (x - y < vlo || x - y >= vhi) continue;
                canon(t, &x, &y);
                t->tuv[2 * t->tn] = (int32_t)wrap(x + y, t->U2); t->tuv[2 * t->tn + 1] = (int32_t)(x - y); t->tn++; c++;
            }
            t->tcnt[t->ntc++] = c;
        }
    }
    if (t->kcap < t->nalive + 1) { t->kcap = 2 * (t->nalive + 1); t->kx = realloc(t->kx, t->kcap * 8); t->ky = realloc(t->ky, t->kcap * 8); }
    t->nk = 0;
    for (int32_t i = 0; i < t->top; i++) {   /* keep what ended in my own range */
        if (!t->alive[i]) continue;
        int64_t dt = M->t1 - t->t0[i], x = t->x[i] + DX[t->d[i]] * dt, y = t->y[i] + DY[t->d[i]] * dt;
        if (x - y < vlo || x - y >= vhi) continue;
        canon(t, &x, &y);
        t->kx[t->nk] = x; t->ky[t->nk] = y; t->nk++;
    }
    t->ptime += now_ns() - c0; t->pballs += t->nk;
}

/* thread id runs its own tile (hot in its cache), then whatever tiles beyond T are left */
static void run_tiles(TP *M, int id) {
    for (int k = id; k < M->NT; k = M->T + atomic_fetch_add(&M->next, 1)) tile_epoch(M, k);
}

typedef struct { TP *M; int id; } TArg;

static void *tile_worker(void *arg) {
    TP *M = ((TArg *)arg)->M; int id = ((TArg *)arg)->id, local = 0; int64_t gen = 0;
    free(arg);
    while (1) {
        pthread_mutex_lock(&M->mu);   /* park until the next call */
        while (M->gen == gen && !M->stop) pthread_cond_wait(&M->cv, &M->mu);
        gen = M->gen;
        pthread_mutex_unlock(&M->mu);
        if (M->stop) break;
        while (1) {
            tbar(M, &local);          /* the epoch is set up, or the call is over */
            if (M->over) { tbar(M, &local); break; }   /* (everyone has seen the end before the next call starts) */
            run_tiles(M, id);
            tbar(M, &local);          /* results ready */
        }
    }
    return NULL;
}

static void pool_free(TP *M) {
    pthread_mutex_lock(&M->mu); M->stop = 1; pthread_cond_broadcast(&M->cv); pthread_mutex_unlock(&M->mu);
    for (int k = 1; k < M->T; k++) pthread_join(M->th[k], NULL);
    free(M->th); free(M->cuts); free(M->lcuts); free(M);
}

#define HB 16  /* histogram bin width in v for the cuts */
#define RECUT 8  /* epochs between recuts of the ranges (each a full reload of the tiles) */
#ifndef TILES_PER_THREAD
#define TILES_PER_THREAD 1  /* 2-4 measured slower: the per-tile costs (ghosts, widening) outweigh the balance */
#endif

/* As world_run_trace, on T threads with epochs of D steps: traces are the
 * ntr sorted steps trs (each > the current time) whose balls go to uv/counts
 * as there. Returns the pairs written (at most cap). */
int64_t world_run_tiles(World *w, int64_t t_end, int64_t nf, const int64_t *feeds,
                        int64_t nr, const int64_t *reads, uint8_t *out, int T, int64_t D,
                        int64_t ntr, const int64_t *trs, int32_t *uv, int64_t cap, int64_t *counts) {
    int NT = T * TILES_PER_THREAD;
    if (w->ntiles < NT) {
        w->tiles = realloc(w->tiles, NT * sizeof(World *));
        for (int k = w->ntiles; k < NT; k++) w->tiles[k] = tile_new(w);
        w->ntiles = NT;
    }
    if (w->pool && w->pool->T != T) { pool_free(w->pool); w->pool = NULL; }
    if (!w->pool) {
        TP *P = w->pool = calloc(1, sizeof *P);
        P->w = w; P->T = T; P->NT = NT;
        P->cuts = malloc((NT + 1) * 8); P->lcuts = malloc((NT + 1) * 8);
        P->th = malloc(T * sizeof(pthread_t));
        pthread_mutex_init(&P->mu, NULL); pthread_cond_init(&P->cv, NULL);
        for (int k = 1; k < T; k++) { TArg *a = malloc(sizeof *a); *a = (TArg){P, k}; pthread_create(&P->th[k], NULL, tile_worker, a); }
    }
    TP *M = w->pool;
    M->out = out; M->feeds = feeds; M->reads = reads; M->trs = trs; M->over = 0;
    int64_t ti = 0, m = 0;
    while (ti < ntr && trs[ti] <= w->t) ti++;
    pthread_mutex_lock(&M->mu); M->gen++; pthread_cond_broadcast(&M->cv); pthread_mutex_unlock(&M->mu);   /* wake the pool */
    {   /* the master's particles become tile 0's "previous kept list", one segment holding the whole band */
        World *t = w->tiles[0];
        free(t->px); free(t->py);
        t->np = world_list(w, w->t, &t->px, &t->py); t->pcap = t->np + 1;
        for (int k = 1; k < NT; k++) w->tiles[k]->np = 0;
        M->lcuts[0] = w->lo;
        for (int k = 1; k <= NT; k++) M->lcuts[k] = w->hi + 1;
    }
    world_clear(w, w->t);
    int64_t nb = w->V2 / HB + 2; double *hist = malloc(nb * sizeof(double));
    int64_t fi = 0, ri = 0;
    while (fi < nf && feeds[3 * fi] < w->t) fi++;
    while (ri < nr && reads[3 * ri] < w->t) ri++;
    M->first = 1;
    int64_t epoch = 0;
    while (w->t < t_end) {
        M->t0 = w->t; M->t1 = w->t + D < t_end ? w->t + D : t_end;
        M->recut = 0;
        if (epoch++ % RECUT == 0) {   /* recut the ranges: a coarse histogram of v, each ball weighted by its
                                       * tile's measured time per ball since the last recut (the hot spots
                                       * persist), cut into equal weights; the tiles then re-match every ball */
            memset(hist, 0, nb * 8);
            double wsum = 0;
            for (int k = 0; k < NT; k++) {   /* weights: 1 before any measurement (equal counts) */
                World *t = w->tiles[k];
                double wk = t->ptime > 0 ? t->ptime / (t->pballs + 2.0) : 0;
                for (int64_t j = 0; j < t->np; j++) hist[(t->px[j] - t->py[j] - w->lo) / HB] += wk > 0 ? wk : 1;
                wsum += t->np * (wk > 0 ? wk : 1);
                t->ptime = 0; t->pballs = 0;
            }
            M->cuts[0] = w->lo; M->cuts[NT] = w->hi + 1;
            double acc = 0; int k = 1;
            for (int64_t b = 0; b < nb && k < NT; b++) {
                acc += hist[b];
                while (k < NT && acc * NT >= wsum * k) M->cuts[k++] = w->lo + (b + 1) * HB;
            }
            for (; k < NT; k++) M->cuts[k] = w->hi + 1;
            M->recut = 1;
        }
        M->fa = fi; while (fi < nf && feeds[3 * fi] < M->t1) fi++; M->fb = fi;
        M->ra = ri; while (ri < nr && reads[3 * ri] < M->t1) ri++; M->rb = ri;
        M->ta = ti; while (ti < ntr && trs[ti] <= M->t1) ti++; M->tb = ti;
        atomic_store(&M->next, 0);
        tbar(M, &M->local);
        run_tiles(M, 0);
        tbar(M, &M->local);
        for (int k2 = 0; k2 < NT; k2++) w->tiles[k2]->toff = 0;
        for (int64_t q = M->ta; q < M->tb; q++) {   /* the recorded steps: every tile's share, in step order */
            int64_t c = 0;
            for (int k2 = 0; k2 < NT; k2++) {
                World *t = w->tiles[k2];
                int64_t n = t->tcnt[q - M->ta], take = m + n <= cap ? n : (m < cap ? cap - m : 0);
                memcpy(uv + 2 * m, t->tuv + 2 * t->toff, take * 8);
                m += take; t->toff += n; c += n;
            }
            counts[q] = c;
        }
        for (int k2 = 0; k2 < NT; k2++) {   /* the kept lists become the previous ones (segment k2 = tile k2's range); no copy */
            World *t = w->tiles[k2];
            int64_t *x = t->px, *y = t->py, c = t->pcap;
            t->px = t->kx; t->py = t->ky; t->np = t->nk; t->pcap = t->kcap;
            t->kx = x; t->ky = y; t->kcap = c; t->nk = 0;
            M->lcuts[k2] = M->cuts[k2];
            if (t->broken) { w->broken = 1; t->broken = 0; }
        }
        M->lcuts[NT] = M->cuts[NT];
        w->t = M->t1;
        M->first = 0;
    }
    M->over = 1;   /* the call is over: the workers park */
    tbar(M, &M->local); tbar(M, &M->local);
    for (int k = 0; k < NT; k++) {   /* the master is a world again */
        const World *t = w->tiles[k];
        for (int64_t j = 0; j < t->np; j++) schedule(w, new_particle(w, t->px[j], t->py[j], w->t));
    }
    free(hist);
    return m;
}
#endif
