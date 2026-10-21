# Goal: Brick Breaker on a Billiard Ball Computer

Build a playable brick-breaker game whose entire game logic is computed by a
billiard-ball-model cellular automaton (BBMCA). This will take days. You will be
resumed many times and lose context between sessions — the repo files are your
memory. Project dir: `~/billiard-breaker` (git repo; create if missing).

## Definition of done
All of these are true, each verified by a command you actually ran:
1. `python3 play.py` launches an interactive terminal game: paddle moves with
   arrow keys / A-D, ball bounces, bricks break, lives drop, win/lose shown.
2. Every frame of game state is computed by the BBMCA. The frontend may only:
   (a) inject input balls at input ports, (b) inject input-independent constant
   "power" streams at source ports, (c) delete balls arriving at sink ports,
   (d) read output cells to draw the screen. Nothing else.
3. `python3 tests/run_all.py` passes, including the end-to-end differential test:
   BBMCA vs reference oracle, bit-exact, ≥1000 frames × ≥20 random input sequences.
4. README.md documents architecture, how to run, and *measured* numbers:
   fps on this machine, grid size, frame period P, gate count, tile count.
Performance target: ≥15 game frames/s; ≥5 counts as playable. If missed, done
still requires the honest measured fps and a profiled bottleneck in the README.

## Fixed decisions (do not relitigate)
- **Model:** Margolus BBMCA. 2D bit grid, 2×2 blocks, partition offset alternates
  (0,0)/(1,1) every step. Per block: exactly one ball → moves to the diagonally
  opposite cell; exactly two balls on a diagonal → move to the other diagonal;
  every other configuration unchanged. References: Margolus, "Physics-like models
  of computation" (Physica D, 1984); Toffoli & Margolus, *Cellular Automata
  Machines* (1987). Verify free flight, collision, and reflection empirically
  before building on it. If static mirrors are awkward in the pure rule, you may
  add a second fixed bit-plane for walls — record it in DECISIONS.md.
- **Architecture:** one synchronous loop. State bits leave the logic and route
  back to its inputs with total loop latency exactly P steps for every bit.
  One game frame = P steps. All signals in a frame share one phase.
- **Layout via fixed tiles:** a small library of fixed-size tiles (straight, turn,
  crossover, delay, interaction/AND gate, copy/NOT using a constant-1 stream,
  source, sink), each with the *same* input→output latency (pad with delay as
  needed). Timing alignment then reduces to counting tiles. Hand-compaction is a
  later optimization only.
- **Irreversibility:** constant-1 balls enter at boundary sources each frame;
  garbage exits at sinks and is deleted. That is the only non-reversible part.
- **Synthesis:** write the frame-step logic once in Verilog (`hw/game.v`) and
  synthesize with Yosys to a netlist over the gate basis your tiles support
  (e.g. `abc -g ...` then `write_json`). If Yosys isn't installable in one
  command, write a tiny Python DSL that emits the same netlist JSON. Decide in M0.
- **Deps:** Python 3 + numpy. Optional only if available or one-command install:
  yosys, numba, a GPU array lib. Frontend uses curses (stdlib). Nothing else.

## Game spec (pin exact rules in SPEC.md during M1, then freeze)
Small on purpose:
- Field 32×32, 5-bit x/y, y=0 at top.
- Ball: one cell, moves exactly one cell diagonally per frame (dx, dy ∈ {−1,+1}).
- Bricks: 8 columns × 4 rows, each 4 wide × 2 tall, occupying y=2..9 → 32-bit bitmap.
- Paddle: 6 wide at y=30, 5-bit x (clamped), moves ≤1 cell/frame from two input
  bits (left, right; both pressed = no move).
- Lives: 3 (2-bit). Flags: win, lose. Input: reset.
- You define the exact bounce rules and edge cases (corners, simultaneous
  wall+brick hits, paddle edges). Prefer rules that keep the circuit small.
  After freeze, changes only via a dated DECISIONS.md entry with the reason.

## Milestones — each gate must pass before moving on
- **M0 Setup:** repo, PROGRESS.md, DECISIONS.md, `tests/run_all.py` (plain
  asserts, no framework). Record environment: python, numpy, yosys, numba, GPU, cores.
