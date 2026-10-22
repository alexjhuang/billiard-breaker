/* Exact BBMCA on the diagonal cylinder, same rule as cyl.py (which it is
 * tested against).
 *
 * Cells are stored by index i = u2 * S + (v2 + PAD), where u2 = (x + y) mod 2U
 * and v2 = x - y - lo; S = V2 + 2*PAD. The PAD columns on both sides of the
 * band are always empty, so block lookups never need bounds checks; any ball
 * that steps into them is dropped (the sinks). Moving x by one changes the
 * index by S + 1, moving y by one by S - 1. Two bitmaps: static walls and
 * moving particles. Each step visits only the moving particles; a block is
 * processed once (its moving bits are cleared as it is handled) and the new
 * positions are written afterwards. */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
#include <stdatomic.h>

#define PAD 2

typedef struct {
    int64_t U2, lo, hi, V2, S, N;  /* N = U2 * S cells */
    uint64_t *wall, *mov;
    int64_t *p, *np;               /* particle cell indices now / next */
    int32_t *pu, *pv, *nu, *nv;    /* their u2 and (v2 + PAD) */
    int64_t n, cap;
    int64_t t;
    int broken;
    uint64_t **tile_mov; int ntile_mov;   /* private bitmaps for world_run_tiles, kept */
} World;

static inline int getb(const uint64_t *b, int64_t i) { return (b[i >> 6] >> (i & 63)) & 1; }
static inline void setb(uint64_t *b, int64_t i) { b[i >> 6] |= 1ULL << (i & 63); }
static inline void clrb(uint64_t *b, int64_t i) { b[i >> 6] &= ~(1ULL << (i & 63)); }
static inline int64_t wrap(World *w, int64_t i) { return i < 0 ? i + w->N : (i >= w->N ? i - w->N : i); }

World *world_new(int64_t U, int64_t lo, int64_t hi) {
    World *w = calloc(1, sizeof(World));
    w->U2 = 2 * U; w->lo = lo; w->hi = hi; w->V2 = hi - lo + 1;
    w->S = w->V2 + 2 * PAD; w->N = w->U2 * w->S;
    int64_t words = (w->N + 63) / 64 + 1;
    w->wall = calloc(words, 8);
    w->mov = calloc(words, 8);
    w->cap = 1024;
    w->p = malloc(w->cap * 8); w->np = malloc(w->cap * 8);
    w->pu = malloc(w->cap * 4); w->pv = malloc(w->cap * 4);
    w->nu = malloc(w->cap * 4); w->nv = malloc(w->cap * 4);
    return w;
}

void world_free(World *w) {
    for (int k = 0; k < w->ntile_mov; k++) free(w->tile_mov[k]);
    free(w->tile_mov);
    free(w->wall); free(w->mov); free(w->p); free(w->np);
    free(w->pu); free(w->pv); free(w->nu); free(w->nv); free(w);
}

static void grow(World *w, int64_t need) {
    if (need <= w->cap) return;
    while (w->cap < need) w->cap *= 2;
    w->p = realloc(w->p, w->cap * 8); w->np = realloc(w->np, w->cap * 8);
    w->pu = realloc(w->pu, w->cap * 4); w->pv = realloc(w->pv, w->cap * 4);
    w->nu = realloc(w->nu, w->cap * 4); w->nv = realloc(w->nv, w->cap * 4);
}

static inline int64_t mod(int64_t a, int64_t m) { int64_t r = a % m; return r < 0 ? r + m : r; }

/* (x, y) -> index, or -1 if outside the band */
static inline int64_t index_of(World *w, int64_t x, int64_t y) {
    int64_t v = x - y;
    if (v < w->lo || v > w->hi) return -1;
    return mod(x + y, w->U2) * w->S + (v - w->lo + PAD);
}

static inline int inside(World *w, int64_t i) {
    int64_t c = i % w->S;
    return c >= PAD && c < PAD + w->V2;
}

