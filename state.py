"""Game state layout (no game rules here). See SPEC.md."""
from typing import NamedTuple

FIELDS = [('bx', 5), ('by', 5), ('dx', 1), ('dy', 1), ('px', 5), ('bricks', 32), ('lives', 2),
          ('score', 9), ('speed', 2), ('ph', 3), ('hits', 4), ('small', 1), ('screen', 1)]
NBITS = sum(w for _, w in FIELDS)
W = H = 32
PADDLE_Y, PADDLE_W, PADDLE_W_SMALL = 30, 6, 3
POINTS = (7, 5, 3, 1)  # per brick row, top to bottom (red, orange, green, yellow)


class State(NamedTuple):
    bx: int
    by: int
    dx: int  # 0 right, 1 left
    dy: int  # 0 down, 1 up
    px: int
    bricks: int
    lives: int
    score: int
    speed: int   # 0..3: the ball moves on 1/2, 5/8, 3/4, all frames
    ph: int      # frame counter mod 8 for the cadence
    hits: int    # bricks hit with this ball (saturates at 15)
    small: int   # paddle is PADDLE_W_SMALL wide
    screen: int  # 0 first wall of bricks, 1 second


def pack(s):
    v, sh = 0, 0
    for (name, w), x in zip(FIELDS, s):
        v |= x << sh
        sh += w
    return v


def unpack(v):
    out = []
    for _, w in FIELDS:
        out.append(v & ((1 << w) - 1))
        v >>= w
    return State(*out)


def brick_at(x, y):
    """Brick index covering cell (x, y), or None outside the brick rows."""
    return ((y >> 1) & 3) * 8 + (x >> 2) if 8 <= y < 16 else None


def paddle_w(s):
    return PADDLE_W_SMALL if s.small else PADDLE_W
