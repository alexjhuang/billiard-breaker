"""Build (and cache) the compiled brick-breaker machine."""
import os
import pickle
from hw import game
from hw.lower import lower
from plan import Plan, check
from compile import Machine
from state import FIELDS

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'build', 'game_machine.pkl')


def state_names():
    names = {}
    for name, w in FIELDS:
        key = 'brick' if name == 'bricks' else name
        for i in range(w):
            names[f'{key}{i}'] = f"{key}{i}'"
    return names


def build():
    net = lower(game.build())
    plan = Plan(net, state_names(), ext=['left', 'right', 'reset'], extra_out=['win', 'lose'])
    check(plan)
    return Machine(plan)


def load(rebuild=False):
    if not rebuild and os.path.exists(CACHE):
        with open(CACHE, 'rb') as f:
            return pickle.load(f)
    m = build()
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, 'wb') as f:
        pickle.dump(m, f, protocol=pickle.HIGHEST_PROTOCOL)
    return m


if __name__ == '__main__':
    import time
    import hardware
    t = time.time()
    m = load(rebuild=True)
    hardware.export(m)
    print(f'built in {time.time() - t:.1f}s: {len(m.plan.slots)} slots, U={m.U}, P={m.P}, '
          f'{len(m.L.bars)} bars, {len(m.L.feeds)} balls per frame -> {hardware.PATH}')