void world_add(World *w, int64_t n, const int64_t *xy, int wall) {
    grow(w, w->n + n);
    for (int64_t k = 0; k < n; k++) {
        int64_t i = index_of(w, xy[2 * k], xy[2 * k + 1]);
        if (i < 0) continue;
        if (wall) { setb(w->wall, i); continue; }
        if (getb(w->mov, i)) continue;
        setb(w->mov, i);
        w->pu[w->n] = (int32_t)(i / w->S); w->pv[w->n] = (int32_t)(i % w->S);
        w->p[w->n++] = i;
    }
}

static void step(World *w) {
    const int o = (int)(w->t & 1);
    const int64_t S = w->S, DX = S + 1, DY = S - 1, N = w->N;
    const int32_t U2 = (int32_t)w->U2, lo = (int32_t)w->lo;
    uint64_t *mov = w->mov, *wall = w->wall;
    int64_t m = 0;
    grow(w, 4 * w->n + 4);
    int64_t *np = w->np;
    int32_t *nu = w->nu, *nv = w->nv;
    static const int32_t DU[4] = {0, 1, 1, 2}, DV[4] = {0, 1, -1, 0};  /* TL TR BL BR from TL */
    for (int64_t k = 0; k < w->n; k++) {
        int64_t i = w->p[k];
        if (!getb(mov, i)) continue;  /* its block was already handled */
        int32_t u2 = w->pu[k], vv = w->pv[k], v = vv - PAD + lo;
        int rx = (((u2 + v) >> 1) - o) & 1, ry = (((u2 - v) >> 1) - o) & 1;
        int me = rx + 2 * ry;
        int64_t c0 = i - rx * DX - ry * DY;
        int32_t tu = u2 - DU[me], tv = vv - DV[me];
        if (c0 < 0) c0 += N;
        if (tu < 0) tu += U2;
        int64_t ci[4] = {c0, c0 + DX, c0 + DY, c0 + DX + DY};
        for (int c = 1; c < 4; c++) if (ci[c] >= N) ci[c] -= N;
        int mv[4], wl[4], f[4], cnt = 0;
        for (int c = 0; c < 4; c++) {
            mv[c] = getb(mov, ci[c]); wl[c] = getb(wall, ci[c]);
            f[c] = mv[c] | wl[c]; cnt += f[c];
        }
        if (cnt == 1) {  /* lone particle: to the opposite corner */
            clrb(mov, i);
            int c = 3 - me, cu = tu + DU[c];
            np[m] = ci[c]; nu[m] = cu >= U2 ? cu - U2 : cu; nv[m] = tv + DV[c]; m++;
            continue;
        }
        int g[4];
        if (cnt == 2 && f[0] && f[3]) { g[0] = 0; g[3] = 0; g[1] = 1; g[2] = 1; }
        else if (cnt == 2 && f[1] && f[2]) { g[1] = 0; g[2] = 0; g[0] = 1; g[3] = 1; }
        else { for (int c = 0; c < 4; c++) g[c] = f[c]; }
        for (int c = 0; c < 4; c++) {
            if (mv[c]) clrb(mov, ci[c]);
            if (wl[c] && !g[c]) w->broken = 1;
        }
        for (int c = 0; c < 4; c++)
            if (g[c] && !wl[c]) {
                int cu = tu + DU[c];
                np[m] = ci[c]; nu[m] = cu >= U2 ? cu - U2 : cu; nv[m] = tv + DV[c]; m++;
            }
    }
    int64_t kept = 0;  /* write new positions; drop those in the padding (sinks) */
    const int32_t vmax = PAD + (int32_t)w->V2;
    for (int64_t k = 0; k < m; k++) {
        if (nv[k] < PAD || nv[k] >= vmax) continue;
        setb(mov, np[k]);
        np[kept] = np[k]; nu[kept] = nu[k]; nv[kept] = nv[k]; kept++;
    }
    w->np = w->p; w->p = np;
    int32_t *t;
    t = w->pu; w->pu = nu; w->nu = t;
    t = w->pv; w->pv = nv; w->nv = t;
    w->n = kept;
    w->t++;
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
        while (fi < nf && feeds[3 * fi] == w->t) { world_add(w, 1, feeds + 3 * fi + 1, 0); fi++; }
        while (ri < nr && reads[3 * ri] == w->t) {
            int64_t i = index_of(w, reads[3 * ri + 1], reads[3 * ri + 2]);
            out[ri] = i < 0 ? 0 : getb(w->mov, i);
            ri++;
        }
        step(w);
        if (!counts) continue;
        counts[k++] = w->n;
        for (int64_t j = 0; j < w->n && m < cap; j++, m++) {
            uv[2 * m] = w->pu[j]; uv[2 * m + 1] = w->pv[j] - PAD + (int32_t)w->lo;
        }
    }
    return m;
}

