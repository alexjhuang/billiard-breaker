"""physics.py formulas vs the real cellular automaton (sim.py)."""
import random
from sim import Grid, direction, VEC
from physics import Ball, Bar, reflect, hit, interaction

OFF = 200  # shift everything so grid coordinates stay positive


def run(balls, bars, T, size=400):
    g = Grid(size, size)
    walls = set().union(*(b.cells() for b in bars)) if bars else set()
    for x, y in walls | set().union(*(b.cells(0) for b in balls)):
        g.set(x + OFF, y + OFF)
    g.step(T)
    cells = {(x - OFF, y - OFF) for x, y in g.cells()}
    if not walls <= cells:
        return None
    return decompose(cells - walls, T, sorted({b.s for b in balls}, reverse=True))


def decompose(cells, T, sizes):
    """Final cells -> sorted list of Balls (t=0 form), or None if any particle is unpaired."""
    out, by = [], {}
    for c in cells:
        by.setdefault(direction(*c, T), set()).add(c)
    for d, ps in by.items():
        dx, dy = VEC[d]
        for s in sizes:
            for c in sorted(ps):
                n = (c[0] + s * dx, c[1] + s * dy)
                if c in ps and n in ps:
                    ps -= {c, n}
                    out.append(Ball(d, s, n[0] - dx * T, n[1] - dy * T))
        if ps:
            return None
    return sorted(out)


def ball_near(d, s, x, y, t=0):
    """A valid Ball whose lead is at (about) (x, y) at step t."""
    dx, dy = VEC[d]
    for ox in (0, 1):
        for oy in (0, 1):
            if direction(x + ox, y + oy, t) == d:
                return Ball(d, s, x + ox - dx * t, y + oy - dy * t)


def test_reflect_formula():
    rng = random.Random(5)
    for _ in range(40):
        s, kind, d, k = rng.choice((2, 6)), rng.choice('HV'), rng.choice(list(VEC)), rng.randrange(-20, 20)
        bar = Bar(kind, -150 + (1 + (-150 + k) % 2) % 2, 150 + (k % 2 == 0), k)
        bar = bar._replace(b=bar.a + 299)
        assert bar.ok(), bar
        dx, dy = VEC[d]
        a = rng.randrange(-20, 21)
        if kind == 'H':
            b = ball_near(d, s, a, k - 30 if dy == 1 else k + 31)
        else:
            b = ball_near(d, s, k - 30 if dx == 1 else k + 31, a)
        assert run([b], [bar], 70) == [reflect(b, bar)], (b, bar)


def test_bar_ends():
    # sweep balls across both ends of short bars, every orientation and face
    for kind in 'HV':
        for d in VEC:
            dx, dy = VEC[d]
            for s in (2, 6):
                for length in (4, 10):
                    bar = Bar(kind, 1, length, 0)
                    for along in range(-8, length + 9):
                        # lead crosses the bar's near line at coordinate `along`
                        if kind == 'H':
                            near = 0 if dy == 1 else 1
                            b = ball_near(d, s, along - 30 * dx, near - 30 * dy, 0)
                        else:
                            near = 0 if dx == 1 else 1
                            b = ball_near(d, s, near - 30 * dx, along - 30 * dy, 0)
                        kind_t = hit(b, bar)
                        got = run([b], [bar], 70)
                        want = {'pass': [b], 'reflect': [reflect(b, bar)], 'shatter': None}[kind_t[0]]
                        if kind_t[0] == 'shatter':
                            assert got is None or got not in ([b], [reflect(b, bar)]), (b, bar, kind_t, got)
                        else:
                            assert got == want, (b, bar, kind_t, got)


def test_interaction_rule():
    rng = random.Random(7)
    pairs = [('SE', 'SW'), ('NE', 'NW'), ('SE', 'NE'), ('SW', 'NW')]
    n = 0
    while n < 150:
        da, db = rng.choice(pairs)
        sa, sb = rng.choice((2, 4, 6)), rng.choice((2, 4, 6))
        (ax, ay), (bx, by) = VEC[da], VEC[db]
        g = rng.choice([0, 0] + list(range(-10, 11, 2)))
        a = ball_near(da, sa, 0, 0, 40)
        lx, ly = a.lead(40)
        if ay == by:   # converge in x, offset g along shared y
            b = Ball(db, sb, 0, 0)
            pb = next((Ball(db, sb, lx + o - bx * 40 + (-9 * bx), ly + ay * g - by * 40) for o in (0, 1, -1)
                       if direction(lx + o + (-9 * bx), ly + ay * g, 40) == db), None)
        else:
            pb = next((Ball(db, sb, lx + bx * g - bx * 40, ly + o - by * 40 + (-9 * by)) for o in (0, 1, -1)
                       if direction(lx + bx * g, ly + o + (-9 * by), 40) == db), None)
        if pb is None:
            continue
        n += 1
        kind, a2, b2 = interaction(a, pb)
        got = run([a, pb], [], 110)
        if kind == 'shatter':
            assert got is None, (a, pb, got)
        else:
            assert got == sorted([a2, b2]), (kind, a, pb, got)


def test_switch_gate():
    """Messenger Q (s=6) reads control X then writes data R; all four input cases."""
    X = Ball('SE', 2, -40, -40)       # lead at (0, 0) at t=40
    Q = Ball('SW', 6, 49, -40)        # lead at (9, 0) at t=40: same row as X
    R = Ball('SE', 2, -70, -42)       # meets Q's row after Q is shifted by X
    _, X2, Q2 = interaction(X, Q)
    assert interaction(R, Q)[0] == 'pass'  # Q unshifted passes R
    _, R2, Q3 = interaction(R, Q2)
    for x in (0, 1):
        for r in (0, 1):
            balls = [Q] + [X] * x + [R] * r
            want = {(0, 0): [Q], (0, 1): [R, Q], (1, 0): [X2, Q2], (1, 1): [X2, R2, Q3]}[x, r]
            assert run(balls, [], 160) == sorted(want), (x, r)
    assert R2.q - R.q == -6 and X2.q - X.q == -6  # outputs hop 6: far enough for a splitter
