"""The brick-breaker frame step as boolean logic (mirrors oracle.step bit for bit).

build() returns {output name: expression} over variables named like the state
fields ('bx0'..'bx4', 'brick0'.., 'lives1', 'score0'.., 'screen0') plus 'left',
'right', 'reset'. Outputs are the next state (same names with a trailing "'")
plus 'win', 'lose' for the current state.
"""
from hw.logic import var, AND, OR, NOT, XOR, ORX, MUX, ONE, ZERO
from state import FIELDS

INIT = dict(bx=16, by=29, dx=0, dy=1, px=13, bricks=(1 << 32) - 1, lives=3,
            score=0, speed=0, ph=0, hits=0, small=0, screen=0)
BALL0 = dict(bx=16, by=29, dx=0, dy=1)
NEW_BALL = dict(hits=0, speed=0, small=0)  # reset with the ball (lost or new screen)


def bits(name, w):
    return [var(f'{name}{i}') for i in range(w)]


def const(v, w):
    return [ONE if v >> i & 1 else ZERO for i in range(w)]


def all_of(xs):
    out = ONE
    for x in xs:
        out = AND(out, x)
    return out


def eq(v, k):
    return all_of(b if k >> i & 1 else NOT(b) for i, b in enumerate(v))


def step_by_one(v, down, up=ONE):
    """v - 1 if down, v + 1 if up (callers make down and up exclusive), else v."""
    out, carry, borrow = [], ONE, ONE
    for b in v:
        out.append(XOR(b, ORX(AND(up, carry), AND(down, borrow))))
        carry, borrow = AND(carry, b), AND(borrow, NOT(b))
    return out


def sub(a, b):
    """a - b mod 2^w and the borrow out."""
    out, br = [], ZERO
    for x, y in zip(a, b):
        out.append(XOR(XOR(x, y), br))
        br = OR(AND(NOT(x), y), AND(NOT(XOR(x, y)), br))
    return out, br


def add(a, b):
    """a + b mod 2^len(a); b may be shorter."""
    out, c = [], ZERO
    for i, x in enumerate(a):
        y = b[i] if i < len(b) else ZERO
        out.append(XOR(XOR(x, y), c))
        c = OR(AND(x, y), AND(XOR(x, y), c))
    return out


