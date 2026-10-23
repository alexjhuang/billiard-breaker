"""web.py: no game logic in the server or the page; its frames match the
oracle, and its pictures are the machine's real balls at the stated steps."""
import base64
import gzip
import http.client
import json
import pathlib
import subprocess
import sys
from array import array

from test_play import FORBIDDEN

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_web_imports_no_game_logic():
    code = 'import sys, web; print(" ".join(sorted(sys.modules)))'
    mods = set(subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.split())
    assert not (mods & FORBIDDEN), mods & FORBIDDEN
    for name in ('web.py', 'web/index.html'):
        assert 'oracle' not in (ROOT / name).read_text(), name
    src = (ROOT / 'web.py').read_text()
    for word in ('step(', 'win(', 'lose('):
        assert word not in src, word


def events(resp):
    """Yield (kind, data) from a server-sent event stream."""
    kind = data = None
    while True:
        line = resp.readline().decode().rstrip('\n')
        if line.startswith('event: '):
            kind = line[7:]
        elif line.startswith('data: '):
            data = json.loads(line[6:])
        elif line == '' and kind:
            yield kind, data
            kind = data = None


def steps(counts, flat):
    """Per step, the sorted (x + y, x - y) cells from a flat [u, v, u, v, ...] list."""
    out, o = [], 0
    for c in counts:
        out.append(sorted(zip(flat[2 * o:2 * (o + c):2], flat[2 * o + 1:2 * (o + c):2])))
        o += c
    return out


def pic_steps(pic, lo):
    a = array('H')
    a.frombytes(base64.b64decode(pic['b']))
    return steps(pic['counts'], [x if k % 2 == 0 else x + lo for k, x in enumerate(a)])


def test_web_serves_the_machine():
    sys.path.insert(0, str(ROOT))
    import fastsim
    import hardware
    import oracle
    import web
    from state import State
    srv = web.serve(port=0, fps=100)
    port = srv.server_address[1]

    def req(method, path, body=None):
        c = http.client.HTTPConnection('127.0.0.1', port, timeout=60)
        c.request(method, path, body=body)
        r = c.getresponse()
        return r.status, r.read()

    try:
        status, page = req('GET', '/')
        assert status == 200 and b'id="gc"' in page and b'id="mc"' in page
        meta = json.loads(req('GET', '/machine')[1])
        assert len(meta['messengers']) == 560 and len(meta['field']['bricks']) == 32
        walls = array('H')
        walls.frombytes(gzip.decompress(req('GET', '/walls')[1]))
        assert len(walls) == 2 * meta['squares'] and sum(walls[0::2]) < meta['U2']
        for bad in (b'{"sweep": "x"}', b'[1]', b'not json'):
            assert req('POST', '/control', bad)[0] == 400, bad
        # watch 14 frames, holding right then left
        c = http.client.HTTPConnection('127.0.0.1', port, timeout=60)
        c.request('GET', '/events')
        frames, pics = [], {}
        for kind, data in events(c.getresponse()):
            if kind == 'frame':
                frames.append(data)
                if len(frames) == 3:
                    req('POST', '/control', b'{"right": 1}')
                if len(frames) == 8:
                    req('POST', '/control', b'{"right": 0, "left": 1}')
            elif kind == 'pics':
                pics[data['n']] = data
                if len(frames) >= 14:
                    break
        c.close()
    finally:
        srv.shutdown()
    assert [f['n'] for f in frames] == list(range(len(frames))) and frames[0]['inp']['reset']
    assert any(f['inp']['right'] for f in frames) and any(f['inp']['left'] for f in frames)
    st = prev = None
    for f in frames:  # frame 0 shows the empty world; then the oracle, flags one frame behind
        shown = State(**f['state'])
        if st is not None:
            assert shown == st, (f['n'], shown, st)
        if prev is not None:
            assert (f['win'], f['lose']) == (oracle.win(prev), oracle.lose(prev)), f['n']
        i = f['inp']
        prev, st = st, oracle.step(shown if st is None else st, i['left'], i['right'], i['reset'])
    # replay the same inputs in a fresh (serial) runner, recording the same steps
    r = fastsim.Runner(hardware.load())
    clips = 0
    for f in frames:
        p, start = pics[f['n']], r.done_until
        got = r.feed(f['inp'], [(start + q['ph'], len(q['counts'])) for q in p['pics']])
        for q, (T, counts, uv) in zip(sorted(p['pics'], key=lambda q: q['ph']), got):
            assert pic_steps(q, meta['lo']) == steps(counts, uv), (f['n'], q['ph'])
            clips += len(counts) > 1
    assert clips, 'no microscope clip in 14 frames'
