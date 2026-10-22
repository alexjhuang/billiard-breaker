"""Billiard Ball Model cellular automaton (Margolus neighborhood), stdlib only.

The grid is one Python int, bit y*w + x = cell (x, y). Step t uses 2x2 blocks
whose top-left corner has x = y = t % 2 (mod 2). Per block:
  exactly one ball           -> moves to the opposite corner (rotate 180°)
  two balls on a diagonal    -> move to the other diagonal (rotate 90°)
  anything else              -> unchanged
Cells not inside a complete block at the grid edge are left unchanged.
The block map is an involution, so stepping back = replaying offsets in reverse.
"""

DIRS = {(0, 0): 'SE', (1, 0): 'SW', (0, 1): 'NE', (1, 1): 'NW'}
VEC = {'SE': (1, 1), 'SW': (-1, 1), 'NE': (1, -1), 'NW': (-1, -1)}


def direction(x, y, t):
    """Direction a lone ball at (x, y) moves on step t."""
    o = t & 1
    return DIRS[((x - o) & 1, (y - o) & 1)]


def _mask(w, h, pred):
    bits = ''.join('1' if pred(x, y) else '0' for y in range(h) for x in range(w))
    return int(bits[::-1], 2) if bits else 0


_MASKS = {}


def _masks(w, h):
    if (w, h) not in _MASKS:
        lane, keep = [], []
        for o in (0, 1):
            inside = lambda x, y, o=o: x - o >= 0 and y - o >= 0 and x - o < (w - o) // 2 * 2 and y - o < (h - o) // 2 * 2
            lane.append(_mask(w, h, lambda x, y: inside(x, y) and (x - o) % 2 == 0 and (y - o) % 2 == 0))
            keep.append(_mask(w, h, lambda x, y: not inside(x, y)))
        _MASKS[w, h] = lane, keep
    return _MASKS[w, h]


class Grid:
    def __init__(self, w, h):
        self.w, self.h, self.g, self.t = w, h, 0, 0
        self.lane, self.keep = _masks(w, h)

    def set(self, x, y, v=1):
        bit = 1 << (y * self.w + x)
        self.g = self.g | bit if v else self.g & ~bit

    def get(self, x, y):
        return self.g >> (y * self.w + x) & 1

    def cells(self):
        g, w, out, i = self.g, self.w, set(), 0
        while g:
            low = g & -g
            i = low.bit_length() - 1
            out.add((i % w, i // w))
            g ^= low
        return out

    def _apply(self, o):
        g, m, w = self.g, self.lane[o], self.w
        a, b, c, d = g & m, g >> 1 & m, g >> w & m, g >> (w + 1) & m
        one = a ^ b ^ c ^ d
        one &= ~((a & b) | (c & d))
        diag = a & d & ~(b | c)
        anti = b & c & ~(a | d)
        ch = one | diag | anti
        a2 = (one & d) | anti | (a & ~ch)
        b2 = (one & c) | diag | (b & ~ch)
        c2 = (one & b) | diag | (c & ~ch)
        d2 = (one & a) | anti | (d & ~ch)
        self.g = (g & self.keep[o]) | a2 | b2 << 1 | c2 << w | d2 << (w + 1)

    def step(self, n=1):
        for _ in range(n):
            self._apply(self.t & 1)
            self.t += 1

    def unstep(self, n=1):
        for _ in range(n):
            self.t -= 1
            self._apply(self.t & 1)
