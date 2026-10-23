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


def test_traced_runs_are_exact():
    """Recording K steps before chosen times (the web view) changes nothing."""
    import fastsim
    import hardware
    hw = hardware.load()
    keys = [dict(left=0, right=0, reset=1)] + [dict(left=0, right=k % 2, reset=0) for k in range(2)]
    results = []
    for traced in (False, True):
        r = fastsim.Runner(hw)
        recs = []
        for inp in keys:
            s = r.done_until
            got = r.feed(inp, [(s + 5000, 16), (s + 20000, 1), (s + 41000, 3)] if traced else ())
            for T, counts, uv in got or ():
                assert len(counts) in (16, 1, 3) and sum(counts) * 2 == len(uv)
                recs.append((T, counts, sorted(zip(uv[0::2], uv[1::2]))))
        results.append((r.outputs, sorted(r.w.moving), recs))
    assert results[0][:2] == results[1][:2]
    assert len(results[1][2]) == 9
