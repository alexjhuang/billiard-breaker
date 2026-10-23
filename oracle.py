"""Reference brick breaker: pure frame step over the bit-level state. See SPEC.md."""
from state import State, PADDLE_Y, PADDLE_W, PADDLE_W_SMALL, POINTS, W, brick_at

PX_MAX = W - PADDLE_W
PX_MAX_SMALL = W - PADDLE_W_SMALL
BALL0 = (16, 29, 0, 1)
ALL = (1 << 32) - 1
INIT = State(*BALL0, 13, ALL, 3, 0, 0, 0, 0, 0, 0)
CADENCE = (4, 5, 6, 8)  # speed s: the ball moves on the frames with bitrev3(ph) < CADENCE[s]


def win(s):
    return s.bricks == 0 and s.screen == 1


def lose(s):
    return s.lives == 0


def moves(s):
    """Does the ball move this frame? bitrev3(ph) < CADENCE[speed] spreads the moves evenly."""
    r = (s.ph & 1) << 2 | (s.ph & 2) | s.ph >> 2
    return r < CADENCE[s.speed]


def step(s, left, right, reset):
    if reset:
        return INIT
    if lose(s) or win(s):
        return s
    px = s.px
    if left and not right and px > 0:
        px -= 1
    elif right and not left and px < (PX_MAX_SMALL if s.small else PX_MAX):
        px += 1
    ph = (s.ph + 1) & 7
    if s.bricks == 0:  # the wall is cleared: a second one, a new ball
        return State(*BALL0, px, ALL, s.lives, s.score, 0, ph, 0, 0, 1)
    if not moves(s):
        return s._replace(px=px, ph=ph)
    dx, dy = s.dx, s.dy
    if (dx == 0 and s.bx == W - 1) or (dx == 1 and s.bx == 0):
        dx ^= 1
    bx = s.bx + (-1 if dx else 1)
    small = s.small
    if dy == 1 and s.by == 0:
        dy, small = 0, 1  # the ball got through the wall of bricks: the paddle shrinks
    elif dy == 0 and s.by == PADDLE_Y - 1 and 0 <= bx - s.px < (PADDLE_W_SMALL if s.small else PADDLE_W):
        dy = 1
    if dy == 0 and s.by == W - 1:
        return State(*BALL0, px, s.bricks, s.lives - 1, s.score, 0, ph, 0, 0, s.screen)
    by = s.by + (-1 if dy else 1)
    bricks, score, hits, speed = s.bricks, s.score, s.hits, s.speed
    i = brick_at(bx, by)
    if i is not None and bricks >> i & 1:
        bricks &= ~(1 << i)
        dy ^= 1
        row = i >> 3
        score += POINTS[row]
        hits = min(hits + 1, 15)
        lvl = 3 if row == 0 else 2 if row == 1 or hits >= 12 else 1 if hits >= 4 else 0
        speed = max(speed, lvl)
    return State(bx, by, dx, dy, px, bricks, s.lives, score, speed, ph, hits, small, s.screen)
