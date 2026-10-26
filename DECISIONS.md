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

## 2026-10-23 — event-driven simulator: free flight is not stepped
Measured on the game: 9.0 M particle-steps per frame, of which 272 k (3%)
are blocks with more than one thing in them; everything else is a lone
particle moving one cell. csim/bbm.c now keeps every particle as a straight
line and jumps from event to event: the next wall is found by scanning the
three rows in front of the particle in a bitmap (two orientations, rows of
constant x−y for SE/NW movers and of constant x+y for NE/SW), the next
partner through an index keyed by the coordinate two directions keep in
common (x−t, y−t, x+t, y+t, and x−y / x+y for head-on pairs), with the
meeting step solved in closed form. Events are grouped per block and the
Margolus rule is applied as before, so the result is bit for bit the same
(tests/test_fastsim.py vs cyl.py on dense random patterns, difftest vs the
oracle). One core: 63 ms/frame, against 125 ms stepping serially and ~60 ms
on 16 tile threads, so the threaded tiles are gone. The gain is bounded by
the layout, not the engine: ~375 k reschedules per frame at ~165 ns each,
because every lane detours in every slot. web/engine.js is the same engine
in JavaScript (int32 times, re-based every frame): 141 ms/frame under node
against 171 stepping. Positions at any step are extrapolated, so the page's
pictures come free.

## 2026-10-23 — arcade Breakout rules (SPEC.md unfrozen for this)
Added, in the oracle and the circuit alike: brick rows score 7/5/3/1 (the
arcade's red/orange/green/yellow; we have one row of each, not two), a
9-bit score, four ball speeds raised after the 4th and 12th hit and on an
orange or red brick, the paddle shrinking to half after the ball gets
through to the ceiling, and a second wall of bricks before the win. Speed is
a cadence (the ball moves on 1/2, 5/8, 3/4 or all frames, spread evenly by
comparing the bit-reversed frame counter with a threshold) because the ball
is one cell and moves one cell per frame: moving two cells would double the
collision logic. Not done: the arcade's angle changes from where the ball
hits the paddle (the ball here is strictly diagonal), its 8 brick rows, and
the paddle re-growing. Circuit: 488 → 776 switch gates, state 51 → 71 bits.

## 2026-10-24 — the machine on the GPU (web/gpu.js, WebGPU)
Why stepping, not events: the event engine is serial by nature (a heap and
an index), and skipping steps on a GPU would need the whole machine to agree
on the next event, but with ~7 events per step somewhere in the band almost
no step is free. So the GPU engine steps the automaton exactly, one workgroup
of 256 threads for the whole machine, two ball slots per thread (up to 512
balls; the game has ~230). The balls' cells live in a 1024-entry hash set in
workgroup memory, rebuilt every step, so the Margolus rule on a ball's block
is three on-chip lookups; the only global memory a step touches is the wall
bitmaps, read when a ball's direction changed, to find the step at which its
front reaches a wall and the wall cells of that block (the same scans as
bbm.c; both bitmaps, 160 MB, are uploaded once). A step is four barriers.
Feeds, reads and the pictures are served in the shader at their steps; a
frame is 35 dispatches of 2048 steps in one command buffer and one readback.
Exact: tests/test_gpu.py runs it against web/engine.js on Chromium's software
WebGPU adapter (SwiftShader), the only WebGPU on this machine: identical
states, flags and recorded balls. Not measured here: speed on a real GPU
(SwiftShader takes ~33 s a frame emulating 256 threads on the CPU). The
expectation is 0.3–1 µs a step (a few barriers, L2-resident wall rows) i.e.
20–70 ms a frame against 274 ms on the CPU engine; the page shows the number.
The worker falls back to engine.js without WebGPU (or with ?cpu in the URL).
Not done: the C engine on threads (tiles across the band with a light-cone
margin, as the old stepper did) would do the same for web.py.

## 2026-10-24 — the event engine on threads: tiles across the band, incremental
world_run_tiles is back, for the event engine: every epoch of D steps the band
is cut (by v, equal particle counts, fixed for the call) into T ranges; tile k
is a private event-driven world sharing the wall bitmaps, holding the balls
within G = 2D + 4 of its range; it keeps what ends inside the range (exact by
the light cone, as the old stepper). Two things made it pay: tiles keep their
own-range balls from epoch to epoch and only swap the ghost zones (re-adding a
ghost lowers the events of the balls it can meet, so nothing is missed), and
the window edges avoid splitting a falling or rising dimer. A tile's broken
flag is trusted only where its window edges cannot have reached.
Measured, 16 threads, epoch 64: play.py --bench 40 goes 8.7 -> 22 frames/s
(112 -> ~45 ms a frame). The ceiling is geometric, not overhead: at any moment
the live balls fill only ~2 200 of the band's 7 541 rows, so a tile's own
range is ~110 rows wide while the margin is 132 on each side; the ghost zones
redo 1.7x the real machine's events (measured: 1.39 M blocks per frame across
tiles against 518 k real), and event density varies 1.4-1.75x between tiles.
Shorter epochs shrink the margin but multiply the barriers (2232 at D = 32).
Not tried: cutting by predicted events instead of ball counts; re-cutting
every epoch with a cell set to find migrated balls.

