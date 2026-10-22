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
    return w;
}

void world_free(World *w) { free(w->wall); free(w->mov); free(w->p); free(w->np); free(w); }

static void grow(World *w, int64_t need) {
    if (need <= w->cap) return;
    while (w->cap < need) w->cap *= 2;
    w->p = realloc(w->p, w->cap * 8); w->np = realloc(w->np, w->cap * 8);
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
        w->p[w->n++] = i;
    }
}

static void step(World *w) {
    const int o = (int)(w->t & 1);
    const int64_t S = w->S, DX = S + 1, DY = S - 1, lo = w->lo;
    uint64_t *mov = w->mov, *wall = w->wall;
    int64_t m = 0;
    grow(w, 4 * w->n + 4);
    int64_t *np = w->np;
    for (int64_t k = 0; k < w->n; k++) {
        int64_t i = w->p[k];
        if (!getb(mov, i)) continue;  /* its block was already handled */
        int64_t u2 = i / S, v = i - u2 * S - PAD + lo;
        /* x = (u2 + v)/2, y = (u2 - v)/2; corner of this cell in its block */
        int64_t x = (u2 + v) >> 1, y = (u2 - v) >> 1;
        int rx = (int)((x - o) & 1), ry = (int)((y - o) & 1);
        int64_t c0 = wrap(w, i - rx * DX - ry * DY);   /* TL */
        int64_t ci[4] = {c0, wrap(w, c0 + DX), wrap(w, c0 + DY), wrap(w, c0 + DX + DY)};
        int mv[4], wl[4], f[4], cnt = 0;
        for (int c = 0; c < 4; c++) {
            mv[c] = getb(mov, ci[c]); wl[c] = getb(wall, ci[c]);
            f[c] = mv[c] | wl[c]; cnt += f[c];
        }
        int me = rx + 2 * ry;
        if (cnt == 1) {  /* lone particle: to the opposite corner */
            clrb(mov, i);
            np[m++] = ci[3 - me];
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
            if (g[c] && !wl[c]) np[m++] = ci[c];
    }
    /* write new positions; drop those in the padding (outside the band) */
    int64_t kept = 0;
    for (int64_t k = 0; k < m; k++) {
        int64_t i = np[k];
        if (!inside(w, i)) continue;
        setb(mov, i);
        np[kept++] = i;
    }
    w->np = w->p; w->p = np;
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
        int64_t i = w->p[k], u2 = i / w->S, v = i - u2 * w->S - PAD + w->lo;
        xy[2 * k] = (u2 + v) / 2; xy[2 * k + 1] = (u2 - v) / 2;
    }
    return w->n;
}