void world_run(World *w, int64_t t_end, int64_t nf, const int64_t *feeds,
               int64_t nr, const int64_t *reads, uint8_t *out) {
    world_run_trace(w, t_end, nf, feeds, nr, reads, out, NULL, 0, NULL);
}

int64_t world_count(World *w) { return w->n; }
int world_broken(World *w) { return w->broken; }
int64_t world_time(World *w) { return w->t; }
void world_set_time(World *w, int64_t t) { w->t = t; }

/* moving cells as (x, y) in canonical form (x + y in [0, 2U)) */
int64_t world_cells(World *w, int64_t *xy, int64_t cap) {
    int64_t n = w->n < cap ? w->n : cap;
    for (int64_t k = 0; k < n; k++) {
        int64_t u2 = w->pu[k], v = w->pv[k] - PAD + w->lo;
        xy[2 * k] = (u2 + v) / 2; xy[2 * k + 1] = (u2 - v) / 2;
    }
    return w->n;
}

/* ------------------------------------------------------------------------
 * Tiled multithreaded run (same result as world_run). Every epoch of D
 * steps, the particles are split by v (across the band) into T ranges with
 * equal counts. Thread i copies the particles within G = 2*D + 4 of its
 * range into a private bitmap, runs the D steps on that window alone and
 * keeps only the particles that end inside its own range. Nothing moves
 * faster than 2 in v per step, so whatever the window gets wrong near its
 * edges cannot reach the range in D steps: the kept particles are exact.
 * (Cutting along u does worse: the lanes travel along u together, so u
 * ranges get narrower than their margins.) Reads are done by the thread
 * owning the read cell's range. */

typedef struct {
    uint64_t *mov;                        /* private moving bits (full size) */
    int64_t *p, *np; int32_t *pu, *pv, *nu, *nv; int64_t n, cap;
    int32_t lo, hi, wlo, whi;             /* own range and window, in pv */
    int64_t *fe; int64_t nfe, fcap;       /* feeds in the window: (t, i, u, v) x4 */
    int64_t *rd; int64_t nrd, rcap;       /* my reads this epoch: (t, i, j) x3 */
} Tile;

typedef struct {
    World *w; int T; int64_t t0, t1; uint8_t *out;
    Tile *tl; int32_t *cuts;              /* T+1 range ends in pv */
    _Alignas(64) atomic_int count; _Alignas(64) atomic_int sense; int stop;
} TP;

#include <time.h>
static double prof[4];  /* serial setup, parallel epoch (thread 0), merge, max tile particles */
static inline double now(void) { struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts); return ts.tv_sec + 1e-9 * ts.tv_nsec; }
void world_prof(double *o) { for (int j = 0; j < 4; j++) { o[j] = prof[j]; prof[j] = 0; } }

static void tbar(TP *M, int *local) {
    *local = !*local;
    if (atomic_fetch_add(&M->count, 1) == M->T - 1) { atomic_store(&M->count, 0); atomic_store(&M->sense, *local); }
    else while (atomic_load_explicit(&M->sense, memory_order_acquire) != *local) ;
}