## 2026-10-24 — GPU scans fetch the block's wall cells in the same round trip
A direction change used to cost two serialized memory latencies on the GPU:
the corridor scan, then four dependent loads for the wall cells of the block
it found. Those cells always lie in the three scanned rows within one word of
the hit, so the scan now fetches the neighbouring words as it goes (six loads
an iteration instead of three, issued together) and the cells come from
registers; only a block across the loop's seam or at the band's edge falls
back to direct loads. Exact (tests/test_gpu.py). Unmeasured on real
hardware: SwiftShader got slower (54 s a frame from 33) because it pays for
every load on the CPU, whereas a GPU pays for latency, which this halves per
event. If a real GPU disagrees, revert this entry's commit.

## 2026-10-25 — first real GPU: 0.7 frames/s on an RTX 5060 Ti, and why
Two serial memory-latency chains the software adapter could not show:
(1) a falling messenger's column has no mirror until the U-turn thousands of
rows down, so its scan walked ~236 words with a dependent global load each
(~150 µs, the whole workgroup waiting at the barrier), ~2 600 times a frame;
(2) thread 0 polled the feed and read schedules in global memory every step.
Now each wall row has a summary word per 32 words (one bit: the word is not
zero; 2.5 MB per orientation), so a scan is the word holding the ball and its
neighbours in one round trip, else summaries then that word: ~3 latencies
for an empty column instead of ~240; and the next feed/read times are cached.
Exact (tests/test_gpu.py). Expected now: ~1 global latency a step (most steps
have a rescan somewhere), so 50-100 ms a frame on that card; the next lever
is issuing the rescan loads in the previous step so they overlap the barriers.

## 2026-10-26 — GPU engine, round three: 2 fps on the 5060 Ti after the summaries
Still 7 µs a step, so the step itself was restructured and instrumented:
- GPU timestamps (timestamp-query): the page now shows the GPU time of a
  frame next to the wall time, so shader cost, host overhead and low clocks
  can be told apart.
- Two barriers a step instead of five: three cell sets rotate (step t reads
  t % 3, writes (t+1) % 3, clears (t+2) % 3), the picture counters are
  storage atomics, feeds and reads are thread 0's work in the first phase and
  a fed ball is scanned by thread 0 itself.
- Prefetch: a ball that turned fetches the words of its next scan in the
  same phase (after the rule), so their latency overlaps the barriers and the
  rescan in the next phase usually touches no memory.
- Workgroup memory is 22.5 KB now, so the 32 KB limit is requested where the
  adapter has it (all desktop GPUs); otherwise 448 slots and 512-entry sets.
- ?spin=N runs N busy workgroups beside the machine: a diagnostic for the
  hypothesis that one workgroup on one of 36 SMs leaves the card at idle
  clocks (a 3-5x factor that would explain much of the gap).
- Found on the way: 9 storage buffers exceed the default 8 per stage; the
  pipeline was invalid and the previous error path did not say so (now
  createComputePipelineAsync). The two summaries share one buffer.
Exact (tests/test_gpu.py; the software adapter is now ~2 min a frame, it
pays for every load). Numbers from the real card decide the next step.

## 2026-10-26 — GPU engine, round four: tiles across the band, CPU by default
Measured on the real card after round three: 653 ms a frame, all of it GPU
time (timestamps), i.e. ~9 µs a step for one workgroup, which no instruction
count explains; the likely causes are a card idling at low clocks for a
single workgroup and poor code for that shape. Also measured here: 71 131 of
the 71 396 steps of a frame have an event somewhere (gaps of 2-4 steps only
246 times), so a GPU engine cannot skip steps; 60 frames/s means 233 ns a
step, out of reach for one workgroup rebuilding a 230-cell set per step.
So the engine is now the C tiles on the GPU: T workgroups (16) of 64
threads, each holding the balls of a range of x-y plus a light-cone margin
(2D + 4 rows), stepping them D (64) steps per dispatch with the rotating cell
sets, and writing back the balls that ended in its range. Every dispatch
re-reads the list the previous one wrote, ranks the balls by x-y (O(N²) over
~230, shared memory) to cut ranges of equal counts, and takes its window; wall
knowledge (steps to the next wall and its cells) travels with the ball, so
ghosts need no rescans. P / D = 1116 dispatches a frame, each with its own
uniform (dynamic offset), one command buffer, no host work in between.
Exact (tests/test_gpu.py, 16 × 64 and 8 × 128). The software adapter is
twice as fast as the one-workgroup version since it uses 16 CPU threads; the
real card's number is pending. ?tiles=N&epoch=D tune it; ?spin=N as before.
The page runs the CPU engine by default (playable everywhere) with a button
to try the GPU engine (?gpu), as asked.

## 2026-10-26 — the frame's tail: not a state-line ordering problem
P = U + 28·slots has a tail of 159 idle slots (15% of P): the last next-state
values are produced at lines 80–183 and every state line is at 0–70, so the
deepest lane climbs ~145 lines, which at 20 rows a line of up-jog against the
28-row lattice is a debt of ~208 slots (up_cost). Tried: assigning state
lines by the depth at which each value is produced (and the reverse): 72 008
and 78 128 steps against 71 396 — the values are all deep, the order does not
matter. Shortening P needs the lanes off the lattice (no idle detour per slot)
or several gates a slot; both are the compiler redesign noted under M8.
