"""Schedule a switch netlist into slots and give every signal a line.

Lines are numbered downward: line 0 is the top of the band. A slot holds up
to MAXL ops at distinct *levels*, or is an input/lift/idle slot. The physical
rules this enforces: a messenger only falls, so an op's data lane must be
strictly below all of its control lanes (data lanes in the wrong place get
copied below the controls first); and a messenger interacts only with lanes
at its own row level, so ops sharing a slot must use disjoint lanes at
different levels. A level is a lattice offset: a lane with debt d sits d
slots' worth of rows (ROW each) below the slot's lattice row. Lanes are
delayed (one ordinary bump per slot of delay, in shared columns before zone
A) to the level of the op that uses them, and leave it with that debt; idle
lanes with debt skip their idle bump and so catch up one slot per slot. Next-state lanes must end the frame on their
state lines; `lift` slots move lanes there. An up-move of n lines costs ROW*m
rows with ROW*m/2 - 20n >= 5 (debt m - 1); a down-move costs one ordinary slot.
"""
import os
from collections import namedtuple
from hw.lower import Op

Slot = namedtuple('Slot', 'kind live ops op data_line power_line out_lines land drops moves inputs')
Place = namedtuple('Place', 'op level delays data_line power_line out_lines land drops')
# kind: 'input' | 'op' | 'lift' | 'idle'
# live: {sig: (line, debt)} at the start of the slot
# ops: [Place] the ops of an op slot, in column order; op, data_line, power_line,
#   land are the first one's (for old readers), out_lines and drops the union:
#   level: lattice offset the op runs at; delays: {sig: slots} lanes delayed to it;
#   data_line: line of the consumed data (None for power); power_line: line a
#   power ball lands on; out_lines: {sig: line} created; land: (sig, line) for an
#   'AB' op whose B output lands on another line; drops: sigs sent out of the band
# moves: {sig: (from_line, to_line, m)}; inputs: {name: (sig, line)} (input slot)


def up_cost(n, row=28):
    """m (the lift delays the lane m slots' worth of rows) for moving up n lines
    at pitch 40: the up-jog is h = row*m/2 high and the down-jog h - 20n >= 5."""
    m = 1
    while row * m // 2 - 20 * n < 5:
        m += 1
    return m


