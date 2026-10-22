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
 * before the step at that time. reads: sorted (t, x, y); out[k] = occupied. */
void world_run(World *w, int64_t t_end, int64_t nf, const int64_t *feeds,
               int64_t nr, const int64_t *reads, uint8_t *out) {
    int64_t fi = 0, ri = 0;
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
    }
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
 * steps, the particles are split by u2 into T ranges with equal counts.
 * Thread i copies the particles within G = 2*D + 4 rows of its range into a
 * private bitmap, runs the D steps on that window alone and keeps only the
 * particles that end inside its own range. Nothing moves faster than 2 rows
 * of u2 per step, so whatever the window gets wrong near its edges cannot
 * reach the range in D steps: the kept particles are exact. One barrier per
 * epoch. Reads are done by the thread owning the read cell's range. */

typedef struct {
    uint64_t *mov;                        /* private moving bits (full size) */
    int64_t *p, *np; int32_t *pu, *pv, *nu, *nv; int64_t n, cap;
    int64_t lo, hi, wlo, whi;             /* own range and window, in u2 (unwrapped) */
    int64_t *fe; int64_t nfe, fcap;       /* feeds in the window: (t, i, u, v) x4 */
    int64_t *rd; int64_t nrd, rcap;       /* my reads this epoch: (t, i, j) x3 */
} Tile;

typedef struct {
    World *w; int T; int64_t D, t_end;
    const int64_t *feeds, *reads; int64_t nf, nr; uint8_t *out;
    Tile *tl;
    int64_t *su; int64_t ns, scap;        /* all particles sorted by u2: (u2, i, v) x3 */
    int64_t *glob; int64_t ng, gcap;      /* merged results */
    int64_t t0, t1, start;                /* epoch [t0, t1); start = u2 where ranges begin */
    int64_t *cuts;                        /* T+1 range ends (unwrapped from start) */
    _Alignas(64) atomic_int count; _Alignas(64) atomic_int sense; int stop;
} TP;

#include <time.h>
#include <limits.h>
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

/* distance from `start` going up the ring */
static inline int64_t ring(World *w, int64_t start, int64_t u) { int64_t d = u - start; return d < 0 ? d + w->U2 : d; }

static void tile_add(World *w, Tile *t, int64_t i, int32_t u, int32_t v) {
    if (getb(t->mov, i)) return;
    tl_grow(t, t->n + 1);
    setb(t->mov, i);
    t->p[t->n] = i; t->pu[t->n] = u; t->pv[t->n] = v; t->n++;
}

static void tile_step(World *w, Tile *t, int o, int64_t start) {
    const int64_t S = w->S, DX = S + 1, DY = S - 1, N = w->N;
    const int32_t U2 = (int32_t)w->U2, lo = (int32_t)w->lo, vmax = PAD + (int32_t)w->V2;
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
            if (nvv < PAD || nvv >= vmax) continue;
            int32_t cu = tu + DU[c]; if (cu >= U2) cu -= U2;
            int64_t d = ring(w, start, cu);           /* windows may hang over the ring ends */
            if ((d < t->wlo || d >= t->whi) && (d - w->U2 < t->wlo || d - w->U2 >= t->whi)
                && (d + w->U2 < t->wlo || d + w->U2 >= t->whi)) continue;   /* left the window */
            np[m] = ci[c]; nu[m] = cu; nv[m] = nvv; m++;
        }
    }
    for (int64_t k = 0; k < m; k++) setb(mov, np[k]);
    t->np = t->p; t->p = np;
    int32_t *tmp = t->pu; t->pu = nu; t->nu = tmp;
    tmp = t->pv; t->pv = nv; t->nv = tmp;
    t->n = m;
}

static int64_t lower_bound_u(TP *M, int64_t d) {  /* first sorted particle with ring dist >= d */
    int64_t a = 0, b = M->ns;
    while (a < b) { int64_t c = (a + b) / 2; if (M->su[3 * c] < d) a = c + 1; else b = c; }
    return a;
}

