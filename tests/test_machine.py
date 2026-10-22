"""Whole pipeline: logic -> switch netlist -> plan -> bars + feeds -> the CA, frame by frame."""
import random
import time
from hw.logic import var, XOR, AND, OR
from hw.lower import lower
from plan import Plan, check
from compile import Machine
import netsim


def run_vs_netsim(outs, state, ext, extra, nframes, seed, init=None):
    net = lower(outs)
    plan = Plan(net, state, ext=ext, extra_out=extra)
    check(plan)
    m = Machine(plan)
    rng = random.Random(seed)
    ins = [{e: rng.randrange(2) for e in ext} for _ in range(nframes)]
    st = init or {s: rng.randrange(2) for s in state}
    t = time.time()
    got = m.simulate(st, ins, nframes)
    dt = time.time() - t
    for n in range(nframes + 1):
        assert {s: got[n][s] for s in state} == st, (n, got[n], st)
        if n == nframes:
            break
        v = netsim.run(net, {**st, **ins[n]})
        st = {s: v[o] for s, o in state.items()}
        for e in extra:
            assert got[n + 1][e] == v[e], (n, e)
    return m, dt


def test_counter_with_enable():
    q0, q1, en = var('q0'), var('q1'), var('en')
    outs = {"q0'": XOR(q0, en), "q1'": XOR(q1, AND(q0, en)), 'carry': AND(AND(q0, q1), en)}
    run_vs_netsim(outs, {'q0': "q0'", 'q1': "q1'"}, ['en'], ['carry'], 12, 1)


def test_four_bit_accumulator():
    acc = [var(f'a{i}') for i in range(4)]
    inp = [var(f'i{i}') for i in range(4)]
    outs, c = {}, None
    for i in range(4):
        s = XOR(acc[i], inp[i]) if c is None else XOR(XOR(acc[i], inp[i]), c)
        g = AND(acc[i], inp[i])
        c = g if c is None else OR(g, AND(XOR(acc[i], inp[i]), c))
        outs[f"a{i}'"] = s
    outs['cout'] = c
    m, dt = run_vs_netsim(outs, {f'a{i}': f"a{i}'" for i in range(4)}, [f'i{i}' for i in range(4)],
                          ['cout'], 20, 2)
    print(f'    {len(m.plan.slots)} slots, P={m.P}, {len(m.L.bars)} bars, {dt / 20:.1f}s/frame')


def test_keep_both_outputs():
    """An op whose A and B outputs are both used (B lands on its own line)."""
    from hw.logic import NOT
    a, b, c = var('a'), var('b'), var('c')
    outs = {"a'": AND(a, b), "b'": AND(a, NOT(b)), "c'": XOR(c, b)}
    assert any(op.keep == 'AB' for op in lower(outs).ops)
    for x in range(8):
        run_vs_netsim(outs, {'a': "a'", 'b': "b'", 'c': "c'"}, [], [], 3, x,
                      init={'a': x >> 2 & 1, 'b': x >> 1 & 1, 'c': x & 1})