class Plan:
    MAXL = int(os.environ.get('BB_MAXL', 3))  # ops per slot (levels 0 .. MAXL-1)

    def __init__(self, net, state, ext=(), extra_out=()):
        """state: {input name: output name} pairs that are fed back each frame;
        ext: names of inputs injected by the frontend each frame;
        extra_out: output names read by the frontend (dropped next frame)."""
        self.net, self.state, self.ext, self.extra = net, dict(state), list(ext), list(extra_out)
        self.slots, self.live, self.n = [], {}, net.n
        self.uses = {}
        for op in net.ops:
            for s in op.ctrls + ((op.data,) if op.data else ()):
                self.uses[s] = self.uses.get(s, 0) + 1
        for name, s in net.outputs.items():
            self.uses[s] = self.uses.get(s, 0) + 1
        self.state_line = {name: i for i, name in enumerate(sorted(state))}
        self.ext_line = {name: len(state) + i for i, name in enumerate(self.ext)}
        self.cur = None  # the op slot being filled: dict(places, lanes, lines, levels)
        self.build()

    # --- helpers -----------------------------------------------------------
    def occupied(self):
        return {line for line, _ in self.live.values()}

    def free_below(self, line, taken=()):
        busy = self.occupied() | set(taken) | set(self.ext_line.values())
        i = line + 1
        while i in busy:
            i += 1
        return i

    def new_sig(self):
        self.n += 1
        return self.n

    def emit(self, kind, **kw):
        live = dict(self.live)
        fields = dict(ops=[], op=None, data_line=None, power_line=None, out_lines={}, land=None,
                      drops=[], moves={}, inputs={})
        fields.update(kw)
        self.slots.append(Slot(kind, live, **fields))

    # --- schedule ----------------------------------------------------------
    def build(self):
        net = self.net
        # input slot: state lanes are live on their lines; ext inputs arrive
        for name, line in self.state_line.items():
            self.live[net.inputs[name]] = (line, 0)
        # one input slot per external input (each needs its own power column);
        # the first also drops last frame's extra outputs
        prev_extra = [('extra', name) for name in self.extra]
        for k, name in enumerate(self.ext or [None]):
            inputs = {} if name is None else {name: (net.inputs[name], self.ext_line[name])}
            self.emit('input', inputs=inputs, drops=prev_extra if k == 0 else [])
            for nm, (s, line) in inputs.items():
                self.live[s] = (line, 0)
        self.target = {net.outputs[o]: self.state_line[i] for i, o in self.state.items()}
        # list scheduling: every slot takes the ready ops that fit, longest remaining path first
        prod = {}
        for i, op in enumerate(net.ops):
            for sig in (op.a, op.b):
                if sig is not None:
                    prod[sig] = i
        height = [0] * len(net.ops)
        users = {}
        for i, op in enumerate(net.ops):
            for sig in op.ctrls + ((op.data,) if op.data else ()):
                if sig in prod:
                    users.setdefault(prod[sig], []).append(i)
        for i in reversed(range(len(net.ops))):
            height[i] = 1 + max([height[j] for j in users.get(i, [])], default=0)
        pending = [(height[i], i, op) for i, op in enumerate(net.ops)]
        while pending:
            self.cur = dict(places=[], lanes=set(), lines=set(), levels=set())
            rest = []
            for h, i, op in sorted(pending, key=lambda e: -e[0]):
                if len(self.cur['places']) >= self.MAXL:
                    rest.append((h, i, op))
                    continue
                r = self.try_place(op)
                if r is True:
                    continue
                rest.append((h, i, op if r is False else r))
            pending = rest
            if not self.cur['places']:
                raise RuntimeError(('nothing placeable', pending[:3]))
            self.flush()
        self.finish()

    def try_place(self, op):
        """Place op in the slot being filled if its lanes are live and it fits:
        True if placed, False if not (yet), or the op rewritten to read a copy of
        its data (the copy itself was placed, if it fit)."""
        lanes = op.ctrls + ((op.data,) if op.data else ())
        if any(c not in self.live for c in lanes):
            return False
        if op.data is not None and self.uses[op.data] > 1:
            return False  # the data is used up here: every other use must have been placed first
        lo = max(self.live[c][0] for c in op.ctrls)
        if (op.data is not None and op.keep == 'A' and len(op.ctrls) == 1
                and self.live[op.data][0] < lo and self.uses[op.ctrls[0]] == 1):
            # data & ctrl is symmetric: if the control is not needed afterwards and sits
            # lower, let it be the data instead of copying the data below it
            op = op._replace(ctrls=(op.data,), data=op.ctrls[0])
            lo = self.live[op.ctrls[0]][0]
        if op.data is not None and self.live[op.data][0] <= lo:
            copy = Op((op.data,), None, 'A', self.new_sig(), None)
            self.uses[copy.a] = 1
            if not self.place(copy, below=lo):
                self.uses.pop(copy.a)
                self.n -= 1
                return False
            return op._replace(data=copy.a)   # the op itself next slot, on the copy
        return self.place(op)

    def place(self, op, below=-1):
        """Put op in the slot being filled at the lowest level its lanes allow;
        False if the slot is full, shares a lane or has no free level.
        below: a power lane made here must also land below this line."""
        cur = self.cur
        lanes = set(op.ctrls) | ({op.data} if op.data else set())
        if len(cur['places']) >= self.MAXL or (lanes & cur['lanes']):
            return False
        lo = max(max(self.live[c][0] for c in op.ctrls), below)
        level0 = max(self.live[c][1] for c in lanes)
        level = next((lvl for lvl in range(level0, self.MAXL) if lvl not in cur['levels']), None)
        if level is None:
            return False
        power_line = None
        if op.data is None:
            line = power_line = self.free_below(lo, taken=cur['lines'])
        else:
            line = self.live[op.data][0]
        out, land = {}, None
        if op.keep == 'AB':
            out[op.a] = line
            bl = self.free_below(line, taken=cur['lines'] | {line})
            out[op.b], land = bl, (op.b, bl)
        else:
            out[op.a if op.keep == 'A' else op.b] = line
        drops = []
        for c in op.ctrls:
            self.uses[c] -= 1
        if op.data is not None:
            self.uses[op.data] -= 1
        for c in set(op.ctrls):
            if self.uses[c] == 0:
                drops.append(c)
        delays = {c: level - self.live[c][1] for c in lanes if level - self.live[c][1] > 0}
        cur['places'].append(Place(op, level, delays, None if op.data is None else line, power_line, out, land, drops))
        cur['lanes'] |= lanes | set(out)
        cur['lines'] |= set(out.values()) | ({power_line} if power_line is not None else set())
        cur['levels'].add(level)
        return True

    def flush(self):
        """Close the slot being filled: emit it and apply what it does to the lanes."""
        cur, self.cur = self.cur, None
        if not cur:
            return
        places = cur['places']
        # a lift's climb crosses the slot's lanes where they leave it: safe only at the lattice rows
        moves = {} if any(p.level for p in places) else self.early_lift(cur['lanes'])
        out_lines = {s: l for p in places for s, l in p.out_lines.items()}
        drops = [c for p in places for c in p.drops]
        self.emit('op', ops=places, op=places[0].op, data_line=places[0].data_line, power_line=places[0].power_line,
                  out_lines=out_lines, land=places[0].land, drops=drops, moves=moves)
        for p in places:
            if p.op.data is not None:
                del self.live[p.op.data]
            for c in p.drops:
                del self.live[c]
            for c in set(p.op.ctrls) - set(p.drops):
                self.live[c] = (self.live[c][0], p.level)  # delayed to the op's level
            for s, l in p.out_lines.items():
                self.live[s] = (l, p.level)
        for s, (a, b, m) in moves.items():
            self.live[s] = (b, m - 1)
        for s, (line, debt) in list(self.live.items()):
            if debt and s not in moves and s not in cur['lanes']:
                self.live[s] = (line, debt - 1)

    def early_lift(self, used):
        """Pick one finished next-state lane (no uses left but its output, not
        used in this slot) whose state line is free and lift it in this slot's
        lift zone, so its delay is paid off while later ops run."""
        busy = self.occupied()
        for s, goal in sorted(self.target.items(), key=lambda kv: kv[1]):
            if s not in self.live or self.uses.get(s) != 1 or s in used:
                continue
            line, debt = self.live[s]
            if debt or line == goal or goal in busy:
                continue
            if goal > line:
                continue  # down-moves are free at the end
            return {s: (line, goal, up_cost(line - goal))}
        return {}

    def finish(self):
        """Move next-state lanes onto their state lines and pay off all debt."""
        net = self.net
        target = {net.outputs[o]: self.state_line[i] for i, o in self.state.items()}
        keep = set(target) | {net.outputs[o] for o in self.extra}
        stray = [s for s in self.live if s not in keep]
        assert not stray, ('lanes left over', stray)
        # extra outputs may end anywhere below the state/ext block (goal None)
        reserved = set(self.state_line.values()) | set(self.ext_line.values())
        bottom = max(reserved) + 1
        for o in self.extra:
            target[net.outputs[o]] = None
        for _ in range(10000):
            moves, spans = {}, []
            busy = self.occupied()
            for s, (line, debt) in sorted(self.live.items(), key=lambda kv: kv[1][0]):
                goal = target.get(s)
                if goal is None:  # extra output: just keep it out of the reserved block
                    goal = line
                    while goal < bottom or goal in busy - {line} or goal in target.values():
                        goal += 1
                if debt or line == goal:
                    continue
                if goal in busy or any(g == goal for _, g, _ in moves.values()):
                    continue
                lo, hi = sorted((line, goal))
                if any(not (hi < a or lo > b) for a, b in spans):
                    continue
                m = up_cost(line - goal) if goal < line else 1
                moves[s] = (line, goal, m)
                spans.append((lo, hi))
                busy = busy - {line} | {goal}
            done = all(self.live[s][0] == g for s, g in target.items() if g is not None) and \
                all(self.live[s][0] >= bottom for s in target if target[s] is None)
            if not moves and done and not any(d for _, d in self.live.values()):
                break
            self.emit('lift' if moves else 'idle', moves=moves)
            for s, (line, debt) in list(self.live.items()):
                if s in moves:
                    self.live[s] = (moves[s][1], moves[s][2] - 1)
                elif debt:
                    self.live[s] = (line, debt - 1)
        else:
            raise RuntimeError('lift phase did not converge')
        self.final = dict(self.live)

    # --- reports -----------------------------------------------------------
    def stats(self):
        kinds = {}
        for s in self.slots:
            kinds[s.kind] = kinds.get(s.kind, 0) + 1
        lines = max(l for s in self.slots for l, _ in s.live.values()) + 1
        return dict(slots=len(self.slots), lines=lines, **kinds)


def check(plan):
    """Assert the geometric rules every slot relies on."""
    for j, s in enumerate(plan.slots):
        if s.kind != 'op':
            continue
        lanes, levels = set(), set()
        for p in s.ops:
            lo = max(s.live[c][0] for c in p.op.ctrls)
            line = p.power_line if p.op.data is None else p.data_line
            assert line > lo, (j, 'data/power not below controls', p.op, line, lo)
            if p.land:
                assert p.land[1] > line, (j, 'landing not below data')
            mine = set(p.op.ctrls) | ({p.op.data} if p.op.data else set()) | set(p.out_lines)
            assert not (mine & lanes), (j, 'ops sharing a lane')
            assert p.level not in levels, (j, 'ops sharing a level')
            for c in set(p.op.ctrls) | ({p.op.data} if p.op.data else set()):
                assert s.live[c][1] + p.delays.get(c, 0) == p.level, (j, 'lane not at the op level', c)
            lanes |= mine
            levels.add(p.level)
