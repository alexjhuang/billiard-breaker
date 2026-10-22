"""Small shared fixtures for tests."""
import random
from hw.logic import var, XOR, AND, OR
from hw.lower import lower
from plan import Plan
from compile import Machine


def small_machine(frames=6, seed=2):
    acc = [var(f'a{i}') for i in range(4)]
    inp = [var(f'i{i}') for i in range(4)]
    outs, c = {}, None
    for i in range(4):
        s = XOR(acc[i], inp[i]) if c is None else XOR(XOR(acc[i], inp[i]), c)
        g = AND(acc[i], inp[i])
        c = g if c is None else OR(g, AND(XOR(acc[i], inp[i]), c))
        outs[f"a{i}'"] = s
    outs['cout'] = c
    net = lower(outs)
    m = Machine(Plan(net, {f'a{i}': f"a{i}'" for i in range(4)}, ext=[f'i{i}' for i in range(4)], extra_out=['cout']))
    rng = random.Random(seed)
    return m, {f'a{i}': rng.randrange(2) for i in range(4)}, [{f'i{i}': rng.randrange(2) for i in range(4)} for _ in range(frames)]
