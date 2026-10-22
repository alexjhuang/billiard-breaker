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

## 2026-10-21 — the gate: perpendicular dimer collisions + a "messenger"
Correction to the above: perpendicular dimers *do* collide cleanly at one
exact timing (earlier search filtered those outcomes out by mistake). Measured
(SE ball A, SW ball B; "row" r = y - t, constant while moving; g = r_B - r_A):
- g == 0: clean collision. Both keep direction; A's row drops by s_B, B's by
  s_A (s = dimer spacing). Net effect: each hops to a parallel track.
- g in {-s_A, s_B - s_A, s_B}: shatter (forbidden in layouts).
- any other g: no interaction (safe crossing).
- Mirror bar ends shatter a band of s/2+1 adjacent tracks, so tracks must be
  >= s+4 apart (in x-y) to be split by a bar end. For s=2 lanes that is 6.
Gate ("switch"): control X (s=2), data R (s=2), power messenger Q (s=6).
Q hits X at g=0 -> X always hops by 6 (deterministic: X survives, re-usable);
Q's row drops by 2 iff X. Q then meets R aligned with its dropped row: clean
iff X (else g=+2, a pass). So R hops by 6 iff X, and a bar end splits R into
R&X / R&~X. With R = power this is COPY + NOT. Verified for all 4 inputs.

## 2026-10-21 — floorplan ("conveyor"), in uv = ((x+y)/2, (x-y)/2)
uv turns diagonal motion into axis-aligned beams: SE=right, NE=up, SW=down,
NW=left; bars are 45° mirrors. Signals are lanes moving right. Each gate is a
messenger column: Q enters at the top boundary, falls, reads X, writes R,
U-turns below the last lane, rises and leaves at the top. Unwanted outputs
are bounced up and out the top by the splitter. State returns along the
bottom (below every U-turn, so no vertical traffic crosses it). Every
perpendicular crossing must have a safe g; the layouter checks all of them.