static void tl_grow(Tile *t, int64_t need) {
    if (need <= t->cap) return;
    while (t->cap < need) t->cap = t->cap ? 2 * t->cap : 1024;
    t->p = realloc(t->p, t->cap * 8); t->np = realloc(t->np, t->cap * 8);
    t->pu = realloc(t->pu, t->cap * 4); t->pv = realloc(t->pv, t->cap * 4);
    t->nu = realloc(t->nu, t->cap * 4); t->nv = realloc(t->nv, t->cap * 4);
}

static void tile_add(Tile *t, int64_t i, int32_t u, int32_t v) {
    if (getb(t->mov, i)) return;
    tl_grow(t, t->n + 1);
    setb(t->mov, i);
    t->p[t->n] = i; t->pu[t->n] = u; t->pv[t->n] = v; t->n++;
}

static void tile_step(World *w, Tile *t, int o) {
    const int64_t S = w->S, DX = S + 1, DY = S - 1, N = w->N;
    const int32_t U2 = (int32_t)w->U2, lo = (int32_t)w->lo;
    uint64_t *mov = t->mov; const uint64_t *wall = w->wall;
    static const int32_t DU[4] = {0, 1, 1, 2}, DV[4] = {0, 1, -1, 0};
    int64_t m = 0;
    tl_grow(t, 4 * t->n + 4);
    int64_t *np = t->np; int32_t *nu = t->nu, *nv = t->nv;
    for (int64_t k = 0; k < t->n; k++) {
        int64_t i = t->p[k];
        if (!getb(mov, i)) continue;
        int32_t u2 = t->pu[k], vv = t->pv[k], v = vv - PAD + lo;
        int rx = (((u2 + v) >> 1) - o) & 1, ry = (((u2 - v) >> 1) - o) & 1;
        int me = rx + 2 * ry;
        int64_t c0 = i - rx * DX - ry * DY;
        int32_t tu = u2 - DU[me], tv = vv - DV[me];
        if (c0 < 0) c0 += N;
        if (tu < 0) tu += U2;
        int64_t ci[4] = {c0, c0 + DX, c0 + DY, c0 + DX + DY};
        int mv[4], wl[4], f[4], cnt = 0;
        for (int c = 0; c < 4; c++) {
            if (ci[c] >= N) ci[c] -= N;
            mv[c] = getb(mov, ci[c]); wl[c] = getb(wall, ci[c]);
            f[c] = mv[c] | wl[c]; cnt += f[c];
        }
        int g[4];
        if (cnt == 1) { for (int c = 0; c < 4; c++) g[c] = f[3 - c]; }
        else if (cnt == 2 && f[0] && f[3]) { g[0] = 0; g[3] = 0; g[1] = 1; g[2] = 1; }
        else if (cnt == 2 && f[1] && f[2]) { g[1] = 0; g[2] = 0; g[0] = 1; g[3] = 1; }
        else { for (int c = 0; c < 4; c++) g[c] = f[c]; }
        for (int c = 0; c < 4; c++) {
            if (mv[c]) clrb(mov, ci[c]);
            if (wl[c] && !g[c]) w->broken = 1;   /* only trusted inside own range; rechecked serially if set */
        }
        for (int c = 0; c < 4; c++) {
            if (!g[c] || wl[c]) continue;
            int32_t nvv = tv + DV[c];
            if (nvv < t->wlo || nvv >= t->whi) continue;   /* left the window (or the band) */
            int32_t cu = tu + DU[c]; if (cu >= U2) cu -= U2;
            np[m] = ci[c]; nu[m] = cu; nv[m] = nvv; m++;
        }
    }
    for (int64_t k = 0; k < m; k++) setb(mov, np[k]);
    t->np = t->p; t->p = np;
    int32_t *tmp = t->pu; t->pu = nu; t->nu = tmp;
    tmp = t->pv; t->pv = nv; t->nv = tmp;
    t->n = m;
}

typedef struct { TP *M; int id; } TArg;

