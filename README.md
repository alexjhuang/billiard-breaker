# Brick Breaker on a Billiard Ball Computer

A playable brick-breaker game whose entire game logic is computed by billiard
balls: a Margolus billiard-ball-model cellular automaton (BBMCA). The frontend
only adds balls at input ports (your keys, plus a fixed schedule of "power"
balls that is the same every frame), deletes balls that leave the world, and
reads where the state balls are. Every frame of game state is computed by
balls bouncing off mirrors and each other.

```
python3 game_machine.py     # compile the game into mirrors + a ball schedule (build/, ~10 s)
python3 play.py             # play it (a/d or arrows, r reset, q quit; terminal >= 66x35)
python3 play_oracle.py      # the same game in plain Python, for comparison
python3 tests/run_all.py    # all tests, incl. the 20 x 1000-frame differential test (~3 min)
python3 difftest.py 1000 20 --threads 4   # billiard machine vs reference, 20 x 1000 frames
```

Needs Python 3.12 (stdlib only) and gcc, which builds the C simulator on first use.

## How it works

**Physics (sim.py, physics.py).** The world is a grid of cells updated in
2x2 blocks whose alignment alternates every step. A lone ball moves to the
opposite corner (so it flies diagonally), two balls on a diagonal turn to the
other diagonal, and everything else stays put. Static walls are 2x2 squares of
balls at the right parity. A single ball hitting a wall just bounces back, so
a signal is a *dimer*: two balls on one diagonal, two cells apart. It reflects
90° off a 2-thick bar. Two dimers crossing at right angles with exactly the
same timing collide cleanly: each hops to the neighbouring parallel track. Off
by 2 steps they shatter, and 4 or more steps apart they pass straight through.
physics.py has exact formulas for mirrors, collisions and mirror ends, all
re-checked against the automaton in tests/test_physics.py.

**The gate (layout.py).** Each gate is a falling *messenger* dimer with gap 6
(a power ball). It hits control lane X, which always hops (X survives and can
be read again), and the messenger hops only if X was there. It then meets data
lane R, lined up so they collide only if the messenger hopped. R's two
possible tracks are split apart by the end of a short mirror. The outputs are
X, R∧X and R∧¬X: Fredkin's switch gate. With R a power ball it gives copy and
NOT. Every gate in a frame uses this single construction.

**The world (cyl.py).** The world is a band wrapped into a cylinder along the
direction lanes travel, so state lanes simply circle back to the start of the
next frame. The two edges of the band are the sources and sinks. Lanes move
along the loop; messengers fall across it.

**Timing.** All lanes rest on a lattice of rows (y − t ≡ 0 mod 28). A gate
delays its control and data lanes onto the messenger's row with small detours
(two jogs) and puts them back afterwards; idle lanes take a 28-row detour in
every slot to stay in step. Every unplanned crossing between a falling ball
and a lane is then at least 8 rows away from any offset that would shatter.

**The compiler.** hw/game.py writes the frame step as boolean logic (frozen
games are handled by gating every cause of change; the brick decoder is a
switch tree). hw/lower.py turns it into switch gates. plan.py gives each gate a
slot along the loop and every signal a line, and enforces that messengers
fall: a gate's data lane must sit below its controls, inserting copies where
needed. Next-state values therefore drift down the band and are lifted back to
their state lines as soon as they are final. compile.py turns slots into
mirrors and the per-frame ball schedule; hardware.py exports just that.

**The simulator (csim/bbm.c).** An exact sparse simulator: walls and moving
balls in bitmaps, only moving balls visited each step. For speed it runs on
threads using overlapping tiles: every 32 steps the balls are split across
the band (by v) into ranges with equal counts, and each thread runs its range
plus a ghost margin of 2·32+4 on a private copy, keeping only its own range.
Nothing moves faster than 2 per step, so the kept balls are exact. (Cutting
along the loop instead does badly: the lanes travel together, so those ranges
end up narrower than their margins.) Results are identical to the
single-threaded simulator (tests/test_fastsim.py), which is itself checked
against the pure-Python reference simulators.

| Whole frame (u along the loop, v up the band; dark = mirrors) |
|---|
| ![frame](docs/frame.png) |

The block on top is every live lane taking its idle detour in each slot.
Values drift down the band as the computation proceeds, and the lifts at the
right bring next-state values back up. `docs/slot.png` zooms in on one gate.

## Measured (this machine: 192 cores, shared with other users)

| | |
|---|---|
| frame rate, `python3 play.py --bench 40` (16 threads) | **15.9, 16.5, 18.2 frames/s** (3 runs, load average 51–59) |
| frame rate, `--threads 1` | 6.8 frames/s |
| world | cylinder 25 900 × 6 181 (u × x−y): 160 M cells |
| frame period P | 43 456 automaton steps |
| gates | 488 switch gates + 72 copies = 560 messengers per frame |
| slots | 627 (3 input, 560 gate, 1 lift, 63 catch-up) |
| mirrors | 150 402 bars (1.2 M wall cells) |
| balls added per frame | 816 (560 messengers, 253 power, 3 key inputs) |
| lanes alive | ≤ 150 lines; ~207 moving balls on average |
| tiles (layout primitives placed) | 560 messengers, 560 splitters, 256 power/input sources, 321 drops, 37 300 jogs |

## Verification

- `tests/run_all.py`: physics formulas vs the automaton; every layout primitive
  and a full gate slot (all inputs, both gate orientations); small circuits
  (counter, 4-bit accumulator, keep-both gates) frame by frame vs the gate-level
  simulator; the game logic and the switch netlist vs the reference on random
  states; the C simulator (serial and threaded) vs the Python simulators;
  play.py's frames vs the reference, a check that play.py never imports game
  logic, and a terminal smoke run; and the differential test below
  (tests/test_difftest.py: 20 sequences × 1000 frames, plus 2 bot games of
  1500 frames that must each reach a win).
- `difftest.py`: the billiard machine against `oracle.py`, bit for bit, state
  and win/lose flags every frame. Last full run on the current build:
  20/20 sequences × 1000 frames (4 threads) and 2 × 1500 frames with full
  wins (16 threads, as play.py runs it); logs in build/.

## Known limitations

- Frame rate depends on free cores: with 16 threads one frame is ~1 360
  barrier-separated epochs of ~40 µs, so other load on the machine shows up
  directly. Serially the simulator does ~17 ns per ball per step, and one frame
  is ~43 000 steps × ~207 balls.
- Display lags input by one frame, and win/lose flags lag the state by one
  frame (they are computed from the frame's input state).
- The circuit runs gates one per slot. Ideas that would shorten the frame
  further but did not pay off yet are in DECISIONS.md: packing several gates
  per slot with lazy timing, and splitting the loop into parallel strips.

## What I'd do next

- Shorten the frame: lanes now all detour together in every slot, so P grows
  by 28 steps per gate. Letting lanes keep their own timing and packing
  independent gates into the same slot would cut P several-fold.
- Partition the simulator in 2-D (u and v) to scale past ~16 threads.
- Pin threads to cores; on a busy shared machine stragglers set the pace.

## Layout of the repo

`SPEC.md` game rules · `oracle.py` reference game · `hw/` logic and lowering ·
`plan.py`, `compile.py`, `layout.py`, `physics.py` compiler · `sim.py`, `cyl.py`
Python simulators · `csim/bbm.c`, `fastsim.py` C simulator · `play.py`,
`screen.py` frontend · `debug.py` fault finder · `difftest.py` differential test ·
`DECISIONS.md` design log · `PROGRESS.md` work log.
