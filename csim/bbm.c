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

#define PAD 2

typedef struct {
    int64_t U2, lo, hi, V2, S, N;  /* N = U2 * S cells */
    uint64_t *wall, *mov;
    int64_t *p, *np;               /* particle cell indices now / next */
    int32_t *pu, *pv, *nu, *nv;    /* their u2 and (v2 + PAD) */
    int64_t n, cap;
    int64_t t;
    int broken;
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
