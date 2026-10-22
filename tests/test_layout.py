"""Layout primitives and a whole gate slot, run in the real automaton (cyl.py)."""
from layout import Layout
from physics import Ball, interaction


def run(L, lanes, t0, T):
    w = L.world(lanes, t0)
    L.step_world(w, T - t0)
    return w


def test_retime_sizes():
    for delay in (10, 14, 18, 32):
        for dline in (-6, 0):
            L = Layout(400, -300, 300)
            lane = Ball('SE', 2, -60, -60)
            out = L.retime(lane, 0, delay, dline)
            assert out.q == lane.q - delay and out.p - out.q == dline
            w = run(L, [lane], 0, 200)
            assert not w.broken and w.moving == {w.canon(*c) for c in out.cells(200)}, (delay, dline)


LINES = {'top': 80, 'X': 40, 'mid': 0, 'R': -40, 'bot': -80}
RHO = -100  # every lane rests at this row at the start of the slot


def build_slot(power, keep):
    """One switch gate: messenger reads X, writes R; idle lanes take the
    32-row sync bump. Returns layout and the predicted lanes leaving the slot."""
    L = Layout(2000, -120, 120)
    lanes = {n: Ball('SE', 2, c + RHO, RHO) for n, c in LINES.items()}
    u0 = 60
    if power:
        lanes['R'] = L.power(u0 - 4, LINES['R'], RHO - 14)
    uA = u0 + 3
    lanes['X'] = L.retime(lanes['X'], uA, 12)
    if not power:
        lanes['R'] = L.retime(lanes['R'], uA, 14)
    idle = {n: L.retime(lanes[n], uA, 32) for n in ('top', 'mid', 'bot')}
    Q = L.messenger(2 * (u0 + 13) + 1, RHO - 12)
    L.split(lanes['R'], u0 + 17, keep)
    _, X2, Q2 = interaction(lanes['X'], Q)
    _, A, _ = interaction(lanes['R'], Q2)
    uD = u0 + 21
    out = {'X': L.retime(X2, uD, 14, -6),
           'A': L.retime(A, uD, 12, -6) if keep == 'A' else None,
           'B': L.retime(lanes['R'], uD, 18) if keep == 'B' else None}
    return L, idle, out


def test_gate_slot_all_cases():
    for power in (False, True):
        for keep in 'AB':
            L, idle, out = build_slot(power, keep)
            for x in (0, 1):
                for r in ((1,) if power else (0, 1)):
                    start = [Ball('SE', 2, c + RHO, RHO) for n, c in LINES.items() if n in ('top', 'mid', 'bot')]
                    start += [Ball('SE', 2, LINES['X'] + RHO, RHO)] * x
                    start += [Ball('SE', 2, LINES['R'] + RHO, RHO)] * (r and not power)
                    want = list(idle.values()) + [out['X']] * x
                    if r and keep == 'A' and x:
                        want.append(out['A'])
                    if r and keep == 'B' and not x:
                        want.append(out['B'])
                    assert all(b.q == RHO - 32 for b in want)  # everything leaves in sync
                    w = run(L, start, 0, 300)
                    assert not w.broken
                    assert w.moving == {w.canon(*c) for b in want for c in b.cells(300)}, (power, keep, x, r)
