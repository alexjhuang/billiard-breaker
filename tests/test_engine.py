"""web/engine.js and web/wasm.js (the in-browser machines of the static site) ==
the C simulator: same states, flags and recorded balls. Needs node."""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent


def run_engine(frames, engine='js'):
    sys.path.insert(0, str(ROOT))
    import web
    node = shutil.which('node')
    assert node, 'node is needed to test web/engine.js'
    with tempfile.TemporaryDirectory() as d:
        web.export(d)
        out = subprocess.run([node, str(ROOT / 'tests' / 'engine_frames.js'), d, str(frames), engine],
                             capture_output=True, text=True, check=True).stdout
    return [json.loads(line) for line in out.splitlines()]


def test_engine_matches_c_simulator(engine='js'):
    import fastsim
    import hardware
    from play import decode
    hw = hardware.load()
    r = fastsim.Runner(hw)
    frames = run_engine(12, engine)
    assert len(frames) == 12 and any(f['pics'] for f in frames)
    for f in frames:
        n, start = f['n'], r.done_until
        got = r.feed(f['inp'], [(start + q['ph'], K) for q, K in zip(f['pics'], (16, 1, 1) if n % 3 == 0 else (1, 1, 1) if n % 3 == 1 else (16, 1, 3))])
        bits = r.outputs[n]
        assert decode(bits)._asdict() == f['state'], n
        assert (bits['win'], bits['lose']) == (f['win'], f['lose']), n
        assert len(got) == 3, n
        for q, K, (T, counts, uv) in zip(f['pics'], (16, 1, 1) if n % 3 == 0 else (1, 1, 1) if n % 3 == 1 else (16, 1, 3), got):
            a = q['cells']  # ((x + y) / 2, x - y - lo) pairs
            assert counts == q['counts'] and len(counts) == K, (n, q['ph'], len(counts), K)
            vs = [v + hw['lo'] for v in a[1::2]]
            assert sorted(zip([2 * h + (v & 1) for h, v in zip(a[0::2], vs)], vs)) == sorted(zip(uv[0::2], uv[1::2])), (n, q['ph'])


def test_wasm_engine_matches_c_simulator():
    test_engine_matches_c_simulator('wasm')