static void tile_epoch(TP *M, int id) {
    World *w = M->w; Tile *t = &M->tl[id];
    int32_t G = 2 * (int32_t)(M->t1 - M->t0) + 4, vmax = PAD + (int32_t)w->V2;
    t->lo = M->cuts[id]; t->hi = M->cuts[id + 1];
    t->wlo = t->lo - G > PAD ? t->lo - G : PAD;
    t->whi = t->hi + G < vmax ? t->hi + G : vmax;
    t->n = 0;
    for (int64_t k = 0; k < w->n; k++)
        if (w->pv[k] >= t->wlo && w->pv[k] < t->whi) tile_add(t, w->p[k], w->pu[k], w->pv[k]);
    for (int64_t tt = M->t0; tt < M->t1; tt++) {
        for (int64_t j = 0; j < t->nfe; j++)   /* feeds due now inside the window */
            if (t->fe[4 * j] == tt) tile_add(t, t->fe[4 * j + 1], (int32_t)t->fe[4 * j + 2], (int32_t)t->fe[4 * j + 3]);
        for (int64_t j = 0; j < t->nrd; j++)   /* reads of cells in my own range */
            if (t->rd[3 * j] == tt) M->out[t->rd[3 * j + 2]] = (uint8_t)getb(t->mov, t->rd[3 * j + 1]);
        tile_step(w, t, (int)(tt & 1));
    }
    for (int64_t k = 0; k < t->n; k++) clrb(t->mov, t->p[k]);  /* leave the private map clean */
    int64_t keep = 0;  /* keep only what ended in my own range */
    for (int64_t q = 0; q < t->n; q++) {
        if (t->pv[q] < t->lo || t->pv[q] >= t->hi) continue;
        t->p[keep] = t->p[q]; t->pu[keep] = t->pu[q]; t->pv[keep] = t->pv[q]; keep++;
    }
    t->n = keep;
}

static void *tile_worker(void *arg) {
    TP *M = ((TArg *)arg)->M; int id = ((TArg *)arg)->id;
    int local = 0;
    while (1) {
        tbar(M, &local);              /* thread 0 has set up the epoch */
        if (M->stop) break;
        tile_epoch(M, id);
        tbar(M, &local);              /* results ready for thread 0 */
    }
    return NULL;
}

#define HB 16  /* histogram bin width in pv for the cuts */

