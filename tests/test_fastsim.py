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


def test_tiled_threads_match_serial():
    """world_run_tiles (tiles across the band on threads) must equal the serial run exactly."""
    import fastsim
    from tests_support import small_machine
    m, init, ins = small_machine(5)
    import hardware, os
    hw = hardware.export(m, path=os.devnull)
    results = []
    for threads, epoch in ((1, 64), (3, 16), (4, 40)):
        fastsim.FastCyl.threads, fastsim.FastCyl.epoch = threads, epoch
        r = fastsim.Runner(hw, init)
        for inp in ins:
            r.feed(inp)
        results.append((r.outputs, sorted(r.w.moving)))
    fastsim.FastCyl.threads, fastsim.FastCyl.epoch = 1, 64
    assert results[0] == results[1] == results[2]


def test_tiled_threads_match_serial_on_the_game():
    """The real game hardware for a few frames: 16 threads (as in play.py) == 1 thread, cell for cell."""
    import fastsim
    import hardware
    hw = hardware.load()
    keys = [dict(left=0, right=0, reset=1)] + [dict(left=k % 3 == 0, right=k % 2, reset=0) for k in range(4)]
    results = []
    for threads, epoch in ((1, 64), (16, 64), (16, 256)):
        fastsim.FastCyl.threads, fastsim.FastCyl.epoch = threads, epoch
        r = fastsim.Runner(hw)
        for inp in keys:
            r.feed(inp)
        results.append((r.outputs, sorted(r.w.moving)))
    fastsim.FastCyl.threads, fastsim.FastCyl.epoch = 1, 64
    assert results[0] == results[1] == results[2]


def test_traced_runs_are_exact():
    """Recording K steps before chosen times (the web view) changes nothing, and
    the recorded cells are the same on 1 and 4 threads."""
    import fastsim
    import hardware
    hw = hardware.load()
    keys = [dict(left=0, right=0, reset=1)] + [dict(left=0, right=k % 2, reset=0) for k in range(2)]
    results = []
    for threads, traced in ((1, False), (1, True), (4, True)):
        r = fastsim.Runner(hw)
        r.w.threads = threads
        recs = []
        for inp in keys:
            s = r.done_until
            got = r.feed(inp, [(s + 5000, 16), (s + 20000, 1), (s + 41000, 3)] if traced else ())
            for T, counts, uv in got or ():
                assert len(counts) in (16, 1, 3) and sum(counts) * 2 == len(uv)
                recs.append((T, counts, sorted(zip(uv[0::2], uv[1::2]))))
        results.append((r.outputs, sorted(r.w.moving), recs))
    assert results[0][:2] == results[1][:2] == results[2][:2]
    assert results[1][2] == results[2][2] and len(results[1][2]) == 9
