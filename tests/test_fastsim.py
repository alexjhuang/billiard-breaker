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
    """world_run_tiles (overlapping tiles on threads) must equal the serial run exactly."""
    import fastsim
    from tests_support import small_machine
    m, init, ins = small_machine(5)
    import hardware, os
    hw = hardware.export(m, path=os.devnull)
    results = []
    for threads, epoch in ((1, 32), (3, 16), (4, 40)):
        fastsim.FastCyl.threads, fastsim.FastCyl.epoch = threads, epoch
        r = fastsim.Runner(hw, init)
        for inp in ins:
            r.feed(inp)
        results.append((r.outputs, sorted(r.w.moving)))
    fastsim.FastCyl.threads, fastsim.FastCyl.epoch = 1, 32
    assert results[0] == results[1] == results[2]


def test_tiled_threads_match_serial_on_the_game():
    """The real game hardware for a few frames: 8 threads == 1 thread, cell for cell."""
    import fastsim
    import hardware
    hw = hardware.load()
    keys = [dict(left=0, right=0, reset=1)] + [dict(left=k % 3 == 0, right=k % 2, reset=0) for k in range(4)]
    results = []
    for threads in (1, 8):
        fastsim.FastCyl.threads, fastsim.FastCyl.epoch = threads, 64
        r = fastsim.Runner(hw)
        for inp in keys:
            r.feed(inp)
        results.append((r.outputs, sorted(r.w.moving)))
    fastsim.FastCyl.threads, fastsim.FastCyl.epoch = 1, 32
    assert results[0] == results[1]
