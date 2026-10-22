import random
from sim import Grid
from cyl import Cyl
from physics import Ball, Bar, reflect, interaction


def test_matches_dense_sim_away_from_edges():
    rng = random.Random(3)
    for trial in range(5):
        cells = {(x, y) for x in range(60, 90) for y in range(60, 90) if rng.random() < 0.25}
        g = Grid(160, 160)
        for c in cells:
            g.set(*c)
        w = Cyl(400, -150, 150)
        w.add(cells)
        for t in range(20):
            g.step()
            w.step()
            assert w.cells() == g.cells(), (trial, t)


def test_walls_reflect_like_physics():
    for kind, d, start in [('H', 'SE', (0, -20)), ('V', 'NE', (-20, 4)), ('H', 'NW', (30, 25))]:
        bar = Bar(kind, -41, 40, 0) if kind == 'H' else Bar('V', -41, 40, 0)
        b = next(Ball(d, 2, start[0] + ox, start[1] + oy) for ox in (0, 1) for oy in (0, 1)
                 if Ball(d, 2, start[0] + ox, start[1] + oy).ok())
        w = Cyl(600, -200, 200)
        w.add(bar.cells(), wall=True)
        w.add(b.cells(0))
        w.step(50)
        assert not w.broken and w.moving == {w.canon(*c) for c in reflect(b, bar).cells(50)}, (kind, d)


def test_wraps_and_absorbs():
    U = 200
    w = Cyl(U, -40, 40)
    se, nw = Ball('SE', 2, 10, 10), Ball('NW', 6, 31, 25)
    w.add(se.cells(0) | nw.cells(0))
    w.step(U)
    assert w.moving == {w.canon(*c) for c in se.cells(0) | nw.cells(0)}
    sw = Ball('SW', 2, 11, 10)  # crosses the band and leaves it
    w.add(sw.cells(U))
    w.step(60)
    assert len(w.moving) == 4  # only the two circling balls are left


def test_switch_gate_across_the_seam():
    U = 120
    X, Q, R = Ball('SE', 2, -40, -40), Ball('SW', 6, 49, -40), Ball('SE', 2, -70, -42)
    _, X2, Q2 = interaction(X, Q)
    _, R2, Q3 = interaction(R, Q2)
    shift = 58  # even, and puts the collisions right on the wrap seam

    def placed(balls, t):
        return {w.canon(cx + shift, cy + shift) for b in balls for cx, cy in b.cells(t)}

    for x in (0, 1):
        for r in (0, 1):
            w = Cyl(U, -110, 110)
            w.add(placed([Q] + [X] * x + [R] * r, 0))
            w.step(70)
            want = {(0, 0): [Q], (0, 1): [R, Q], (1, 0): [X2, Q2], (1, 1): [X2, R2, Q3]}[x, r]
            assert w.moving == placed(want, 70), (x, r)
