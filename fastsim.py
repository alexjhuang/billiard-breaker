"""ctypes wrapper for csim/bbm.c (built with gcc on first use) and a fast
version of Machine.simulate. Same results as cyl.py, checked by tests."""
import ctypes
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, 'csim', 'bbm.c')
_lib = None
I64 = ctypes.c_int64
P64 = ctypes.POINTER(ctypes.c_int64)


def lib():
    global _lib
    if _lib is None:
        import hashlib
        tag = hashlib.sha1(open(SRC, 'rb').read()).hexdigest()[:12]
        lib_path = os.path.join(HERE, 'build', f'libbbm-{tag}.so')  # never overwrite a loaded library
        if not os.path.exists(lib_path):
            os.makedirs(os.path.dirname(lib_path), exist_ok=True)
            tmp = lib_path + f'.{os.getpid()}.tmp'
            subprocess.run(['gcc', '-O3', '-march=native', '-shared', '-fPIC', '-o', tmp, SRC], check=True)
            os.replace(tmp, lib_path)
        L = ctypes.CDLL(lib_path)
        L.world_new.restype = ctypes.c_void_p
        L.world_new.argtypes = [I64, I64, I64]
        L.world_free.argtypes = [ctypes.c_void_p]
        L.world_add.argtypes = [ctypes.c_void_p, I64, P64, ctypes.c_int]
        L.world_run.argtypes = [ctypes.c_void_p, I64, I64, P64, I64, P64, ctypes.POINTER(ctypes.c_uint8)]
        L.world_count.restype = I64
        L.world_count.argtypes = [ctypes.c_void_p]
        L.world_broken.argtypes = [ctypes.c_void_p]
        L.world_time.restype = I64
        L.world_time.argtypes = [ctypes.c_void_p]
        L.world_set_time.argtypes = [ctypes.c_void_p, I64]
        L.world_cells.restype = I64
        L.world_cells.argtypes = [ctypes.c_void_p, P64, I64]
        L.world_prof.argtypes = [ctypes.POINTER(ctypes.c_double)]
        L.world_run_tiles.argtypes = [ctypes.c_void_p, I64, I64, P64, I64, P64, ctypes.POINTER(ctypes.c_uint8),
                                      ctypes.c_int, I64]
        _lib = L
    return _lib


def _arr(values):
    return (ctypes.c_int64 * len(values))(*values)


class FastCyl:
    """Same interface as cyl.Cyl for what the tests and frontends use."""

    def __init__(self, U, lo, hi):
        self.U, self.lo, self.hi = U, lo, hi
        self.w = lib().world_new(U, lo, hi)

    def __del__(self):
        if getattr(self, 'w', None):
            lib().world_free(self.w)

    def add(self, cells, wall=False):
        flat = [v for c in cells for v in c]
        if flat:
            lib().world_add(self.w, len(flat) // 2, _arr(flat), int(wall))

    @property
    def t(self):
        return lib().world_time(self.w)

    @t.setter
    def t(self, v):
        lib().world_set_time(self.w, v)

    @property
    def broken(self):
        return bool(lib().world_broken(self.w))

    threads, epoch = 1, 32  # >1 threads: overlapping tiles, `epoch` steps per barrier

    def run(self, t_end, feeds=(), reads=()):
        """feeds/reads: sorted lists of (t, x, y). Returns list of read bits."""
        f = [v for e in feeds for v in e]
        r = [v for e in reads for v in e]
        out = (ctypes.c_uint8 * max(1, len(reads)))()
        args = (self.w, t_end, len(feeds), _arr(f) if f else None, len(reads), _arr(r) if r else None, out)
        if self.threads > 1:
            lib().world_run_tiles(*args, self.threads, self.epoch)
        else:
            lib().world_run(*args)
        return list(out[:len(reads)])

    def step(self, n=1):
        self.run(self.t + n)

    @property
    def moving(self):
        n = lib().world_count(self.w)
        buf = (ctypes.c_int64 * (2 * n + 2))()
        lib().world_cells(self.w, buf, n)
        return {(buf[2 * k], buf[2 * k + 1]) for k in range(n)}

    def canon(self, x, y):
        k = (x + y) // (2 * self.U)
        return x - k * self.U, y - k * self.U


class Runner:
    """Runs compiled hardware frame by frame in the C simulator.
    hw: a hardware.py dict (or a compile.Machine). feed() one frame of inputs
    at a time; outputs[n] holds what was read at the start of frame n."""

    def __init__(self, hw, init=None):
        if not isinstance(hw, dict):
            import hardware
            hw = hardware.export(hw, path=os.devnull)
        self.P = hw['P']
        self.w = FastCyl(hw['U'], hw['lo'], hw['hi'])
        self.w.add(hw['walls'], wall=True)
        self.feeds = sorted(hw['feeds'])
        self.reads = hw['reads']
        self.t_lo = min(min(t for t, _, _ in self.feeds), min(t for t, _ in self.reads.values()))
        self.t_hi = max(max(t for t, _, _ in self.feeds), max(t for t, _ in self.reads.values()))
        self.inputs, self.outputs, self.pending = [], [], []
        for name, bit in (init or {}).items():
            if bit:
                t, cells = self.reads[name]
                self.pending += [(t, x, y) for x, y in cells]
        self.w.t = self.t_lo
        self.done_until = self.t_lo

    def feed(self, inp):
        """Add one more frame of inputs and run everything that is now determined."""
        n, P = len(self.inputs), self.P
        self.inputs.append(inp)
        for t, cells, tag in self.feeds:
            if tag.startswith('input:') and not inp.get(tag[6:]):
                continue
            self.pending += [(t + n * P, x, y) for x, y in cells]
        # frame n's readouts need frames <= n of feeds; feeds of frame n+1 start at t_lo+(n+1)P
        self.advance(self.t_lo + (n + 1) * P)

    def advance(self, until):
        P = self.P
        feeds = sorted(e for e in self.pending if e[0] < until)
        self.pending = [e for e in self.pending if e[0] >= until]
        reads, keys = [], []
        n0 = len(self.outputs)
        for n in range(n0, n0 + 3):
            for name, (t0, cells) in self.reads.items():
                t = t0 + n * P
                if self.done_until <= t < until:
                    for x, y in cells:
                        reads.append((t, x, y))
                        keys.append((n, name))
        order = sorted(range(len(reads)), key=lambda k: reads[k])
        bits = self.w.run(until, feeds, [reads[k] for k in order])
        got = {}
        for k, bit in zip(order, bits):
            got.setdefault(keys[k], []).append(bit)
        for (n, name), bs in sorted(got.items()):
            while len(self.outputs) <= n:
                self.outputs.append({})
            assert sum(bs) in (0, 2), ('half a ball', name, n)
            self.outputs[n][name] = int(sum(bs) == 2)
        self.done_until = until
        assert not self.w.broken

    def frame_ready(self, n):
        return len(self.outputs) > n and len(self.outputs[n]) == len(self.reads)


def simulate(m, init, inputs, nframes):
    """Fast Machine.simulate: same arguments and result."""
    r = Runner(m, init)
    for n in range(nframes):
        r.feed(inputs[n])
    r.advance(r.t_hi + nframes * r.P + 1)
    return r.outputs[:nframes + 1]
