"""Geometry primitives for building circuits out of bars and injected balls.

Works on the diagonal cylinder of cyl.py. A *lane* is a SE-moving dimer
(Ball('SE', 2, p, q)); its line is c = p - q and its row (timing) is q.
In uv = ((x+y)/2, (x-y)/2) a lane moves right along v = c/2, NE balls move up,
SW balls move down, and bars sit at single points. Every primitive returns the
ball state that comes out of it, computed with physics.reflect, so a layout is
built by threading ball states through the primitives.
"""
from physics import Ball, Bar, reflect, hit


class Layout:
    def __init__(self, U, lo, hi):
        self.U, self.lo, self.hi = U, lo, hi
        self.bars = []
        self.feeds = []  # (Ball at t=0, tag): balls the frontend adds each frame

    def bar(self, kind, a, b, k):
        bar = Bar(kind, a, b, k)
        assert bar.ok(), bar
        self.bars.append(bar)
        return bar

    def turn(self, ball, bar):
        assert hit(ball, bar)[0] == 'reflect', (ball, bar, hit(ball, bar))
        return reflect(ball, bar)

    # --- lane moves -------------------------------------------------------
    def up(self, lane, u, h):
        """Turn a lane up (NE) at u, rise, and turn it back to SE h rows higher.
        Row changes by -2h+4 and line by 2h-8."""
        assert lane.d == 'SE' and h >= 5
        k1 = u - (lane.p - lane.q) // 2
        x1 = lane.p - lane.q + k1
        ne = self.turn(lane, self.bar('H', x1 - 3, x1, k1))
        k2 = k1 - h
        x2 = ne.p + ne.q - k2 - 1
        return self.turn(ne, self.bar('H', x2 - 3, x2, k2))

    def down(self, lane, u, w):
        """Turn a lane down (SW) at u and back to SE w columns further left.
        Row changes by -4 and line by -2w+8."""
        assert lane.d == 'SE' and w >= 5
        X3 = u + (lane.p - lane.q) // 2
        y3 = X3 - (lane.p - lane.q)
        sw = self.turn(lane, self.bar('V', y3 - 3, y3, X3))
        X4 = X3 - w
        y4 = sw.q + sw.p - X4 - 1
        return self.turn(sw, self.bar('V', y4 - 3, y4, X4))

    def retime(self, lane, u, delay, dline=0, run=4):
        """Delay a lane by `delay` rows (delay = 2h for an up-jog of h) and move
        it dline (x-y units): up-jog at u, down-jog `run` columns later."""
        assert delay % 2 == 0 and dline % 2 == 0
        h = delay // 2
        w = h - dline // 2
        return self.down(self.up(lane, u, h), u + run, w)

    # --- balls the frontend adds -----------------------------------------
    def feed(self, ball, tag):
        """Schedule `ball` to be added at the top edge, as late as possible
        while still wholly inside the band. Returns the injection step."""
        assert ball.d == 'SW'
        t = -((self.hi - ball.p + ball.q - 2 * ball.s) // 2)  # ceil((x-y at t=0 - hi + 2s) / 2)
        self.feeds.append((t, ball, tag))
        return t

    def power(self, u, line, row, tag='power'):
        """A fresh lane (s=2) on `line` with `row`, starting at column u: a SW
        ball falls from the top edge and a V-bar turns it SE."""
        k = 2 * u + 1                      # SW column (odd x+y) just left of u
        qs = row + 2
        ps = k - qs
        X = (line + k - 5) // 2
        y = k - X - 1
        sw = Ball('SW', 2, ps, qs)
        assert sw.ok(), sw
        lane = self.turn(sw, self.bar('V', y - 3, y, X))
        assert lane == Ball('SE', 2, lane.p, row) and lane.p - lane.q == line, lane
        self.feed(sw, tag)
        return lane

    def messenger(self, u2, row):
        """Power messenger (s=6) falling down column x+y = u2 (odd) with `row`."""
        m = Ball('SW', 6, u2 - row, row)
        assert m.ok(), m
        self.feed(m, 'messenger')
        return m

    def drop(self, lane, u):
        """Send a lane down and out of the band (garbage) at column u."""
        X = u + (lane.p - lane.q) // 2
        y = X - (lane.p - lane.q)
        return self.turn(lane, self.bar('V', y - 3, y, X))

    def split(self, low, u, keep):
        """`low` is R's unshifted track (R & ~X); its shifted twin (R & X) is 6
        higher. Drop the one we don't keep ('A' = shifted, 'B' = unshifted) at u."""
        hi_line = low.p - low.q + 6
        X = u + (low.p - low.q) // 2 + (3 if keep == 'B' else 0)
        line = hi_line if keep == 'B' else low.p - low.q
        y = X - line
        self.bar('V', y - 3, y, X)

    # --- running ----------------------------------------------------------
    def world(self, lanes, t0, period=None):
        """A cyl.Cyl loaded with the walls and `lanes` (Balls) placed at step t0.
        Use step_world() to advance it with the feeds added on schedule."""
        from cyl import Cyl
        w = Cyl(self.U, self.lo, self.hi)
        w.add(set().union(*(b.cells() for b in self.bars)) if self.bars else set(), wall=True)
        for b in lanes:
            w.add(b.cells(t0))
        w.t = t0
        self._period = period
        self._feeds = {}
        for t, b, tag in self.feeds:
            self._feeds.setdefault(t if period is None else t % period, []).append((b, tag))
        return w

    def step_world(self, w, n, inputs=None):
        """Advance n steps, adding feed balls when due. Feeds tagged
        'input:<name>' are added only if inputs[name] is true."""
        for _ in range(n):
            key = w.t if self._period is None else w.t % self._period
            for b, tag in self._feeds.get(key, ()):
                if tag.startswith('input:') and not (inputs or {}).get(tag[6:]):
                    continue
                # the same cells every period: the layout repeats with period P
                w.add(b.cells(key if self._period is None else key))
            w.step()
