# Progress

Source of truth between sessions. Trust `python3 tests/run_all.py` over this file.

## Status
- [x] M0 setup — env: Python 3.12.3 stdlib only (see DECISIONS.md), 192 cores,
      no GPU, no numpy/numba/yosys. Disk `/` is ~100% full (shared machine; not
      ours) — keep outputs small.
- [ ] M1 oracle
- [ ] M2 simulator
- [ ] M3 tiles
- [ ] M4 netlist
- [ ] M5 layouter on small circuits
- [ ] M6 full game layout
- [ ] M7 playable
- [ ] M8 performance
- [ ] M9 finish

## Current
M1 next.

## Open problems
- Head-on interaction gate: pass-through outputs travel back along the other
  input's pipe (reciprocity). Needs a tile design that sends them elsewhere.

## Dead ends
- Single-particle balls: no 90° static mirror exists (brute force, see DECISIONS.md).
- Perpendicular dimer collisions: always shatter the dimers.
