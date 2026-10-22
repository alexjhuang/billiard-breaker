# Progress

Source of truth between sessions. Trust `python3 tests/run_all.py` over this file.

## Status
- [x] M0 setup — env: Python 3.12.3 stdlib only (see DECISIONS.md), 192 cores,
      no GPU, no numpy/numba/yosys. Disk `/` is ~100% full (shared machine; not
      ours) — keep outputs small.
- [x] M1 oracle — `python3 tests/run_all.py` 8 passed. SPEC.md frozen. Tracking-bot
      autoplay wins in 903 frames (game is winnable). `play_oracle.py` smoke-tested in a pty.
- [x] M2 simulator — sim.py big-int Margolus BBM; 15 tests pass (cross-checked vs a
      set-based reference, reversible, conserving, dimer mirrors all 8 cases, head-on
      gate). Speed: ~1.4 Gcell-steps/s (2000x2000: 2.95 ms/step).
- [x] M3 tiles — as parametric primitives in layout.py (DECISIONS.md), each tested
      in the CA; a full gate slot for all inputs, both orientations (tests/test_layout.py).
      Gallery: docs/slot.png, docs/frame.png.
- [x] M4 netlist — hw/logic.py DAG, hw/game.py (frame step as logic), hw/lower.py ->
      switch ops, netsim.py. Both DAG eval and gate-level netsim == oracle on random
      states (tests/test_netlist.py). Stats: 740 gates, 298 power, peak 105 live, ORX up to 32.
- [x] M5 layouter on small circuits — plan.py (slots, lines, copies, lifts),
      compile.py (bars + feeds, frame runner), debug.py (first wrong lane per slot).
      2-bit counter (12 frames) and 4-bit accumulator (20 frames): CA == netsim.
- [x] M6 full game layout — game_machine.py compiles in ~2s: 1136 slots, U=45956,
      P=82308, 326k bars, 1355 feeds/frame. `python3 difftest.py 1000 20`: 20/20
      sequences bit-exact vs oracle for 1000 frames (state + win/lose; log in
      build/difftest_1000x20.log, 0.5 s/frame in C). Those runs never reach a win
      (random resets); `python3 difftest.py 1500 2 1000` (pure bot, seeds 1000/1001):
      both bit-exact for 1500 frames, each including a full win (build/difftest_win.log).
- [x] M7 playable — play.py (curses) drives the C sim from build/hardware.pkl;
      tests/test_play.py: no logic modules imported, frames == oracle, pty smoke run.
      ~2.8 fps measured (with background load).
- [x] M8 performance — 488 gates, 627 slots, P=43456, U=25900. play.py (16 tile
      threads): 15.9-18.2 fps measured (load avg 51-59), 1 thread 6.8 fps.
      Re-verified after every change (see M8 log).
- [x] M9 finish — README.md with measured numbers, layout images, final report.