void world_run_tiles(World *w, int64_t t_end, int64_t nf, const int64_t *feeds,
                     int64_t nr, const int64_t *reads, uint8_t *out, int T, int64_t D) {
    TP M; memset(&M, 0, sizeof M);
    M.w = w; M.T = T; M.out = out;
    M.tl = calloc(T, sizeof(Tile));
    int64_t words = w->N / 64 + 1;
    if (w->ntile_mov < T) {  /* allocate once and touch now: page faults later would stall an epoch */
        w->tile_mov = realloc(w->tile_mov, T * sizeof(uint64_t *));
        for (int k = w->ntile_mov; k < T; k++) { w->tile_mov[k] = malloc(words * 8); memset(w->tile_mov[k], 0, words * 8); }
        w->ntile_mov = T;
    }
    for (int k = 0; k < T; k++) M.tl[k].mov = w->tile_mov[k];
    M.cuts = malloc((T + 1) * 4);
    int64_t nb = w->S / HB + 1, *hist = malloc(nb * 8);
    pthread_t th[T]; TArg args[T];
    for (int k = 1; k < T; k++) { args[k] = (TArg){&M, k}; pthread_create(&th[k], NULL, tile_worker, &args[k]); }
    int local = 0;
    int64_t fi = 0, ri = 0;
    while (fi < nf && feeds[3 * fi] < w->t) fi++;
    while (ri < nr && reads[3 * ri] < w->t) ri++;
    /* serial particle list lives in w->p/pu/pv; w->mov is not used in between */
    for (int64_t k = 0; k < w->n; k++) clrb(w->mov, w->p[k]);
    while (w->t < t_end) {
        double c0 = now();
        M.t0 = w->t; M.t1 = w->t + D < t_end ? w->t + D : t_end;
        /* equal-count ranges from a coarse histogram of v */
        memset(hist, 0, nb * 8);
        for (int64_t k = 0; k < w->n; k++) hist[w->pv[k] / HB]++;
        M.cuts[0] = 0; M.cuts[T] = (int32_t)w->S;
        int64_t acc = 0; int k = 1;
        for (int64_t b = 0; b < nb && k < T; b++) {
            acc += hist[b];
            while (k < T && acc * T >= w->n * k) M.cuts[k++] = (int32_t)((b + 1) * HB);
        }
        for (; k < T; k++) M.cuts[k] = (int32_t)w->S;
        /* feeds of this epoch, to every tile whose window holds them */
        int32_t G = 2 * (int32_t)(M.t1 - M.t0) + 4;
        for (int k = 0; k < T; k++) M.tl[k].nfe = 0;
        for (; fi < nf && feeds[3 * fi] < M.t1; fi++) {
            int64_t i = index_of(w, feeds[3 * fi + 1], feeds[3 * fi + 2]);
            if (i < 0) continue;
            int32_t v = (int32_t)(i % w->S);
            for (int k = 0; k < T; k++) {
                if (v < M.cuts[k] - G || v >= M.cuts[k + 1] + G) continue;
                Tile *t = &M.tl[k];
                if (t->fcap < t->nfe + 1) { t->fcap = 2 * (t->nfe + 1); t->fe = realloc(t->fe, t->fcap * 4 * 8); }
                int64_t *e = t->fe + 4 * t->nfe++;
                e[0] = feeds[3 * fi]; e[1] = i; e[2] = i / w->S; e[3] = v;
            }
        }
        for (int k = 0; k < T; k++) M.tl[k].nrd = 0;
        for (; ri < nr && reads[3 * ri] < M.t1; ri++) {
            int64_t i = index_of(w, reads[3 * ri + 1], reads[3 * ri + 2]);
            if (i < 0) continue;
            int32_t v = (int32_t)(i % w->S);
            int k = 0;
            while (k < T - 1 && v >= M.cuts[k + 1]) k++;
            Tile *t = &M.tl[k];
            if (t->rcap < t->nrd + 1) { t->rcap = 2 * (t->nrd + 1); t->rd = realloc(t->rd, t->rcap * 3 * 8); }
            int64_t *e = t->rd + 3 * t->nrd++;
            e[0] = reads[3 * ri]; e[1] = i; e[2] = ri;
        }
        double c1 = now();
        tbar(&M, &local);
        tile_epoch(&M, 0);
        tbar(&M, &local);
        double c2 = now();
        for (int k = 0; k < T; k++) if (M.tl[k].n > prof[3]) prof[3] = M.tl[k].n;
        /* merge: each tile keeps the particles that ended in its own range */
        int64_t tot = 0;
        for (int k = 0; k < T; k++) tot += M.tl[k].n;
        grow(w, tot + 1);
        w->n = 0;
        for (int k = 0; k < T; k++) {
            Tile *t = &M.tl[k];
            memcpy(w->p + w->n, t->p, t->n * 8); memcpy(w->pu + w->n, t->pu, t->n * 4); memcpy(w->pv + w->n, t->pv, t->n * 4);
            w->n += t->n;
        }
        w->t = M.t1;
        prof[0] += c1 - c0; prof[1] += c2 - c1; prof[2] += now() - c2;
    }
    M.stop = 1;
    tbar(&M, &local);
    for (int k = 1; k < T; k++) pthread_join(th[k], NULL);
    for (int64_t k = 0; k < w->n; k++) setb(w->mov, w->p[k]);
    for (int k = 0; k < T; k++) { Tile *t = &M.tl[k]; free(t->p); free(t->np); free(t->pu); free(t->pv); free(t->nu); free(t->nv); free(t->fe); free(t->rd); }
    free(M.tl); free(M.cuts); free(hist);
}
