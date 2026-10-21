"""Reference brick breaker: pure frame step over the bit-level state. See SPEC.md."""
from state import State, PADDLE_Y, PADDLE_W, W, brick_at

PX_MAX = W - PADDLE_W
BALL0 = (16, 29, 0, 1)
INIT = State(*BALL0, 13, (1 << 32) - 1, 3)


def win(s):
    return s.bricks == 0


def lose(s):
    return s.lives == 0


def step(s, left, right, reset):
    if reset:
        return INIT
    if lose(s) or win(s):
        return s
    px = s.px
    if left and not right and px > 0:
        px -= 1
    elif right and not left and px < PX_MAX:
        px += 1
    dx, dy = s.dx, s.dy
    if (dx == 0 and s.bx == W - 1) or (dx == 1 and s.bx == 0):
        dx ^= 1
    bx = s.bx + (-1 if dx else 1)
    if dy == 1 and s.by == 0:
        dy = 0
    elif dy == 0 and s.by == PADDLE_Y - 1 and 0 <= bx - s.px < PADDLE_W:
        dy = 1
    if dy == 0 and s.by == W - 1:
        return State(*BALL0, px, s.bricks, s.lives - 1)
    by = s.by + (-1 if dy else 1)
    bricks = s.bricks
    i = brick_at(bx, by)
    if i is not None and bricks >> i & 1:
        bricks &= ~(1 << i)
        dy ^= 1
    return State(bx, by, dx, dy, px, bricks, s.lives)
