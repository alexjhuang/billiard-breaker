"""Play brick breaker in a browser, next to a live view of the billiard machine.

    python3 web.py [--port 8000] [--host 127.0.0.1] [--threads 16] [--fps 30]
    python3 web.py --export DIR      # static site (GitHub Pages): the machine runs in the browser

then open http://localhost:8000 (from elsewhere: ssh -L 8000:localhost:8000 <host>,
or --host 0.0.0.0 to serve the network).

Like play.py, every frame is computed by the billiard-ball computer: a machine
thread runs the compiled hardware (build/hardware.pkl) in the C simulator,
adds the key balls at the input ports and reads the state lanes. It publishes
each frame's decoded state and, PICS times per frame, a picture of every
moving ball (some pictures are clips of CLIP consecutive steps, for the
page's microscope). The page (web/index.html) only draws what it gets and
sends keys; there are no game rules in either. Everyone watching shares one
game, and the machine pauses while nobody is.
"""
import base64
import gzip
import json
import os
import sys
import threading
import time
from array import array
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import fastsim
import hardware
from play import decode
from state import FIELDS, H, PADDLE_W, PADDLE_W_SMALL, PADDLE_Y, POINTS, W, brick_at

PICS = 3          # pictures of the machine per frame
CLIP = 16         # steps per microscope clip
CLIP_EVERY = 0.3  # seconds between clips
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web', 'index.html')


def uv(x, y, U2):
    return (x + y) % U2, x - y


class Machine:
    def __init__(self, hw, threads=16, fps=30):
        self.P, self.lo, self.U2, self.fps_cap = hw['P'], hw['lo'], 2 * hw['U'], fps
        self.runner = fastsim.Runner(hw)
        self.runner.w.threads = threads
        self.keys = dict(left=0, right=0, reset=1)  # reset on the first frame: the circuit sets the game up
        self.sweep = 8.0  # time-lapse: seconds per sweep of the frame (0 = real time)
        self.phase, self.fps, self.last_clip = 0.0, 0.0, 0.0
        self.viewers, self.seq, self.events = 0, 0, []
        self.cond = threading.Condition()

    def control(self, req):
        """req: {'left': 0/1, 'right': 0/1, 'reset': 1, 'sweep': seconds}, any subset."""
        sweep = req.get('sweep')
        if sweep is not None and not (isinstance(sweep, (int, float)) and 0 <= sweep <= 600):
            raise ValueError('sweep: seconds in 0..600')
        with self.cond:
            for k in ('left', 'right'):
                if k in req:
                    self.keys[k] = int(bool(req[k]))
            if req.get('reset'):
                self.keys['reset'] = 1
            if sweep is not None:
                self.sweep = float(sweep)

    def publish(self, kind, obj):
        with self.cond:
            self.seq += 1
            self.events.append((self.seq, kind, json.dumps(obj, separators=(',', ':'))))
            del self.events[:-200]
            self.cond.notify_all()

    def picture_times(self, start):
        """PICS moments of this frame's run, advancing `phase` so successive
        pictures sweep through the frame at the chosen time-lapse rate."""
        fps = self.fps or self.fps_cap
        step = self.P / PICS if not self.sweep else max(1.0, self.P / (PICS * fps * self.sweep))
        clip = time.monotonic() - self.last_clip >= CLIP_EVERY
        if clip:
            self.last_clip = time.monotonic()
        times = {}
        for j in range(PICS):
            ph = min(max(int(self.phase + j * step) % self.P, CLIP), self.P - 1)
            times[start + ph] = max(times.get(start + ph, 1), CLIP if clip and j == 0 else 1)
        self.phase = (self.phase + PICS * step) % self.P
        return sorted(times.items())

    def picture(self, start, T, counts, cells):
        """Ball positions as uint16 ((x + y) / 2, x - y - lo) pairs, all steps of a
        clip. x + y has the parity of x - y, so the page gets it back exactly."""
        cells[0::2] = [u >> 1 for u in cells[0::2]]
        cells[1::2] = [v - self.lo for v in cells[1::2]]
        return dict(ph=T - start, counts=counts, b=base64.b64encode(array('H', cells).tobytes()).decode())

    def loop(self):
        try:
            self.run_frames()
        except BaseException as e:  # tell the page, then die loudly
            self.publish('fault', dict(error=f'{type(e).__name__}: {e}'))
            raise

    def run_frames(self):
        r, t_next, t_prev = self.runner, time.monotonic(), None
        while True:
            with self.cond:
                while not self.viewers:
                    self.cond.wait()
                    t_next, t_prev = time.monotonic(), None
            time.sleep(max(0.0, t_next - time.monotonic()))
            t0 = time.monotonic()
            t_next = max(t_next + 1 / self.fps_cap, t0)
            with self.cond:
                inp = dict(self.keys)
                self.keys['reset'] = 0
            n, start = len(r.inputs), r.done_until
            pics = r.feed(inp, self.picture_times(start))
            bits = r.outputs[n]
            dt = time.monotonic() - t0
            if t_prev is not None:  # from the average time between frame starts
                self.period = 0.8 * self.period + 0.2 * (t0 - t_prev) if self.fps else t0 - t_prev
                self.fps = 1 / self.period
            t_prev = t0
            self.publish('frame', dict(n=n, state=decode(bits)._asdict(), win=bits['win'], lose=bits['lose'],
                                       inp=inp, ms=round(1000 * dt, 1), fps=round(self.fps, 2)))
            self.publish('pics', dict(n=n, P=self.P, sweep=self.sweep, pics=[self.picture(start, *p) for p in pics]))


