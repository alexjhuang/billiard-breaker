import random
from cyl import Cyl
from fastsim import FastCyl, simulate
from physics import Ball, Bar


def test_matches_cyl_random():
    rng = random.Random(4)
    for trial in range(6):
        U, lo, hi = 60, -40, 40
        cells = {(x, y) for x in range(-30, 90) for y in range(-30, 90) if rng.random() < 0.15 and lo <= x - y <= hi}
        walls = set(Bar('H', 1, 20, 0).cells()) if trial % 2 else set()
        a, b = Cyl(U, lo, hi), FastCyl(U, lo, hi)
        for w in (a, b):
            w.add(walls, wall=True)
            w.add(cells - walls)
        for t in range(80):
            a.step()
            b.step()
            assert a.moving == b.moving, (trial, t)


def test_machine_matches_python_runner():
    from tests_support import small_machine
    m, init, ins = small_machine()
    assert simulate(m, init, ins, len(ins)) == m.simulate(init, ins, len(ins))