## Current
Browser frontend done (2026-10-21): `python3 web.py`, see README "In a browser";
tests/test_web.py (frames == oracle, pictures == a replay of the machine).
All milestones done (2026-10-21); README.md has the final report. Next ideas:
README "What I'd do next". Slot geometry is documented in compile.py.
Lesson kept: a bump's top bar sits ~2 columns left of its u; keep vertical
traffic >=4 columns away from bump zones.
M8 log (newest first):
- C tiles cut by v (across the band) instead of u: critical path 1.32M -> 1.03M
  particle-steps/frame; private bitmaps pre-touched (warm-up page faults gone).
  play.py: 16 threads, epoch 32. Verified: tests 37/37, difftest 1000x20
  (threads 4) and 1500x2 with wins (threads 16, as play.py); logs build/*vcut*.
- ROW 32 -> 28 (ACT_C 10) and role swap for single-ctrl keep-A ops when the
  ctrl is used once and data sits above: 627 slots, P=43456; verified
  difftest 1000x20 and 1500x2 wins (threads 4).
- C: overlapping-tile threads (world_run_tiles): each epoch of 64 steps the
  particles are split by position into equal-count ranges; each thread runs
  its range plus a 2D+4 ghost margin on a private bitmap and keeps only its
  range (exact by the light-cone argument). Bug found by difftest at scale and
  fixed: tile windows hanging over the ring ends dropped particles. play.py
  uses 8 threads: ~12 fps (1 thread ~6). Also tried and dropped: a per-step
  two-phase threaded step (barriers cost more than the 4 us of work per step).
- plan: symmetric AND ops swap roles instead of copying (78 -> 72 copies).
- early lifts (next-state lanes lifted as soon as final, in op slots' lift zone):
  611 slots, P=44812, verified 300x8 + 1500x2 wins; ~6 fps (load avg ~69).
- logic: freeze folded into controls + switch-tree decoder: 740 -> 488 gates,
  799 slots, P=57848, 5.4 fps; verified 300x8 + 1500x2 wins.
- C step: per-particle division removed (~17 ns/particle-step); tiled and
  u-major bitmaps measured slower for the game; parallel strips: dead end
  (DECISIONS.md).
Older: M8 performance. play.py runs at ~2.8 fps (C sim, 218 moving particles,
~5 us/step, P=82308). Plan: (1) static checker (layout records ball path
segments; flag unintended bar hits/crossings) so layout changes can be checked
without full CA runs; (2) "lazy rows": lanes keep their own row (any value
= rho0 mod 32), catch up only when an op needs them -> no idle bumps, frame
delay D = 32 x critical path instead of 32 x #slots; (3) pack ops at different
row levels into the same slot (rows differing by 32k never interact) with
per-op power/split columns -> U ~10x smaller. Expected P ~10k -> 15+ fps.
Earlier: C simulator (M8 pulled forward) so difftest.py 1000 20 is feasible,
then M7 play.py. Bug found in M6 (fixed): a kept B's landing bar sat in the
messenger column (splitter moved to u0+24). Old notes: Game plan today: 1136 slots, 189 lines, P ~ 80k steps;
python sparse sim ~0.4 s/frame for an 86-slot circuit, so the game needs the
C simulator (M8) before 1000-frame tests are practical; start M6 with
debug.first_fault on single frames. Bugs found in M5 (fixed): all ext inputs
shared one power column (balls merged); drops in one column hit each other's
bars (now lowest-first, 3 columns apart, slots widen); copy power lane was
placed relative to the copy's ctrl instead of the original op's.
Key constraint (still true): a messenger only
falls, so every op's data lane must sit *below* all its control lanes; values
therefore drift down the band during a frame, and next-state lanes must be
lifted back up to the state lines at frame end ("lift" = up-jog + down-jog,
+40n line for 32m delay with 16m - 20n >= 5; the lane then skips m-1 idle
bumps). Upward messengers don't work: their timing depends on distance.

## Verified formulas (sim-checked 2026-10-21; being moved into physics.py + tests)
Ball = (dir, s, p, q): lead at (p + dx*t, q + dy*t), trailer s cells behind.
- Mirror, H-bar rows {Y,Y+1}: from above (dy=+1): q' = 2Y-1-q; from below
  (dy=-1): q' = 2Y+3-q; dy flips, p' = p - s*dx. V-bar cols {X,X+1}: from left
  (dx=+1): p' = 2X-1-p; from right: p' = 2X+3-p; dx flips, q' = q - s*dy.
  (120/120 random cases, s in {2,6}.)
- Collision of perpendicular A,B sharing velocity component c (sign sig):
  g = sig*(phase_c(B) - phase_c(A)). g==0: clean, each phase_c -= sig*s_partner.
  g in {-s_A, s_B-s_A, s_B}: shatter. Else: pass. (298/298, s in {2,4,6,10}.)
- Bar end (long H-bar, left end xe, SE ball crossing row Y at x_cross):
  s=2: pass x_cross<=xe-3, shatter xe-1..xe+1, reflect >=xe+3.
  s=6: shatter xe-1..xe+5, reflect >= xe+7.
- Up-jog of height h changes a SE lane's row by -2h(+mirror consts); down-jog: 0.
Plan being designed: diagonal-cylinder world (u periodic, so state lanes just
wrap around), sparse simulator, resting rows on a 32-lattice, messengers at
rows = 16 mod 32 so every unintended crossing has |g|>=16.

## Open problems
- (solved) head-on gates send pass-through outputs back up input pipes; the
  perpendicular switch gate avoids this.
- Performance: layouts will be ~10^7-10^8 cells but sparse (~10^3 balls);
  a sparse/C simulator will be needed in M8.

## Dead ends
- Single-particle balls: no 90° static mirror exists (brute force, see DECISIONS.md).
- Perpendicular dimer collisions: always shatter the dimers.
