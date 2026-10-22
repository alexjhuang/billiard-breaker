"""Ball-level physics of dimers in the BBMCA, as exact formulas.

Every rule here was measured with sim.py and is re-checked by tests/test_physics.py.
A ball is a dimer: a lead particle and a trailer s cells behind it on the same
diagonal. Ball(d, s, p, q) has its lead at (p + dx*t, q + dy*t) at step t.
"""
from typing import NamedTuple
from sim import VEC, direction

FLIP_Y = {'SE': 'NE', 'SW': 'NW', 'NE': 'SE', 'NW': 'SW'}  # what an H-bar does
FLIP_X = {'SE': 'SW', 'NE': 'NW', 'SW': 'SE', 'NW': 'NE'}  # what a V-bar does


class Ball(NamedTuple):
    d: str
    s: int
    p: int
    q: int

    def lead(self, t):
        dx, dy = VEC[self.d]
        return self.p + dx * t, self.q + dy * t

    def cells(self, t):
        dx, dy = VEC[self.d]
        x, y = self.lead(t)
        return {(x, y), (x - self.s * dx, y - self.s * dy)}

    def ok(self):
        return direction(self.p, self.q, 0) == self.d

    def phase(self, comp):
        return self.q if comp else self.p


class Bar(NamedTuple):
    """Static 2-thick wall. H: x in [a, b], rows k, k+1. V: y in [a, b], cols k, k+1."""
    kind: str
    a: int
    b: int
    k: int

    def ok(self):
        return (self.a + self.k) % 2 == 1 and (self.b - self.a) % 2 == 1

    def cells(self):
        span = range(self.a, self.b + 1)
        if self.kind == 'H':
            return {(x, y) for x in span for y in (self.k, self.k + 1)}
        return {(x, y) for y in span for x in (self.k, self.k + 1)}


def reflect(ball, bar):
    """Ball after bouncing off the long face of bar (assumes a clean hit)."""
    dx, dy = VEC[ball.d]
    s, p, q, k = ball.s, ball.p, ball.q, bar.k
    if bar.kind == 'H':
        return Ball(FLIP_Y[ball.d], s, p - s * dx, 2 * k - 1 - q if dy == 1 else 2 * k + 3 - q)
    return Ball(FLIP_X[ball.d], s, 2 * k - 1 - p if dx == 1 else 2 * k + 3 - p, q - s * dy)


def hit(ball, bar):
    """How ball's (infinite) path meets bar: None if it never does, else
    (kind, t) with kind in 'reflect' / 'shatter' / 'pass' and t the step at
    which the lead reaches the bar's near row/column."""
    dx, dy = VEC[ball.d]
    if bar.kind == 'V':  # transpose: x<->y
        ball = Ball({'SE': 'SE', 'NW': 'NW', 'NE': 'SW', 'SW': 'NE'}[ball.d], ball.s, ball.q, ball.p)
        dx, dy = dy, dx
    near = bar.k if dy == 1 else bar.k + 1
    t = (near - ball.q) * dy
    if t < 0:
        return None
    x = ball.p + dx * t
    entry, far = (bar.a, bar.b) if dx == 1 else (bar.b, bar.a)
    past, left = (x - entry) * dx, (far - x) * dx
    if past <= -2 or left <= -1:
        return 'pass', t
    if past <= ball.s:
        return 'shatter', t
    return 'reflect', t


def interaction(a, b):
    """Two perpendicular balls whose paths meet: ('clean', a', b'),
    ('shatter', None, None) or ('pass', a, b). Callers check that they really meet."""
    (ax, ay), (bx, by) = VEC[a.d], VEC[b.d]
    if ay == by and ax == -bx:
        comp, sig = 1, ay
    elif ax == bx and ay == -by:
        comp, sig = 0, ax
    else:
        raise ValueError('not perpendicular')
    g = sig * (b.phase(comp) - a.phase(comp))
    if g == 0:
        def shift(ball, ds):
            return ball._replace(**{'pq'[comp]: ball.phase(comp) - sig * ds})
        return 'clean', shift(a, b.s), shift(b, a.s)
    if g in (-a.s, b.s - a.s, b.s):
        return 'shatter', None, None
    return 'pass', a, b


def meet_time(a, b):
    """Step at which perpendicular a and b are level along their converging axis."""
    (ax, ay), (bx, by) = VEC[a.d], VEC[b.d]
    comp = 0 if ay == by else 1  # the converging component
    va, vb = (ax, bx) if comp == 0 else (ay, by)
    return (b.phase(comp) - a.phase(comp)) / (va - vb)
