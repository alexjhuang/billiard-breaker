"""web/engine.js (the in-browser machine of the static site) == the C simulator:
same states, flags and recorded balls. Needs node."""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent


def run_engine(frames):
    sys.path.insert(0, str(ROOT))
    import web
    node = shutil.which('node')
    assert node, 'node is needed to test web/engine.js'
    with tempfile.TemporaryDirectory() as d:
        web.export(d)
        out = subprocess.run([node, str(ROOT / 'tests' / 'engine_frames.js'), d, str(frames)],
                             capture_output=True, text=True, check=True).stdout
    return [json.loads(line) for line in out.splitlines()]


def test_engine_matches_c_simulator():
    import fastsim
    import hardware
    from play import decode
    hw = hardware.load()
    r = fastsim.Runner(hw)
    frames = run_engine(12)
    assert len(frames) == 12 and any(f['pics'] for f in frames)
    for f in frames:
        n, start = f['n'], r.done_until
        got = r.feed(f['inp'], [(start + q['ph'], len(q['counts'])) for q in f['pics']])
        bits = r.outputs[n]
        assert decode(bits)._asdict() == f['state'], n
        assert (bits['win'], bits['lose']) == (f['win'], f['lose']), n
        for q, (T, counts, uv) in zip(f['pics'], got or []):
            a = q['cells']
            assert counts == q['counts'], (n, q['ph'])
            assert sorted(zip(a[0::2], [v + hw['lo'] for v in a[1::2]])) == sorted(zip(uv[0::2], uv[1::2])), (n, q['ph'])
