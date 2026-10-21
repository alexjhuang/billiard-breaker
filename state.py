"""Game state layout (no game rules here). See SPEC.md."""
from typing import NamedTuple

FIELDS = [('bx', 5), ('by', 5), ('dx', 1), ('dy', 1), ('px', 5), ('bricks', 32), ('lives', 2)]
NBITS = sum(w for _, w in FIELDS)
W = H = 32
PADDLE_Y, PADDLE_W = 30, 6


class State(NamedTuple):
    bx: int
    by: int
    dx: int  # 0 right, 1 left
    dy: int  # 0 down, 1 up
    px: int
    bricks: int
    lives: int


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
