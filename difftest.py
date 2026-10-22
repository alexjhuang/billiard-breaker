"""Differential test: the billiard-ball machine vs oracle.py over many frames.

Usage: python3 difftest.py FRAMES SEQUENCES [first_seed]
Each sequence starts from the initial state with random inputs (a reset now
and then). Sequences run in parallel, one process each.
"""
import multiprocessing as mp
import random
import sys
import time
import game_machine
import oracle
from hw import game
from state import State

EXT = ('left', 'right', 'reset')


def bits_to_state(bits):
    vals = {**{k: bits[k] for k in bits}}
    env = {k + "'": v for k, v in vals.items()}
    return game.state_of(env)


def inputs_for(seed, n):
    rng = random.Random(seed)
    return [dict(left=int(rng.random() < .35), right=int(rng.random() < .35), reset=int(rng.random() < .01))
            for _ in range(n)]


def run(args):
    seed, nframes = args
    m = game_machine.load()
    ins = inputs_for(seed, nframes)
    env = game.env_of(oracle.INIT, 0, 0, 0)
    init = {k: v for k, v in env.items() if k not in EXT}
    t = time.time()
    got = m.simulate(init, ins, nframes)
    st = oracle.INIT
    for n in range(nframes + 1):
        mine = bits_to_state({k: v for k, v in got[n].items() if k not in ('win', 'lose')})
        if mine != st:
            return seed, f'frame {n}: machine {mine} oracle {st}', time.time() - t
        if n:
            if (got[n]['win'], got[n]['lose']) != (oracle.win(prev), oracle.lose(prev)):
                return seed, f'frame {n}: flags {got[n]["win"], got[n]["lose"]} want {oracle.win(prev), oracle.lose(prev)}', time.time() - t
        if n == nframes:
            break
        prev = st
        st = oracle.step(st, ins[n]['left'], ins[n]['right'], ins[n]['reset'])
    return seed, 'ok', time.time() - t


if __name__ == '__main__':
    frames, seqs = int(sys.argv[1]), int(sys.argv[2])
    first = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    game_machine.load()
    with mp.Pool(min(seqs, mp.cpu_count())) as p:
        res = p.map(run, [(s, frames) for s in range(first, first + seqs)])
    bad = [r for r in res if r[1] != 'ok']
    for r in res:
        print(f'seed {r[0]}: {r[1]} ({r[2]:.0f}s, {r[2] / frames:.1f}s/frame)')
    print(f'{len(res) - len(bad)}/{len(res)} sequences matched the oracle for {frames} frames')
    sys.exit(1 if bad else 0)