GATES = {'power': 0, 'messenger': 0, 'input:left': 1, 'input:right': 2, 'input:reset': 3}


def machine_info(hw):
    """What the page needs to draw the machine (shape, wall squares, ports) and,
    for the in-browser engine (web/engine.js), to run it: the per-frame ball
    schedule (t - t_lo, x, y, gate: 0 always, 1/2/3 only while left/right/reset)
    and the cells each output is read from."""
    U2, lo = 2 * hw['U'], hw['lo']
    t_lo = min(min(t for t, _, _ in hw['feeds']), min(t for t, _ in hw['reads'].values()))
    walls = set(map(tuple, hw['walls']))
    # centres of the 2x2 wall squares (every bar is a union of them), sorted by
    # x + y and sent as (gap in x + y since the previous one, x - y - lo)
    sq = sorted(((x + y + 1) % U2, x - y - lo) for x, y in walls
                if (x + y) % 2 and (x + 1, y) in walls and (x, y + 1) in walls and (x + 1, y + 1) in walls)
    squares, prev = array('H'), 0
    for u, v in sq:
        squares.extend((u - prev, v))
        prev = u
    first = {}
    for t, cells, tag in sorted(hw['feeds']):
        first.setdefault(tag, []).append([t - t_lo, *uv(*cells[0], U2)])
    bricks = {}
    for y in range(H):
        for x in range(W):
            i = brick_at(x, y)
            if i is not None:
                x0, y0, x1, y1 = bricks.get(i, (x, y, x, y))
                bricks[i] = (min(x0, x), min(y0, y), max(x1, x), max(y1, y))
    meta = dict(U2=U2, lo=lo, hi=hw['hi'], P=hw['P'], pics=PICS, clip=CLIP,
                walls=len(walls), squares=len(squares) // 2,
                messengers=first.get('messenger', []), power=len(first.get('power', [])),
                inputs={tag[6:]: v[0] for tag, v in first.items() if tag.startswith('input:')},
                t_lo=t_lo, fields=FIELDS,
                sched=sorted([t - t_lo, x, y, GATES[tag]] for t, cells, tag in hw['feeds'] for x, y in cells),
                reads={name: [t - t_lo, cells] for name, (t, cells) in hw['reads'].items()},
                field=dict(W=W, H=H, paddle_y=PADDLE_Y, paddle_w=PADDLE_W, paddle_w_small=PADDLE_W_SMALL, points=list(POINTS),
                           bricks=[[x0, y0, x1 - x0 + 1, y1 - y0 + 1] for i, (x0, y0, x1, y1) in sorted(bricks.items())]))
    return json.dumps(meta).encode(), gzip.compress(squares.tobytes(), 6)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, body, ctype, gz=False):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-cache')
        if gz:
            self.send_header('Content-Encoding', 'gzip')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        s = self.server
        if self.path == '/':
            self.send(200, open(PAGE, 'rb').read(), 'text/html; charset=utf-8')
        elif self.path == '/machine':
            self.send(200, s.meta, 'application/json')
        elif self.path == '/walls':
            self.send(200, s.walls, 'application/octet-stream', gz=True)
        elif self.path == '/events':
            self.stream(s.machine)
        else:
            self.send(404, b'not found', 'text/plain')

    def do_POST(self):
        if self.path != '/control':
            return self.send(404, b'not found', 'text/plain')
        n = int(self.headers.get('Content-Length') or 0)
        if not 0 <= n <= 4096:
            return self.send(413, b'too large', 'text/plain')
        try:
            req = json.loads(self.rfile.read(n) or b'{}')
            if not isinstance(req, dict):
                raise ValueError('expected a JSON object')
            self.server.machine.control(req)
        except ValueError as e:  # includes bad JSON
            return self.send(400, str(e).encode(), 'text/plain')
        self.send(204, b'', 'text/plain')

    def stream(self, m):
        """Server-sent events: every 'frame' and 'pics' event from now on."""
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        with m.cond:
            m.viewers += 1
            frames = [e[0] for e in m.events if e[1] == 'frame']
            last = frames[-1] - 1 if frames else m.seq  # start with the current frame
            m.cond.notify_all()
        try:
            while True:
                with m.cond:
                    m.cond.wait_for(lambda: m.seq > last, timeout=10)
                    new = [e for e in m.events if e[0] > last]
                if new:
                    last = new[-1][0]
                    self.wfile.write(''.join(f'event: {k}\ndata: {d}\n\n' for _, k, d in new).encode())
                else:
                    self.wfile.write(b': idle\n\n')
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with m.cond:
                m.viewers -= 1