typedef struct { TP *M; int id; } TArg;

static void tile_epoch(TP *M, int id) {
    World *w = M->w; Tile *t = &M->tl[id];
    int64_t G = 2 * (M->t1 - M->t0) + 4, R = w->U2;
    t->lo = M->cuts[id]; t->hi = M->cuts[id + 1];
    t->wlo = t->lo - G; t->whi = t->hi + G;
    /* gather window particles (ring distances may wrap: check three images) */
    t->n = 0;
    for (int img = -1; img <= 1; img++) {
        int64_t a = t->wlo - img * R, b = t->whi - img * R;
        int64_t k = lower_bound_u(M, a < 0 ? 0 : a);
        for (; k < M->ns && M->su[3 * k] < b; k++) {
            int64_t d = M->su[3 * k] + img * R;
            if (d < t->wlo || d >= t->whi) continue;
            int64_t i = M->su[3 * k + 1]; int32_t v = (int32_t)M->su[3 * k + 2];
            tile_add(w, t, i, (int32_t)((M->su[3 * k] + M->start) % R), v);
        }
    }
    for (int64_t tt = M->t0; tt < M->t1; tt++) {
        /* feeds due now inside the window */
        for (int64_t j = 0; j < t->nfe; j++)
            if (t->fe[4 * j] == tt) tile_add(w, t, t->fe[4 * j + 1], (int32_t)t->fe[4 * j + 2], (int32_t)t->fe[4 * j + 3]);
        for (int64_t j = 0; j < t->nrd; j++)   /* reads of cells in my own range */
            if (t->rd[3 * j] == tt) M->out[t->rd[3 * j + 2]] = (uint8_t)getb(t->mov, t->rd[3 * j + 1]);
        tile_step(w, t, (int)(tt & 1), M->start);
    }
    for (int64_t k = 0; k < t->n; k++) clrb(t->mov, t->p[k]);  /* leave the private map clean */
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

static int cmp3(const void *a, const void *b) {
    int64_t x = ((const int64_t *)a)[0], y = ((const int64_t *)b)[0]; return x < y ? -1 : x > y;
}

void world_run_tiles(World *w, int64_t t_end, int64_t nf, const int64_t *feeds,
                     int64_t nr, const int64_t *reads, uint8_t *out, int T, int64_t D) {
    TP M; memset(&M, 0, sizeof M);
    M.w = w; M.T = T; M.D = D; M.t_end = t_end; M.feeds = feeds; M.reads = reads; M.nf = nf; M.nr = nr; M.out = out;
    for (int64_t j = 0; j < nr; j++) out[j] = 0;
    M.tl = calloc(T, sizeof(Tile));
    int64_t words = w->N / 64 + 1;
    if (w->ntile_mov < T) {  /* allocate once: first touches of fresh pages are slow */
        w->tile_mov = realloc(w->tile_mov, T * sizeof(uint64_t *));
        for (int k = w->ntile_mov; k < T; k++) w->tile_mov[k] = calloc(words, 8);
        w->ntile_mov = T;
    }
    for (int k = 0; k < T; k++) M.tl[k].mov = w->tile_mov[k];
    M.cuts = malloc((T + 1) * 8);
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
        /* sort particles by u2; start the ring at the widest gap */
        if (M.scap < w->n + 1) { M.scap = 2 * (w->n + 1); M.su = realloc(M.su, M.scap * 3 * 8); }
        for (int64_t k = 0; k < w->n; k++) { M.su[3 * k] = w->pu[k]; M.su[3 * k + 1] = w->p[k]; M.su[3 * k + 2] = w->pv[k]; }
        M.ns = w->n;
        qsort(M.su, M.ns, 3 * 8, cmp3);
        int64_t start = 0, gap = -1;
        for (int64_t k = 0; k < M.ns; k++) {
            int64_t a = M.su[3 * k], b = k + 1 < M.ns ? M.su[3 * (k + 1)] : M.su[0] + w->U2;
            if (b - a > gap) { gap = b - a; start = (a + (b - a) / 2) % w->U2; }
        }
        M.start = start;
        for (int64_t k = 0; k < M.ns; k++) M.su[3 * k] = ring(w, start, M.su[3 * k]);
        qsort(M.su, M.ns, 3 * 8, cmp3);
        /* equal-count ranges over ring distances; the last range takes the rest of the ring */
        M.cuts[0] = 0; M.cuts[T] = w->U2;
        for (int k = 1; k < T; k++) {
            int64_t idx = M.ns * k / T;
            M.cuts[k] = M.ns ? (idx < M.ns ? M.su[3 * idx] : M.su[3 * (M.ns - 1)] + 1) : w->U2 * k / T;
            if (M.cuts[k] < M.cuts[k - 1]) M.cuts[k] = M.cuts[k - 1];
        }
        /* feeds of this epoch, to every tile whose window holds them */
        int64_t G = 2 * (M.t1 - M.t0) + 4;
        for (int k = 0; k < T; k++) M.tl[k].nfe = 0;
        for (int64_t j = fi; j < nf && feeds[3 * j] < M.t1; j++) {
            int64_t i = index_of(w, feeds[3 * j + 1], feeds[3 * j + 2]);
            if (i < 0) continue;
            int64_t u = i / w->S, d = ring(w, start, u);
            for (int k = 0; k < T; k++) {
                Tile *t = &M.tl[k];
                int64_t lo = M.cuts[k] - G, hi = M.cuts[k + 1] + G;
                if ((d >= lo && d < hi) || (d - w->U2 >= lo && d - w->U2 < hi) || (d + w->U2 >= lo && d + w->U2 < hi)) {
                    if (t->fcap < t->nfe + 1) { t->fcap = 2 * (t->nfe + 1); t->fe = realloc(t->fe, t->fcap * 4 * 8); }
                    int64_t *e = t->fe + 4 * t->nfe++;
                    e[0] = feeds[3 * j]; e[1] = i; e[2] = u; e[3] = i % w->S;
                }
            }
        }
        while (fi < nf && feeds[3 * fi] < M.t1) fi++;
        for (int k = 0; k < T; k++) M.tl[k].nrd = 0;
        for (; ri < nr && reads[3 * ri] < M.t1; ri++) {
            if (reads[3 * ri] < M.t0) continue;
            int64_t i = index_of(w, reads[3 * ri + 1], reads[3 * ri + 2]);
            if (i < 0) continue;
            int64_t d = ring(w, start, i / w->S);
            int k = 0;
            while (k < T - 1 && d >= M.cuts[k + 1]) k++;
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
        w->n = 0;
        for (int k = 0; k < T; k++) {
            Tile *t = &M.tl[k];
            grow(w, w->n + t->n + 1);
            for (int64_t q = 0; q < t->n; q++) {
                int64_t d = ring(w, start, t->pu[q]);
                if (d < M.cuts[k] || d >= M.cuts[k + 1]) continue;
                w->p[w->n] = t->p[q]; w->pu[w->n] = t->pu[q]; w->pv[w->n] = t->pv[q]; w->n++;
            }
        }
        w->t = M.t1;
        prof[0] += c1 - c0; prof[1] += c2 - c1; prof[2] += now() - c2;
    }
    M.stop = 1;
    tbar(&M, &local);
    for (int k = 1; k < T; k++) pthread_join(th[k], NULL);
    for (int64_t k = 0; k < w->n; k++) setb(w->mov, w->p[k]);
    for (int k = 0; k < T; k++) { Tile *t = &M.tl[k]; free(t->p); free(t->np); free(t->pu); free(t->pv); free(t->nu); free(t->nv); free(t->fe); free(t->rd); }
    free(M.tl); free(M.cuts); free(M.su);
}