def build():
    S = {name: bits(name, w) for name, w in FIELDS}
    bricks = bits('brick', 32)
    S['bricks'] = bricks
    left, right, reset = var('left'), var('right'), var('reset')
    bx, by, px, lives = S['bx'], S['by'], S['px'], S['lives']
    score, speed, ph, hits = S['score'], S['speed'], S['ph'], S['hits']
    dx, dy, small, screen = S['dx'][0], S['dy'][0], S['small'][0], S['screen'][0]

    lose = eq(lives, 0)
    cleared = all_of(NOT(b) for b in bricks)
    win = AND(cleared, screen)
    run = NOT(OR(lose, win))  # frozen games change nothing: every cause of change is gated by run
    newscreen = AND(run, cleared)  # (and not screen, as win would have frozen it)
    go = AND(run, NOT(cleared))

    px_max = MUX(small, eq(px, 29), eq(px, 26))
    mv_left = AND(AND(AND(left, NOT(right)), NOT(eq(px, 0))), run)
    mv_right = AND(AND(AND(right, NOT(left)), NOT(px_max)), run)
    px1 = step_by_one(px, mv_left, mv_right)
    ph1 = step_by_one(ph, ZERO, run)

    # cadence: bitrev(ph) = (r2 r1 r0) < (4, 5, 6, 8)[speed]
    r2, r1, r0 = ph[0], ph[1], ph[2]
    cad = ORX(AND(eq(speed, 0), NOT(r2)),
              AND(eq(speed, 1), OR(NOT(r2), AND(NOT(r1), NOT(r0)))),
              AND(eq(speed, 2), OR(NOT(r2), NOT(r1))),
              eq(speed, 3))
    move = AND(go, cad)

    flip_x = AND(ORX(AND(NOT(dx), eq(bx, 31)), AND(dx, eq(bx, 0))), move)
    dx1 = XOR(dx, flip_x)
    bx1 = step_by_one(bx, AND(dx1, move), AND(NOT(dx1), move))

    d, borrow = sub(bx1, px)
    within = MUX(small, AND(NOT(d[2]), NOT(AND(d[1], d[0]))), NOT(AND(d[2], d[1])))  # d < 3 or d < 6
    paddle_ok = AND(NOT(borrow), AND(AND(NOT(d[4]), NOT(d[3])), within))
    ceiling = AND(dy, eq(by, 0))
    paddle = AND(AND(NOT(dy), eq(by, 29)), paddle_ok)
    dy1 = XOR(dy, AND(ORX(ceiling, paddle), move))
    lost = AND(AND(NOT(dy1), eq(by, 31)), move)
    by1 = step_by_one(by, AND(dy1, move), AND(NOT(dy1), move))
    small1 = OR(small, AND(ceiling, move))

    # one-hot brick decode as a switch tree: each split is one keep-both gate
    tree = [AND(AND(NOT(by1[4]), by1[3]), move)]
    for b in (by1[2], by1[1], bx1[4], bx1[3], bx1[2]):  # brick index bits, high first
        tree = [t for x in tree for t in (AND(x, NOT(b)), AND(x, b))]
    hit, bricks1 = [], []
    for i, b in enumerate(bricks):
        h = tree[i]
        hit.append(AND(b, h))
        bricks1.append(AND(b, NOT(h)))
    hit_any = ORX(*hit)
    dy2 = XOR(dy1, hit_any)
    # points 7, 5, 3, 1 by row ((by1 >> 1) & 3 = by1[1], by1[2]): 1, ~row0, ~row1
    score1 = add(score, [hit_any, AND(hit_any, NOT(by1[1])), AND(hit_any, NOT(by1[2]))])
    hits1 = step_by_one(hits, ZERO, AND(hit_any, NOT(eq(hits, 15))))
    red = AND(hit_any, AND(NOT(by1[1]), NOT(by1[2])))
    orange = AND(hit_any, AND(by1[1], NOT(by1[2])))
    ge4, ge12 = AND(hit_any, OR(hits1[2], hits1[3])), AND(hit_any, AND(hits1[2], hits1[3]))
    l3 = red
    l2 = AND(OR(orange, ge12), NOT(red))
    l1 = AND(ge4, NOT(OR(red, OR(orange, ge12))))
    s1, s0 = speed[1], speed[0]
    speed1 = [ORX(l3, AND(l2, AND(s1, s0)), AND(NOT(OR(l3, l2)), OR(s0, AND(l1, NOT(s1))))),
              OR(OR(l3, l2), s1)]
    lives1 = step_by_one(lives, lost, ZERO)

    normal = dict(bx=bx1, by=by1, dx=[dx1], dy=[dy2], px=px1, bricks=bricks1, lives=lives1,
                  score=score1, speed=speed1, ph=ph1, hits=hits1, small=[small1], screen=[screen])
    respawn = ORX(lost, newscreen)
    out = {}
    for name, w in FIELDS:
        new = normal[name]
        if name in BALL0:
            new = [MUX(respawn, k, n) for k, n in zip(const(BALL0[name], w), new)]
        if name in NEW_BALL:
            new = [AND(NOT(respawn), n) for n in new]
        if name == 'bricks':
            new = [ORX(newscreen, AND(NOT(newscreen), n)) for n in new]
        if name == 'screen':
            new = [OR(newscreen, n) for n in new]
        new = [MUX(reset, k, n) for k, n in zip(const(INIT[name], w), new)]
        for i, e in enumerate(new):
            out[f"{'brick' if name == 'bricks' else name}{i}'"] = e
    out['win'], out['lose'] = win, lose
    return out


def env_of(state, left, right, reset):
    env = dict(left=left, right=right, reset=reset)
    for (name, w), v in zip(FIELDS, state):
        for i in range(w):
            env[f"{'brick' if name == 'bricks' else name}{i}"] = v >> i & 1
    return env


def state_of(values):
    from state import State
    vals = []
    for name, w in FIELDS:
        key = 'brick' if name == 'bricks' else name
        vals.append(sum(values[f"{key}{i}'"] << i for i in range(w)))
    return State(*vals)
