/* Exact BBMCA on the diagonal cylinder, same rule as cyl.py (which it is
 * tested against). Cells are stored by (u2, v2) = (x + y mod 2U, x - y - lo)
 * in two bitmaps: static walls and moving particles. Each step visits only
 * the moving particles; a block is processed once (its moving bits are
 * cleared as it is handled) and new positions are written afterwards. */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
    int64_t U2, lo, hi, V2;      /* U2 = 2U, V2 = hi - lo + 1 */
    uint64_t *wall, *mov;
    int64_t *pu, *pv, *nu, *nv;  /* particle positions (u2, v2) now / next */
    int64_t n, cap;
    int64_t t;
    int broken;
} World;

static inline int64_t idx(World *w, int64_t u2, int64_t v2) { return u2 * w->V2 + v2; }
static inline int getb(uint64_t *b, int64_t i) { return (b[i >> 6] >> (i & 63)) & 1; }
static inline void setb(uint64_t *b, int64_t i) { b[i >> 6] |= 1ULL << (i & 63); }
static inline void clrb(uint64_t *b, int64_t i) { b[i >> 6] &= ~(1ULL << (i & 63)); }

static inline int64_t mod(int64_t a, int64_t m) { int64_t r = a % m; return r < 0 ? r + m : r; }

World *world_new(int64_t U, int64_t lo, int64_t hi) {
    World *w = calloc(1, sizeof(World));
    w->U2 = 2 * U; w->lo = lo; w->hi = hi; w->V2 = hi - lo + 1;
    int64_t words = (w->U2 * w->V2 + 63) / 64 + 1;
    w->wall = calloc(words, 8);
    w->mov = calloc(words, 8);
    w->cap = 1024;
    w->pu = malloc(w->cap * 8); w->pv = malloc(w->cap * 8);
    w->nu = malloc(w->cap * 8); w->nv = malloc(w->cap * 8);
    return w;
}

void world_free(World *w) {
    free(w->wall); free(w->mov); free(w->pu); free(w->pv); free(w->nu); free(w->nv); free(w);
}

static void grow(World *w, int64_t need) {
    if (need <= w->cap) return;
    while (w->cap < need) w->cap *= 2;
    w->pu = realloc(w->pu, w->cap * 8); w->pv = realloc(w->pv, w->cap * 8);
    w->nu = realloc(w->nu, w->cap * 8); w->nv = realloc(w->nv, w->cap * 8);
}

/* (x, y) -> (u2, v2); returns 0 if outside the band */
static inline int to_uv(World *w, int64_t x, int64_t y, int64_t *u2, int64_t *v2) {
    int64_t v = x - y;
    if (v < w->lo || v > w->hi) return 0;
    *u2 = mod(x + y, w->U2);
    *v2 = v - w->lo;
    return 1;
}

void world_add(World *w, int64_t n, const int64_t *xy, int wall) {
    grow(w, w->n + n);
    for (int64_t k = 0; k < n; k++) {
        int64_t u2, v2;
        if (!to_uv(w, xy[2 * k], xy[2 * k + 1], &u2, &v2)) continue;
        int64_t i = idx(w, u2, v2);
        if (wall) { setb(w->wall, i); continue; }
        if (getb(w->mov, i)) continue;
        setb(w->mov, i);
        w->pu[w->n] = u2; w->pv[w->n] = v2; w->n++;
    }
}

static void step(World *w) {
    int o = (int)(w->t & 1);
    int64_t m = 0;
    grow(w, 4 * w->n + 4);
    for (int64_t k = 0; k < w->n; k++) {
        int64_t u2 = w->pu[k], v2 = w->pv[k];
        if (!getb(w->mov, idx(w, u2, v2))) continue;  /* block already handled */
        /* back to an (x, y) representative: x = (u2 + v)/2, y = (u2 - v)/2 */
        int64_t v = v2 + w->lo;
        int64_t x = (u2 + v) / 2, y = (u2 - v) / 2;
        int64_t tx = x - ((x - o) & 1), ty = y - ((y - o) & 1);
        int64_t cu[4], cv[4], ci[4];
        int ok[4], mv[4], wl[4];
        for (int c = 0; c < 4; c++) {
            int64_t cx = tx + (c & 1), cy = ty + (c >> 1);
            ok[c] = to_uv(w, cx, cy, &cu[c], &cv[c]);
            ci[c] = ok[c] ? idx(w, cu[c], cv[c]) : -1;
            mv[c] = ok[c] ? getb(w->mov, ci[c]) : 0;
            wl[c] = ok[c] ? getb(w->wall, ci[c]) : 0;
        }
        /* cells: 0=TL 1=TR 2=BL 3=BR; full occupancy */
        int f[4], g[4], cnt = 0;
        for (int c = 0; c < 4; c++) { f[c] = mv[c] | wl[c]; cnt += f[c]; }
        if (cnt == 1) {
            for (int c = 0; c < 4; c++) g[c] = f[3 - c];          /* rotate 180 */
        } else if (cnt == 2 && f[0] && f[3]) {
            g[0] = 0; g[3] = 0; g[1] = 1; g[2] = 1;
        } else if (cnt == 2 && f[1] && f[2]) {
            g[1] = 0; g[2] = 0; g[0] = 1; g[3] = 1;
        } else {
            for (int c = 0; c < 4; c++) g[c] = f[c];
        }
        for (int c = 0; c < 4; c++) {
            if (mv[c]) clrb(w->mov, ci[c]);
            if (wl[c] && !g[c]) w->broken = 1;
        }
        for (int c = 0; c < 4; c++) {
            if (g[c] && !wl[c]) {
                int64_t cx = tx + (c & 1), cy = ty + (c >> 1), uu, vv;
                if (to_uv(w, cx, cy, &uu, &vv)) { w->nu[m] = uu; w->nv[m] = vv; m++; }
            }
        }
    }
    for (int64_t k = 0; k < m; k++) setb(w->mov, idx(w, w->nu[k], w->nv[k]));
    int64_t *tu = w->pu, *tv = w->pv;
    w->pu = w->nu; w->pv = w->nv; w->nu = tu; w->nv = tv;
    w->n = m;
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
            int64_t u2, v2;
            out[ri] = to_uv(w, reads[3 * ri + 1], reads[3 * ri + 2], &u2, &v2) ? getb(w->mov, idx(w, u2, v2)) : 0;
            ri++;
        }
        step(w);
    }
}

int64_t world_count(World *w) { return w->n; }
int world_broken(World *w) { return w->broken; }
int64_t world_time(World *w) { return w->t; }
void world_set_time(World *w, int64_t t) { w->t = t; }

/* moving cells as (x, y) in canonical form (u2 in [0, 2U)) */
int64_t world_cells(World *w, int64_t *xy, int64_t cap) {
    int64_t n = w->n < cap ? w->n : cap;
    for (int64_t k = 0; k < n; k++) {
        int64_t v = w->pv[k] + w->lo, u2 = w->pu[k];
        xy[2 * k] = (u2 + v) / 2; xy[2 * k + 1] = (u2 - v) / 2;
    }
    return w->n;
}