- **M1 Oracle:** `oracle.py` — pure `step(state, inputs) -> state` over the
  bit-level state. SPEC.md frozen. Tests: hand scenarios (wall bounce, corner,
  brick clear, paddle hit, ball loss, win, reset). `play_oracle.py` curses
  frontend to sanity-check the game is correct and playable.
- **M2 Simulator:** `sim.py` (numpy). Tests: lone-ball flight, two-ball
  collision, reflection, reversibility (N steps forward, N inverse → identical),
  ball count conserved without sources/sinks.
- **M3 Tiles:** `tiles.py` — each tile = bit pattern + port metadata (position,
  phase, latency). Every tile tested in isolation for every input combination:
  correct outputs, correct timing, no stray balls left inside. Write a tile
  gallery image to `docs/tiles.ppm` (PPM via numpy is fine).
- **M4 Netlist:** `hw/game.v` + synthesis script → `build/netlist.json`;
  `netsim.py` evaluates it. Test: netsim == oracle bit-exact (exhaustive where
  feasible + ≥100k random state/input pairs). Record gate count.
- **M5 Layouter on small circuits:** `layout.py` — netlist → placed tiles +
  routed wires → full BBMCA grid + port map. Prove it on a 4-bit counter, then a
  4-bit accumulator. Test: BBMCA == netsim for many frames. Also build
  `debug.py` now: given a failing differential run, find the first diverging
  frame and state bit, and dump the grid region along that signal's path.
- **M6 Full game layout:** lay out the full netlist. Test: BBMCA == oracle
  bit-exact, ≥1000 frames × ≥20 random input sequences. Record grid size, P, tiles.
- **M7 Playable:** `play.py` curses frontend driving the BBMCA. Anti-cheat test:
  `play.py` must not import oracle/netsim or contain game rules (check imports
  and grep). Measure fps.
- **M8 Performance:** profile first, then optimize in order of expected payoff:
  bit-packing (64 cells/word), skipping blocks that can't change (static walls),
  numba/GPU if available, layout compaction. Re-run all differential tests after
  every optimization. Stop at target, or document diminishing returns.
- **M9 Finish:** README with measured numbers, a layout image, and the final report.

## Each session
1. Read PROGRESS.md and DECISIONS.md, `git log --oneline -20`, then run
   `tests/run_all.py` to see the real state. Trust the tests over the notes.
2. Take the next unfinished item of the current milestone. Small steps; run the
   relevant test after each change.
3. When a gate passes: commit, mark it done in PROGRESS.md with the exact
   command and its result.
4. Before stopping or when context gets long: update PROGRESS.md (current
   milestone, in-progress work, next concrete step, open problems, dead ends
   tried) and commit. If tests are red, prefix the commit `WIP:` and say so.
5. Push after every commit (`git push`). If a push fails, note it in
   PROGRESS.md and keep working; retry on the next commit.

## Rules
- Never weaken, skip, or delete a test to make progress. If a test is wrong,
  fix it and note why in PROGRESS.md.
- No faking: `play.py` never calls the oracle or netsim, and nothing replays
  precomputed frames. The BBMCA grid is the only thing computing gameplay.
- Measured numbers only. If something doesn't work, say so plainly in PROGRESS.md.
- Stuck rule: after 3 failed approaches to the same problem, write a short
  blocker analysis in PROGRESS.md, then try in order: (a) build a smaller test
  circuit that isolates the problem, (b) bigger tiles with more padding/slack,
  (c) simplify the game rules (recorded in DECISIONS.md).
- Don't ask the user questions. Choose the simplest reasonable option, record
  it in DECISIONS.md, and keep going. Stop only for missing credentials/sudo,
  or for actions outside the project dir.
- Keep it small: few files, no frameworks, no single-use abstractions. Long
  simulation runs get timeouts and write results to `build/`.

## Final report (at M9)
In README: what works, how to run, measured fps, grid size, P, gate and tile
counts, test summary, known limitations, and what you'd do next.
