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
- [ ] M3 tiles
- [x] M4 netlist — hw/logic.py DAG, hw/game.py (frame step as logic), hw/lower.py ->
      switch ops, netsim.py. Both DAG eval and gate-level netsim == oracle on random
      states (tests/test_netlist.py). Stats: 740 gates, 298 power, peak 105 live, ORX up to 32.
- [ ] M5 layouter on small circuits
- [ ] M6 full game layout
- [ ] M7 playable
- [ ] M8 performance
- [ ] M9 finish

## Current
M3 nearly done: physics.py (formulas, tests vs CA), cyl.py (sparse cylinder
CA), layout.py primitives (retime, power, messenger, split, drop) and a full
gate slot verified in the CA for all inputs (tests/test_layout.py).
Slot recipe (rows relative to resting row rho; every lane leaves at rho-32):
  power R: inject at u0-4 with SE row rho-14 | zone A (u0+3): X retime 12,
  data R retime 14, idle lanes retime 32 | messenger col x+y=2(u0+13)+1, row
  rho-12 | split R at u0+17 | zone D (u0+21): X' retime 14 dline -6,
  A retime 12 dline -6, B retime 18. Lane pitch 40 (x-y units).
  Lesson: a 32-delay bump's top bar sits ~2 columns left of its u; keep
  vertical traffic >=4 columns away from bump zones.
Next: M5 planner. Key constraint found while designing it: a messenger only
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
