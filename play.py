"""Play brick breaker on the billiard-ball computer.

Every frame of the game is computed by the billiard-ball cellular automaton
(csim/bbm.c via fastsim.py) running the compiled hardware in build/. This file
only adds key balls at the input ports (plus the fixed power schedule the
runner adds), reads the state lanes, and draws them. It starts from an empty
world and holds reset on the first frame, so the circuit sets the game up.
"""
import os
import sys
import fastsim
import hardware
import screen
from state import FIELDS, State


def decode(bits):
    vals = []
    for name, w in FIELDS:
        key = 'brick' if name == 'bricks' else name
        vals.append(sum(bits[f'{key}{i}'] << i for i in range(w)))
    return State(*vals)


def main():
    hw = hardware.load()
    threads = min(16, os.cpu_count() or 1)
    if '--threads' in sys.argv:
        threads = int(sys.argv[sys.argv.index('--threads') + 1])
    fastsim.FastCyl.threads = threads
    runner = fastsim.Runner(hw)

    def stepper(left, right, reset):
        n = len(runner.inputs)
        runner.feed(dict(left=int(left), right=int(right), reset=int(reset)))
        bits = runner.outputs[n]
        return decode(bits), bits['win'], bits['lose']

    if '--bench' in sys.argv:
        import time
        frames = int(sys.argv[sys.argv.index('--bench') + 1])
        stepper(0, 0, 1)
        t = time.time()
        for k in range(frames):
            stepper(k % 7 == 0, k % 5 == 0, 0)
        dt = time.time() - t
        print(f'{frames} frames in {dt:.1f}s = {frames / dt:.2f} frames/s')
        return
    screen.run(stepper)


if __name__ == '__main__':
    main()
