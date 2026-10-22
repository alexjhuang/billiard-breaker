"""The compiled machine as plain data: what a frontend needs and nothing more.

{'U', 'lo', 'hi', 'P'}: world shape and frame period;
'walls': list of (x, y) wall cells; 'feeds': list of (t, cells, tag) added
every frame at t + n*P ('power', 'messenger' or 'input:<key>');
'reads': {output name: (t, cells)} looked at every frame at t + n*P.
No netlist, no game rules.
"""
import os
import pickle

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'build', 'hardware.pkl')


def export(m, path=PATH):
    hw = dict(U=m.U, lo=m.lo, hi=m.hi, P=m.P,
              walls=[c for b in m.L.bars for c in b.cells()],
              feeds=[(t, sorted(b.cells(t)), tag) for t, b, tag in m.L.feeds],
              reads={name: (-(lane.p - lane.q) // 2, sorted(lane.cells(-(lane.p - lane.q) // 2)))
                     for name, lane in m.readout_balls().items()})
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        pickle.dump(hw, f, protocol=pickle.HIGHEST_PROTOCOL)
    return hw


def load(path=PATH):
    if not os.path.exists(path):
        raise SystemExit(f'{path} missing: run  python3 game_machine.py  first')
    with open(path, 'rb') as f:
        return pickle.load(f)
