"""Schedule a switch netlist into slots and give every signal a line.

Lines are numbered downward: line 0 is the top of the band. One slot holds
one op (or is an input/lift/idle slot). The physical rule this enforces: a
messenger only falls, so an op's data lane must be strictly below all of its
control lanes. Data lanes in the wrong place get copied below the controls
first. Next-state lanes must end the frame on their state lines; `lift`
slots move lanes there. An up-move of n lines costs 32m rows with
16m - 20n >= 5 (the lane then skips m-1 idle bumps: its "debt"); a down-move
costs one ordinary slot.
"""
from collections import namedtuple
from hw.lower import Op

Slot = namedtuple('Slot', 'kind live op data_line power_line out_lines land drops moves inputs')
# kind: 'input' | 'op' | 'lift' | 'idle'
# live: {sig: (line, debt)} at the start of the slot
# op: Op or None; data_line: line of the consumed data (None for power)
# power_line: line a power ball lands on; out_lines: {sig: line} created here
# land: (sig, line) for an 'AB' op whose B output lands on another line
# drops: sigs whose lanes are sent out of the band in this slot
# moves: {sig: (from_line, to_line, m)}; inputs: {name: (sig, line)} (input slot)


def up_cost(n, row=28):
    """m (the lift delays the lane m slots' worth of rows) for moving up n lines
    at pitch 40: the up-jog is h = row*m/2 high and the down-jog h - 20n >= 5."""
    m = 1
    while row * m // 2 - 20 * n < 5:
        m += 1
    return m


class Plan:
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
        fields = dict(op=None, data_line=None, power_line=None, out_lines={}, land=None,
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
        self.pending_lift = None
        for op in net.ops:
            self.do_op(op)
        self.finish()

    def do_op(self, op, below=-1):
        """below: a power lane made here must also land below this line."""
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
            self.do_op(copy, below=lo)
            op = op._replace(data=copy.a)
        lo = max(lo, below)
        for c in op.ctrls + ((op.data,) if op.data else ()):
            assert c in self.live and self.live[c][1] == 0, ('not live/ready', c, op)
        power_line = None
        if op.data is None:
            line = power_line = self.free_below(lo)
        else:
            line = self.live[op.data][0]
        out, land = {}, None
        if op.keep == 'AB':
            out[op.a] = line
            bl = self.free_below(line, taken=[line])
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
        self.emit('op', op=op, data_line=None if op.data is None else line, power_line=power_line,
                  out_lines=out, land=land, drops=drops, moves=self.early_lift(op))
        if op.data is not None:
            del self.live[op.data]
        for c in drops:
            del self.live[c]
        for s, l in out.items():
            self.live[s] = (l, 0)
        for s, (a, b, m) in self.slots[-1].moves.items():
            self.live[s] = (b, m - 1)
        for s, (line, debt) in list(self.live.items()):
            if debt and s not in self.slots[-1].moves:
                self.live[s] = (line, debt - 1)

    def early_lift(self, op):
        """Pick one finished next-state lane (no uses left but its output) whose
        state line is free and lift it in this slot's lift zone, so its delay is
        paid off while later ops run."""
        busy = self.occupied()
        for s, goal in sorted(self.target.items(), key=lambda kv: kv[1]):
            if s not in self.live or self.uses.get(s) != 1 or s in op.ctrls or s == op.data:
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
        lo = max(s.live[c][0] for c in s.op.ctrls)
        line = s.power_line if s.op.data is None else s.data_line
        assert line > lo, (j, 'data/power not below controls', s.op, line, lo)
        if s.land:
            assert s.land[1] > line, (j, 'landing not below data')
