# Decisions

Dated log of choices made where GOAL.md left room, or deviations from it.

## 2026-10-21 — stdlib only (no numpy, no Yosys)
apt is broken on this machine (`mwinit` unmet dependency; `apt --fix-broken`
would likely remove it — not ours to touch) and pip's configured CodeArtifact
index returns 401. So: Python 3.12 stdlib only. The BBMCA grid is stored as
Python big ints (one bit per cell) and stepped with bitwise ops — same
memory-bound cost class as numpy. Netlist comes from a small Python DSL
(`hw/`), the GOAL's documented Yosys fallback. Revisit if numpy/yosys become
installable (`/opt/nki-venv` has numpy 1.26 if a comparison is ever needed).

## 2026-10-21 — pure BBMCA rule, balls are 2-particle "dimers"
Empirical findings (see tests/test_sim.py once written):
- Static walls exist in the pure rule: a 2x2 square of particles is static iff
  its top-left cell has x+y odd; 2-thick bars of even length with TL x+y odd
  are static.
- A single particle hitting a static wall always bounces back 180°. Brute
  force over all static 5x5 patterns and unions of <=3 squares found no
  90° reflector for single particles.
- A "dimer" — two particles on the same diagonal, 2 cells apart — reflects
  90° cleanly off a 2-thick bar (the lead bounces, collides head-on with the
  trailer, both rotate). So a signal ball = one dimer.
- Two dimers meeting head-on on the same line rotate 90° and leave clean:
  the interaction gate. Dimers meeting at right angles break apart: layouts
  must time crossings so perpendicular balls never meet.
So no second wall plane is needed.
