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

#define PAD 2
#define INF INT64_MAX
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
    /* heap of (time, id, version) */
    int64_t *ht; int32_t *hid, *hver; int64_t hn, hcap;
    int32_t *grp; int64_t gcap;
    int64_t t;
    int broken;
    /* tiles (world_run_tiles): private copies sharing the walls; a tile trusts
     * only blocks the window edges cannot have reached yet */
    int tiled, shared_walls; int64_t wlo, whi, tbase;
    struct World_ **tiles; int ntiles;
    int64_t *kx, *ky; int64_t nk, kcap;   /* a tile's kept particles after an epoch */
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

void world_free(World *w) {
    for (int k = 0; k < w->ntiles; k++) world_free(w->tiles[k]);
    free(w->tiles); free(w->kx); free(w->ky);
    if (!w->shared_walls) { free(w->wu); free(w->wv); }
    for (int k = 0; k < NKIND; k++) { free(w->head[k]); free(w->nbm[k]); }
    for (int j = 0; j < 3; j++) { free(w->nxt[j]); free(w->prv[j]); free(w->bk[j]); }
    free(w->x); free(w->y); free(w->t0); free(w->ev); free(w->d); free(w->alive); free(w->ver); free(w->mark);
    free(w->ht); free(w->hid); free(w->hver); free(w->grp); free(w);
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

/* ------------------------------------------------------------------ heap */
static void hpush(World *w, int64_t t, int32_t id) {
    if (w->hn == w->hcap) {
        w->hcap = w->hcap ? 2 * w->hcap : 4096;
        w->ht = realloc(w->ht, w->hcap * 8); w->hid = realloc(w->hid, w->hcap * 4); w->hver = realloc(w->hver, w->hcap * 4);
    }
    int64_t k = w->hn++;
    while (k > 0) {
        int64_t p = (k - 1) >> 1;
        if (w->ht[p] <= t) break;
        w->ht[k] = w->ht[p]; w->hid[k] = w->hid[p]; w->hver[k] = w->hver[p];
        k = p;
    }
    w->ht[k] = t; w->hid[k] = id; w->hver[k] = w->ver[id];
}

static void hpop(World *w) {
    int64_t n = --w->hn;
    if (!n) return;
    int64_t t = w->ht[n]; int32_t id = w->hid[n], ver = w->hver[n], k = 0;
    while (1) {
        int64_t c = 2 * k + 1;
        if (c >= n) break;
        if (c + 1 < n && w->ht[c + 1] < w->ht[c]) c++;
        if (w->ht[c] >= t) break;
        w->ht[k] = w->ht[c]; w->hid[k] = w->hid[c]; w->hver[k] = w->hver[c];
        k = c;
    }
    w->ht[k] = t; w->hid[k] = id; w->hver[k] = ver;
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
    while (w->hn && w->ht[0] < t_end) {
        int64_t T = w->ht[0];
        int m = 0;
        while (w->hn && w->ht[0] == T) {  /* every live event at this step */
            int32_t id = w->hid[0], ver = w->hver[0];
            hpop(w);
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
#include <pthread.h>
#include <stdatomic.h>

typedef struct {
    World *w; int T; int64_t t0, t1; uint8_t *out;
    const int64_t *feeds, *reads; int64_t fa, fb, ra, rb;   /* this epoch's feeds / reads */
    int64_t *cuts; int first;                               /* T + 1 range ends in v; first epoch of the call */
    const int64_t *lx, *ly; int64_t n;                      /* the particle list at t0 */
    _Alignas(64) atomic_int count; _Alignas(64) atomic_int sense; int stop;
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
    w->top = 0; w->nalive = 0; w->free_head = -1; w->hn = 0; w->t = t;
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
static int64_t widen(TP *M, int64_t edge, int outward) {
    World *w = M->w;
    int o = (int)(M->t0 & 1);
    for (int iter = 0; iter < 6; iter++) {
        int moved = 0;
        for (int64_t j = 0; j < M->n && !moved; j++) {
            int64_t x = M->lx[j], y = M->ly[j], v = x - y;
            if (v < edge - 13 || v > edge + 13) continue;
            if (((x - o) & 1) == ((y - o) & 1)) continue;   /* SE or NW: stays on its line */
            int64_t u2 = x + y;
            for (int64_t q = 0; q < M->n; q++) {
                if (q == j || M->lx[q] + M->ly[q] != u2) continue;
                int64_t vq = M->lx[q] - M->ly[q];
                if (vq < v - 12 || vq > v + 12) continue;
                int64_t a = v < vq ? v : vq, b = v < vq ? vq : v;
                if (outward < 0 && a < edge && b >= edge) { edge = a; moved = 1; break; }   /* lower edge: window is [edge, ..] */
                if (outward > 0 && a <= edge && b > edge) { edge = b; moved = 1; break; }   /* upper edge: window is [.., edge] */
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
static void tile_epoch(TP *M, int k) {
    World *w = M->w, *t = w->tiles[k];
    int64_t G = 2 * (M->t1 - M->t0) + 4, vlo = M->cuts[k], vhi = M->cuts[k + 1];
    t->wlo = widen(M, vlo - G > w->lo ? vlo - G : w->lo, -1);
    t->whi = widen(M, vhi - 1 + G < w->hi ? vhi - 1 + G : w->hi, +1);
    t->tbase = M->t0;
    if (M->first) world_clear(t, M->t0);
    else {
        for (int32_t i = 0; i < t->top; i++) {   /* drop last epoch's ghosts (where they are now, not at their last event) */
            if (!t->alive[i]) continue;
            int64_t dt = M->t0 - t->t0[i], v = (t->x[i] - t->y[i]) + (DX[t->d[i]] - DY[t->d[i]]) * dt;
            if (v < vlo || v >= vhi) { index_del(t, i); free_particle(t, i); }
        }
    }
    for (int64_t j = 0; j < M->n; j++) {
        int64_t v = M->lx[j] - M->ly[j];
        if (v < t->wlo || v > t->whi) continue;
        if (!M->first && v >= vlo && v < vhi) continue;   /* mine already, exact */
        schedule(t, new_particle(t, M->lx[j], M->ly[j], M->t0));
    }
    int64_t fi = M->fa, ri = M->ra;
    while (t->t < M->t1) {
        int64_t next = M->t1;
        if (fi < M->fb && M->feeds[3 * fi] < next) next = M->feeds[3 * fi];
        if (ri < M->rb && M->reads[3 * ri] < next) next = M->reads[3 * ri];
        run_to(t, next);
        for (; fi < M->fb && M->feeds[3 * fi] == next; fi++) {
            int64_t v = M->feeds[3 * fi + 1] - M->feeds[3 * fi + 2];
            if (v >= t->wlo && v <= t->whi) world_add(t, 1, M->feeds + 3 * fi + 1, 0);
        }
        for (; ri < M->rb && M->reads[3 * ri] == next; ri++) {
            int64_t v = M->reads[3 * ri + 1] - M->reads[3 * ri + 2];
            if (v >= vlo && v < vhi) M->out[ri] = (uint8_t)occupied(t, M->reads[3 * ri + 1], M->reads[3 * ri + 2], next);
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
}

typedef struct { TP *M; int id; } TArg;

static void *tile_worker(void *arg) {
    TP *M = ((TArg *)arg)->M; int id = ((TArg *)arg)->id, local = 0;
    while (1) {
        tbar(M, &local);              /* the epoch is set up */
        if (M->stop) break;
        tile_epoch(M, id);
        tbar(M, &local);              /* results ready */
    }
    return NULL;
}

#define HB 16  /* histogram bin width in v for the cuts */

void world_run_tiles(World *w, int64_t t_end, int64_t nf, const int64_t *feeds,
                     int64_t nr, const int64_t *reads, uint8_t *out, int T, int64_t D) {
    if (w->ntiles < T) {
        w->tiles = realloc(w->tiles, T * sizeof(World *));
        for (int k = w->ntiles; k < T; k++) w->tiles[k] = tile_new(w);
        w->ntiles = T;
    }
    TP M; memset(&M, 0, sizeof M);
    M.w = w; M.T = T; M.out = out; M.feeds = feeds; M.reads = reads;
    M.cuts = malloc((T + 1) * 8);
    int64_t *lx, *ly, n = world_list(w, w->t, &lx, &ly), cap = n + 1;
    world_clear(w, w->t);
    int64_t nb = w->V2 / HB + 2, *hist = malloc(nb * 8);
    pthread_t th[T]; TArg args[T];
    for (int k = 1; k < T; k++) { args[k] = (TArg){&M, k}; pthread_create(&th[k], NULL, tile_worker, &args[k]); }
    int local = 0;
    int64_t fi = 0, ri = 0;
    while (fi < nf && feeds[3 * fi] < w->t) fi++;
    while (ri < nr && reads[3 * ri] < w->t) ri++;
    memset(hist, 0, nb * 8);   /* equal-count ranges from a coarse histogram of v, fixed for this call */
    for (int64_t j = 0; j < n; j++) hist[(lx[j] - ly[j] - w->lo) / HB]++;
    M.cuts[0] = w->lo; M.cuts[T] = w->hi + 1;
    {
        int64_t acc = 0; int k = 1;
        for (int64_t b = 0; b < nb && k < T; b++) {
            acc += hist[b];
            while (k < T && acc * T >= n * k) M.cuts[k++] = w->lo + (b + 1) * HB;
        }
        for (; k < T; k++) M.cuts[k] = w->hi + 1;
    }
    M.first = 1;
    while (w->t < t_end) {
        M.t0 = w->t; M.t1 = w->t + D < t_end ? w->t + D : t_end;
        M.lx = lx; M.ly = ly; M.n = n;
        M.fa = fi; while (fi < nf && feeds[3 * fi] < M.t1) fi++; M.fb = fi;
        M.ra = ri; while (ri < nr && reads[3 * ri] < M.t1) ri++; M.rb = ri;
        tbar(&M, &local);
        tile_epoch(&M, 0);
        tbar(&M, &local);
        int64_t tot = 0;   /* merge the kept particles */
        for (int k2 = 0; k2 < T; k2++) { tot += w->tiles[k2]->nk; if (w->tiles[k2]->broken) { w->broken = 1; w->tiles[k2]->broken = 0; } }
        if (tot + 1 > cap) { cap = 2 * (tot + 1); lx = realloc(lx, cap * 8); ly = realloc(ly, cap * 8); }
        n = 0;
        for (int k2 = 0; k2 < T; k2++) {
            World *t = w->tiles[k2];
            memcpy(lx + n, t->kx, t->nk * 8); memcpy(ly + n, t->ky, t->nk * 8); n += t->nk;
        }
        w->t = M.t1;
        M.first = 0;
    }
    M.stop = 1;
    tbar(&M, &local);
    for (int k = 1; k < T; k++) pthread_join(th[k], NULL);
    for (int64_t j = 0; j < n; j++) schedule(w, new_particle(w, lx[j], ly[j], w->t));   /* the master is a world again */
    free(lx); free(ly); free(M.cuts); free(hist);
}