def serve(host='127.0.0.1', port=8000, threads=16, fps=30):
    """Start the server and machine threads; returns the server (server_address has the port)."""
    hw = hardware.load()
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    srv.meta, srv.walls = machine_info(hw)
    srv.machine = Machine(hw, threads, fps)
    threading.Thread(target=srv.machine.loop, daemon=True).start()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def export(out):
    """A static copy of the page that runs the machine in the visitor's browser
    (web/gpu.js on the GPU through WebGPU, else web/engine.js on the CPU, in a
    Web Worker) instead of on this server."""
    import hashlib
    meta, walls = machine_info(hardware.load())
    engine = open(os.path.join(os.path.dirname(PAGE), 'engine.js'), 'rb').read()
    gpu = open(os.path.join(os.path.dirname(PAGE), 'gpu.js'), 'rb').read()
    os.makedirs(out, exist_ok=True)
    page = open(PAGE).read().replace('<script>\n', '<script>window.STATIC_SITE = true</script>\n<script>\n', 1)
    v = hashlib.sha1(meta + walls + engine + gpu).hexdigest()[:8]  # the page always loads data from its own build
    for name in ('machine.json', 'walls.bin.gz', 'engine.js'):
        assert page.count(f"'{name}'") == 1, name
        page = page.replace(f"'{name}'", f"'{name}?v={v}'")
    assert engine.count(b"'gpu.js'") == 1
    engine = engine.replace(b"'gpu.js'", f"'gpu.js?v={v}'".encode())
    files = {'index.html': page.encode(), 'machine.json': meta, 'walls.bin.gz': walls, '.nojekyll': b'', 'engine.js': engine, 'gpu.js': gpu}
    for name, data in files.items():
        with open(os.path.join(out, name), 'wb') as f:
            f.write(data)
    return files


def main():
    opts = dict(host='127.0.0.1', port=8000, threads=min(16, os.cpu_count() or 1), fps=30)
    args = sys.argv[1:]
    if '--export' in args:
        out = args[args.index('--export') + 1]
        print(f'static site in {out}:', ', '.join(f'{n} {len(d) // 1024} KB' for n, d in export(out).items()))
        return
    for k in list(opts):
        if f'--{k}' in args:
            v = args[args.index(f'--{k}') + 1]
            opts[k] = v if k == 'host' else int(v)
    srv = serve(**opts)
    print(f'billiard breaker on http://{opts["host"]}:{srv.server_address[1]}  (Ctrl-C to stop)', flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
