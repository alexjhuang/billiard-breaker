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


## 2026-10-21 — netlist from a Python DSL, not Verilog/Yosys
Yosys isn't installable (see first entry). hw/logic.py is a tiny boolean DAG
with constant folding and an ORX ("these are never true together") node,
lowered by hw/lower.py straight to the switch gate, the only gate the
billiard world has. Two implementations (oracle.py and hw/game.py) are
cross-checked bit-exactly, which is what the Verilog was for.

## 2026-10-21 — C simulator via gcc + ctypes
Python simulates a game frame in ~36 s; 1000x20 frames would take days.
csim/bbm.c is the same Margolus rule on the same diagonal cylinder (bitmaps
for walls and moving particles; only moving particles are visited each step),
built with the system gcc on first use and loaded with stdlib ctypes. No new
package dependency. Tested against cyl.py on random dense patterns and whole
machines (tests/test_fastsim.py). ~0.5 s per game frame.

## 2026-10-21 — frontend loads plain hardware data
play.py never sees the netlist: game_machine.py exports build/hardware.pkl
(wall cells, the per-frame ball schedule, readout cells). play.py starts from
an empty world and holds reset for the first frame, so the circuit itself
sets up the game. tests/test_play.py checks the import closure has no logic
modules and that play.py's frames match the oracle.

## 2026-10-21 — dead end: splitting the loop into parallel strips
Tried a threaded C runner that cuts the loop into strips at empty columns,
each strip running D steps behind its left neighbour, lanes handed over at the
cuts and predicted across the wrap seam through an empty zone. It was exact
(matched the serial simulator on whole game frames) but slower: in this
design all lanes move through the slots together as one diagonal wavefront,
so at any moment only ~4 of 31 strips have work, and causality forbids a
strip from running ahead of the lanes it waits for. Removed.

## 2026-10-21 — dead end: space-time packing with rising messengers (plan2)
Idea: lanes keep their own timing (no idle bumps), several ops share a slot
at different row levels, and a transposed copy of the gate recipe (rising
messenger, data above controls) lets values move up as well as down. The
transposed recipe works in the CA (all slot tests pass mirrored), but rising
messengers make the data wait one level per line of distance, multi-control
rising ops need their controls level by line, and without a smarter
register-pressure-aware line allocator the band grew to ~250 lines with
~350 levels: no better than the sequential plan once band height (messenger
lifetimes) is counted. Removed; plan.py + early lifts got most of the gain.

## 2026-10-21 — "tiles" are parametric layout primitives, not fixed bit patterns
GOAL.md M3 asks for tiles.py with fixed bit-pattern tiles. Every placement
here depends on the lane's line and row (bar positions shift with them), so
fixed patterns would mean one tile per (line, row) pair. Instead layout.py has
parametric primitives (bar, turn, jog/retime, power, messenger, split, drop)
built on the verified mirror/collision formulas, each tested in the CA in
isolation, and a whole gate slot tested for every input combination and both
orientations, with no stray balls (tests/test_layout.py). Gallery:
docs/slot.png (one gate) and docs/frame.png (PNG via zlib, no numpy).

## 2026-10-21 — C tiles cut across the band (v), not along the loop (u)
Measured with deterministic counters (particle-steps per frame, and the sum
over epochs of the busiest tile's work, i.e. the critical path), 16 threads,
epoch 32: cutting by u2 gave 15.3M total / 1.32M critical; cutting by v gives
12.9M / 1.03M. The lanes travel along u together, so equal-count u ranges get
narrower than their 68-row ghost margins. v doesn't wrap, which also removed
the ring-distance code. Slow epochs in the first ~8 frames were first-touch
page faults on the private bitmaps; they are now touched at allocation.
Tried, no measurable gain: per-tile hash sets instead of private bitmaps, and
prefetching wall rows ahead of the lanes. Best setting measured: 16 threads,
epoch 32 (24 and 32 threads were not faster on the shared machine).

## 2026-10-21 — browser frontend: stdlib server, server-sent events, ring view
web.py (http.server + threads) runs the machine like play.py and streams
JSON over server-sent events, with keys sent back as POSTs. This avoids a
hand-written WebSocket, and the stdlib has no WebSocket server. The page is
one HTML file with canvas drawing and no libraries. The world is drawn as a
ring because it is a loop (an unrolled strip is 8:1 and would not fit half a
window). A frame is 43k steps in ~1/20 s, so the machine view is
stroboscopic: PICS=3 real pictures per frame at phases that advance a little
each frame (a sweep every 8 s by default). The microscope shows 16-step clips
recorded by world_run_trace (the K steps before a picture run serially and
are recorded; the rest of the frame still runs on the tile threads). Walls
are sent once as sorted, delta-coded 2x2 squares: 41 KB gzipped instead of
1.1 MB. One shared game per server, and the machine pauses with no viewers.

## 2026-10-22 — static site: the machine runs in the browser (web/engine.js)
GitHub Pages serves only files, so the static export ships the hardware as
data (ball schedule + readout cells in machine.json, the wall squares in
walls.bin.gz) plus a JavaScript port of the serial simulator and frame runner,
run in a Web Worker that posts the same events as web.py. It is plain JS
because there is no WebAssembly toolchain here (no clang/emcc). The step uses
one occupancy bitmap (walls | balls; a ball never sits on a wall), so a lone
ball's block takes 4 reads instead of 8: 3.4 -> 6.7 frames/s under Node, about
the C serial speed. Not done yet: threads in the browser need
SharedArrayBuffer (cross-origin isolation, which Pages can only get through a
service worker) and per-worker state for the ghost tiles.
