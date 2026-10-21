import random
from sim import Grid, direction, VEC


def ref_step(cells, o, w, h):
    """Independent set-based BBMCA step (same edge rule: incomplete blocks unchanged)."""
    blocks, out = {}, set()
    for x, y in cells:
        bx, by = (x - o) // 2, (y - o) // 2
        if x < o or y < o or bx >= (w - o) // 2 or by >= (h - o) // 2:
            out.add((x, y))
            continue
        blocks.setdefault((bx, by), set()).add(((x - o) % 2, (y - o) % 2))
    for (bx, by), s in blocks.items():
        if len(s) == 1:
            (cx, cy), = s
            s = {(1 - cx, 1 - cy)}
        elif s == {(0, 0), (1, 1)}:
            s = {(1, 0), (0, 1)}
        elif s == {(1, 0), (0, 1)}:
            s = {(0, 0), (1, 1)}
        out |= {(2 * bx + o + cx, 2 * by + o + cy) for cx, cy in s}
    return out


def grid(w, h, cells):
    g = Grid(w, h)
    for x, y in cells:
        g.set(x, y)
    return g


def dimer(x, y, d):
    """Ball = lead particle at (x, y) plus a trailer two cells behind on the same diagonal."""
    dx, dy = VEC[d]
    return {(x, y), (x - 2 * dx, y - 2 * dy)}


def bar(x0, y0, wd, ht):
    return {(x, y) for x in range(x0, x0 + wd) for y in range(y0, y0 + ht)}


def balls(cells, t):
    """Split moving cells into dimers -> sorted [(lead, dir)]; None if any particle is unpaired."""
    by = {}
    for p in cells:
        by.setdefault(direction(*p, t), set()).add(p)
    out = []
    for d, ps in by.items():
        dx, dy = VEC[d]
        for p in sorted(ps):
            q = (p[0] + 2 * dx, p[1] + 2 * dy)
            if p in ps and q in ps:
                ps -= {p, q}
                out.append((q, d))
        if ps:
            return None
    return sorted(out)


def test_matches_reference_on_random_grids():
    rng = random.Random(0)
    for w, h in [(10, 8), (11, 9), (16, 16), (7, 12)]:
        cells = {(x, y) for x in range(w) for y in range(h) if rng.random() < 0.3}
        g = grid(w, h, cells)
        for t in range(40):
            cells = ref_step(cells, t % 2, w, h)
            g.step()
            assert g.cells() == cells, (w, h, t)


def test_lone_ball_flight():
    for d, (dx, dy) in VEC.items():
        g = Grid(40, 40)
        # pick a start cell whose parity makes it move in direction d at t=0
        x, y = next((x, y) for x in (20, 21) for y in (20, 21) if direction(x, y, 0) == d)
        g.set(x, y)
        g.step(10)
        assert g.cells() == {(x + 10 * dx, y + 10 * dy)}, d


def test_reversible_and_conserving():
    rng = random.Random(1)
    g = Grid(33, 21)
    g.g = rng.getrandbits(33 * 21)
    start, n = g.g, bin(g.g).count('1')
    for _ in range(50):
        g.step()
        assert bin(g.g).count('1') == n
    g.unstep(50)
    assert g.g == start and g.t == 0


def test_static_walls():
    assert all(grid(20, 20, bar(x, y, 2, 2)).cells() == bar(x, y, 2, 2) for x, y in [(5, 6), (6, 5)])
    for x, y, wd, ht, static in [(5, 6, 2, 2, True), (6, 6, 2, 2, False), (5, 6, 8, 2, True),
                                 (5, 6, 7, 2, False), (5, 6, 2, 8, True), (6, 6, 8, 2, False)]:
        g = grid(30, 30, bar(x, y, wd, ht))
        g.step(2)
        assert (g.cells() == bar(x, y, wd, ht)) == static, (x, y, wd, ht)


def test_single_particle_bounces_straight_back():
    wall = bar(1, 40, 60, 2)
    g = grid(64, 64, wall | {(10, 10)})
    g.step(60)
    rest = g.cells() - wall
    assert wall <= g.cells() and len(rest) == 1 and direction(*rest.pop(), 60) == 'NW'


def test_dimer_reflects_90_off_bars():
    hz, vt = bar(1, 80, 158, 2), bar(80, 1, 2, 158)  # 2-thick, TL x+y odd, even length
    # (wall, incoming dir, start region, expected outgoing dir)
    cases = [(hz, 'SE', (70, 60), 'NE'), (hz, 'SW', (90, 60), 'NW'),
             (hz, 'NE', (70, 100), 'SE'), (hz, 'NW', (90, 100), 'SW'),
             (vt, 'SE', (60, 70), 'SW'), (vt, 'NE', (60, 90), 'NW'),
             (vt, 'SW', (100, 70), 'SE'), (vt, 'NW', (100, 90), 'NE')]
    for wall, din, (x0, y0), dout in cases:
        for shift in range(0, 8, 2):
            x, y = next((x, y) for x in (x0 + shift, x0 + shift + 1) for y in (y0, y0 + 1)
                        if direction(x, y, 0) == din)
            g = grid(160, 160, wall | dimer(x, y, din))
            g.step(40)
            assert wall <= g.cells(), (din, shift)
            out = balls(g.cells() - wall, 40)
            assert out is not None and [d for _, d in out] == [dout], (din, dout, shift, out)


def test_dimers_head_on_rotate_cleanly():
    for k in range(4):
        a = dimer(20 + 2 * k, 20 + 2 * k, 'SE')
        b = dimer(41 + 2 * k, 41 + 2 * k, 'NW')
        g = grid(100, 100, a | b)
        g.step(40)
        out = balls(g.cells(), 40)
        assert out is not None and sorted(d for _, d in out) == ['NE', 'SW'], (k, out)
        # either ball alone just passes the meeting point
        for one, d in [(a, 'SE'), (b, 'NW')]:
            g = grid(100, 100, one)
            g.step(40)
            assert [d2 for _, d2 in balls(g.cells(), 40)] == [d]
