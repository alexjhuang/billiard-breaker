"""Differential test: the billiard-ball machine vs oracle.py over many frames.

Usage: python3 difftest.py FRAMES SEQUENCES [first_seed] [--python] [--threads N]
Each sequence starts from the initial state. Even seeds use random keys;
odd seeds a noisy paddle-tracking bot, both with occasional resets. Seeds
>= 1000 use the bot with no random resets, so games run to a win. Sequences run in parallel, one
process each, in the C simulator (or cyl.py with --python).
"""
import multiprocessing as mp
import random
import sys
import time
import game_machine
import oracle
from hw import game
from state import State, paddle_w

EXT = ('left', 'right', 'reset')


def bits_to_state(bits):
    vals = {**{k: bits[k] for k in bits}}
    env = {k + "'": v for k, v in vals.items()}
    return game.state_of(env)


def aim(st):
    """Where the bot wants the paddle's left edge: under the ball's next crossing
    of the paddle row (the oracle tells it; the paddle moves 1 a frame, so it
    has to start early when the ball is fast and the paddle small)."""
    s, w = st, paddle_w(st)
    for _ in range(80):
        if s.dy == 0 and s.by == 29 and oracle.moves(s):
            bx_new = s.bx + (-1 if (s.dx == 1) != (s.bx == 0) else 1)
            return min(max(bx_new - w // 2, 0), 32 - w)
        s = oracle.step(s, 0, 0, 0)
    return st.px


def inputs_for(seed, n):
    """Per-frame inputs, decided from the oracle's state (the machine must agree)."""
    rng = random.Random(seed)
    st, out = oracle.INIT, []
    if seed >= 1000:  # pure bot, no random resets: plays games to the end (covers win)
        for _ in range(n):
            c = aim(st)
            inp = dict(left=int(st.px > c), right=int(st.px < c),
                       reset=int((oracle.win(st) or oracle.lose(st)) and rng.random() < .05))
            out.append(inp)
            st = oracle.step(st, inp['left'], inp['right'], inp['reset'])
        return out
    for _ in range(n):
        if seed % 2 and rng.random() < .9:
            c = aim(st)
            inp = dict(left=int(st.px > c), right=int(st.px < c), reset=0)
        else:
            inp = dict(left=int(rng.random() < .35), right=int(rng.random() < .35), reset=0)
        inp['reset'] = int(rng.random() < .005 or (oracle.win(st) or oracle.lose(st)) and rng.random() < .1)
        out.append(inp)
        st = oracle.step(st, inp['left'], inp['right'], inp['reset'])
    return out


def run(args):
    seed, nframes, python, threads = args
    m = game_machine.load()
    ins = inputs_for(seed, nframes)
    env = game.env_of(oracle.INIT, 0, 0, 0)
    init = {k: v for k, v in env.items() if k not in EXT}
    t = time.time()
    if python:
        got = m.simulate(init, ins, nframes)
    else:
        import fastsim
        fastsim.FastCyl.threads = threads
        got = fastsim.simulate(m, init, ins, nframes)
    st, wins, bricks_hit = oracle.INIT, 0, 0
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
        wins += oracle.win(st) and not oracle.win(prev)
        bricks_hit += bin(prev.bricks).count('1') - bin(st.bricks).count('1') if not ins[n]['reset'] else 0
    return seed, f'ok ({wins} wins, {bricks_hit} bricks broken)', time.time() - t


if __name__ == '__main__':
    argv = sys.argv[1:]
    threads = 1
    if '--threads' in argv:
        k = argv.index('--threads')
        threads = int(argv[k + 1])
        del argv[k:k + 2]
    args = [a for a in argv if not a.startswith('--')]
    python = '--python' in argv
    frames, seqs = int(args[0]), int(args[1])
    first = int(args[2]) if len(args) > 2 else 0
    game_machine.load()
    with mp.Pool(min(seqs, mp.cpu_count())) as p:
        res = p.map(run, [(s, frames, python, threads) for s in range(first, first + seqs)])
    bad = [r for r in res if not r[1].startswith('ok')]
    for r in res:
        print(f'seed {r[0]}: {r[1]} ({r[2]:.0f}s, {r[2] / frames:.1f}s/frame)')
    print(f'{len(res) - len(bad)}/{len(res)} sequences matched the oracle for {frames} frames')
    sys.exit(1 if bad else 0)
