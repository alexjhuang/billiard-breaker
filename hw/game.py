"""The brick-breaker frame step as boolean logic (mirrors oracle.step bit for bit).

build() returns {output name: expression} over variables named like the state
fields ('bx0'..'bx4', 'brick0'.., 'lives1') plus 'left', 'right', 'reset'.
Outputs are the next state (same names with a trailing "'") plus 'win', 'lose'
for the current state.
"""
from hw.logic import var, AND, OR, NOT, XOR, ORX, MUX, ONE, ZERO
from state import FIELDS

INIT = dict(bx=16, by=29, dx=0, dy=1, px=13, bricks=(1 << 32) - 1, lives=3)
BALL0 = dict(bx=16, by=29, dx=0, dy=1)


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


def build():
    S = {name: bits(name, w) for name, w in FIELDS}
    bricks = bits('brick', 32)
    S['bricks'] = bricks
    left, right, reset = var('left'), var('right'), var('reset')
    bx, by, px, lives = S['bx'], S['by'], S['px'], S['lives']
    dx, dy = S['dx'][0], S['dy'][0]

    lose = eq(lives, 0)
    win = all_of(NOT(b) for b in bricks)
    run = NOT(OR(lose, win))  # frozen games change nothing: every cause of change is gated by run

    mv_left = AND(AND(AND(left, NOT(right)), NOT(eq(px, 0))), run)
    mv_right = AND(AND(AND(right, NOT(left)), NOT(eq(px, 26))), run)
    px1 = step_by_one(px, mv_left, mv_right)

    flip_x = AND(ORX(AND(NOT(dx), eq(bx, 31)), AND(dx, eq(bx, 0))), run)
    dx1 = XOR(dx, flip_x)
    bx1 = step_by_one(bx, AND(dx1, run), AND(NOT(dx1), run))

    d, borrow = sub(bx1, px)
    paddle_ok = AND(NOT(borrow), AND(AND(NOT(d[4]), NOT(d[3])), NOT(AND(d[2], d[1]))))
    ceiling = AND(dy, eq(by, 0))
    paddle = AND(AND(NOT(dy), eq(by, 29)), paddle_ok)
    dy1 = XOR(dy, AND(ORX(ceiling, paddle), run))
    lost = AND(AND(NOT(dy1), eq(by, 31)), run)
    by1 = step_by_one(by, AND(dy1, run), AND(NOT(dy1), run))

    # one-hot brick decode as a switch tree: each split is one keep-both gate
    tree = [AND(AND(NOT(by1[4]), by1[3]), run)]
    for b in (by1[2], by1[1], bx1[4], bx1[3], bx1[2]):  # brick index bits, high first
        tree = [t for x in tree for t in (AND(x, NOT(b)), AND(x, b))]
    hits, bricks1 = [], []
    for i, b in enumerate(bricks):
        h = tree[i]
        hits.append(AND(b, h))
        bricks1.append(AND(b, NOT(h)))
    dy2 = XOR(dy1, ORX(*hits))
    lives1 = step_by_one(lives, lost, ZERO)

    normal = dict(bx=bx1, by=by1, dx=[dx1], dy=[dy2], px=px1, bricks=bricks1, lives=lives1)
    out = {}
    for name, w in FIELDS:
        new = normal[name]
        if name in BALL0:
            new = [MUX(lost, k, n) for k, n in zip(const(BALL0[name], w), new)]
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
