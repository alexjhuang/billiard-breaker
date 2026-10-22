"""Space-time schedule: every op gets a slot (where along the loop) and a
level (when: lanes rest at row R0 - 32*level). Replaces plan.py's one-op-per-
slot, everyone-in-step schedule.

Rules the geometry relies on (see compile2.py):
- an op at level l in slot s needs all its input lanes resting at level l
  when they enter s; its outputs (and surviving controls) leave at level l+1;
- a lane that is not used in a slot may take one catch-up bump there (+1 level);
- two ops in one slot must have different levels (rows 32k apart never meet);
- an op's data/power lane is strictly below its controls (messengers fall);
- lines are numbered downward and a lane owns its line from birth to death;
- next-state lanes end on their state lines, all at the final level.
An up-move of n lines costs m levels with 16m - 20n >= 5; a down-move 1.
"""
from collections import namedtuple
from hw.lower import Op

OpAt = namedtuple('OpAt', 'op level ctrl_lines data_line power_line out_lines land drops')
Move = namedtuple('Move', 'sig a b m')  # line a -> b, costs m levels


def up_cost(n):
    m = 1
    while 16 * m - 20 * n < 5:
        m += 1
    return m


class Lane:
    __slots__ = ('sig', 'line', 'level', 'born', 'last', 'bumps', 'died')

    def __init__(self, sig, line, level, born):
        self.sig, self.line, self.level, self.born, self.last = sig, line, level, born, born
        self.bumps, self.died = [], None  # slots where it catches up; slot it is dropped/consumed


class Slot:
    def __init__(self):
        self.ops = []        # OpAt
        self.levels = set()  # levels taken by ops
        self.moves = []      # Move
        self.inputs = []     # (name, sig, line) external inputs arriving here
        self.drop_extra = False


class Plan2:
    MAX_OPS = 6  # per slot

    def __init__(self, net, state, ext=(), extra_out=()):
        self.net, self.state, self.ext, self.extra = net, dict(state), list(ext), list(extra_out)
        self.lanes, self.slots, self.n = {}, [], net.n
        self.uses = {}
        for op in net.ops:
            for s in op.ctrls + ((op.data,) if op.data else ()):
                self.uses[s] = self.uses.get(s, 0) + 1
        for name, s in net.outputs.items():
            self.uses[s] = self.uses.get(s, 0) + 1
        self.state_line = {name: i for i, name in enumerate(sorted(state))}
        self.ext_line = {name: len(state) + i for i, name in enumerate(self.ext)}
        self.reserved = set(self.state_line.values()) | set(self.ext_line.values())
        self.line_use = {}  # line -> list of [from_slot, to_slot or None]
        self.build()

    # --- lines ---------------------------------------------------------------
    def line_free(self, line, s0, s1=None):
        for a, b in self.line_use.get(line, ()):
            if (b is None or b >= s0) and (s1 is None or a <= s1):
                return False
        return True

    def take_line(self, line, s0):
        self.line_use.setdefault(line, []).append([s0, None])

    def release_line(self, line, s1):
        for iv in self.line_use[line]:
            if iv[1] is None:
                iv[1] = s1
                return
        raise AssertionError('line not held', line)

    def free_below(self, line, s0, avoid=()):
        i = line + 1
        while i in self.reserved or i in avoid or not self.line_free(i, s0):
            i += 1
        return i

    # --- slots ---------------------------------------------------------------
    def slot(self, s):
        while len(self.slots) <= s:
            self.slots.append(Slot())
        return self.slots[s]

    def new_lane(self, sig, line, level, s):
        lane = Lane(sig, line, level, s)
        self.lanes[sig] = lane
        self.take_line(line, s)
        return lane

    def kill(self, lane, s):
        lane.died = s
        self.release_line(lane.line, s)

    def catch_up(self, lane, level, s):
        """Bump `lane` (idle) in the slots just before s until it reaches level."""
        need = level - lane.level
        free = [t for t in range(lane.last + 1, s) if t not in lane.bumps]
        assert len(free) >= need, ('no room to catch up', lane.sig, need, free)
        lane.bumps += free[len(free) - need:] if need else []
        lane.level = level

    def place(self, inputs, s_min=0, l_min=0):
        """Earliest (slot, level) where all input lanes can be caught up."""
        s = max([s_min] + [l.last + 1 for l in inputs])
        level = max([l_min] + [l.level for l in inputs])
        while True:
            ok = all(level - l.level <= len([t for t in range(l.last + 1, s) if t not in l.bumps])
                     for l in inputs)
            sl = self.slot(s)
            if ok and level not in sl.levels and len(sl.ops) < self.MAX_OPS:
                return s, level
            s += 1

    # --- schedule ------------------------------------------------------------
    def build(self):
        net = self.net
        for name, line in self.state_line.items():
            self.new_lane(net.inputs[name], line, 0, 0)
        for k, name in enumerate(self.ext):
            self.slot(k).inputs.append((name, net.inputs[name], self.ext_line[name]))
            self.new_lane(net.inputs[name], self.ext_line[name], 0, k)
        self.slot(0).drop_extra = True
        for op in net.ops:
            self.do_op(op)
        self.finish()

    def do_op(self, op, below=-1):
        ctrls = [self.lanes[c] for c in dict.fromkeys(op.ctrls)]
        lo = max(l.line for l in ctrls)
        if op.data is not None and self.lanes[op.data].line <= lo:
            copy = Op((op.data,), None, 'A', self.new_sig(), None)
            self.uses[copy.a] = 1
            self.do_op(copy, below=lo)
            op = op._replace(data=copy.a)
        data = self.lanes[op.data] if op.data is not None else None
        lo = max(lo, below)
        inputs = ctrls + ([data] if data else [])
        s, level = self.place(inputs)
        for l in inputs:
            self.catch_up(l, level, s)
        sl = self.slot(s)
        ctrl_lines = {l.sig: l.line for l in ctrls}
        power_line = None
        if data is None:
            line = power_line = self.free_below(lo, s)
        else:
            line = data.line
        out, land = {}, None
        if op.keep == 'AB':
            bl = self.free_below(line, s, avoid=[line])
            out, land = {op.a: line, op.b: bl}, (op.b, bl)
        else:
            out = {op.a if op.keep == 'A' else op.b: line}
        drops = []
        for c in op.ctrls:
            self.uses[c] -= 1
        if data:
            self.uses[data.sig] -= 1
        for l in ctrls:
            if self.uses[l.sig] == 0:
                drops.append(l.sig)
                self.kill(l, s)
            else:
                l.level, l.last = level + 1, s
        if data:
            data.died = s  # its line passes to the output
        sl.ops.append(OpAt(op, level, ctrl_lines, None if data is None else line, power_line, out, land, drops))
        sl.levels.add(level)
        for sig, ln in out.items():
            if data is not None and ln == line:
                self.lanes[sig] = Lane(sig, ln, level + 1, s)  # inherits data's line hold
            else:
                self.new_lane(sig, ln, level + 1, s)

    def new_sig(self):
        self.n += 1
        return self.n

    def finish(self):
        """Lift next-state lanes to their state lines and line everyone up."""
        net = self.net
        target = {net.outputs[o]: self.state_line[i] for i, o in self.state.items()}
        alive = {sig: l for sig, l in self.lanes.items() if l.died is None}
        keep = set(target) | {net.outputs[o] for o in self.extra}
        stray = [s for s in alive if s not in keep]
        assert not stray, ('lanes left over', stray)
        # extra outputs stay where they are unless that is a reserved line
        for o in self.extra:
            l = alive[net.outputs[o]]
            assert l.line not in self.reserved
        # lifts: highest targets first so no lift crosses a lane already lifted
        order = sorted(target, key=lambda sig: target[sig])
        for sig in order:
            l, goal = alive[sig], target[sig]
            if l.line == goal:
                continue
            m = up_cost(l.line - goal) if goal < l.line else 1
            s = l.last + 1
            # the state line must be free from s on; previous lanes there have died
            while not self.line_free(goal, s) or not self.move_ok(l, goal, s, m):
                s += 1
            self.slot(s).moves.append(Move(sig, l.line, goal, m))
            self.release_line(l.line, s)
            self.take_line(goal, s)
            l.line, l.level, l.last = goal, l.level + m, s
        final = max(l.level for l in alive.values())
        end = max([l.last for l in alive.values()] + [len(self.slots) - 1])
        for l in alive.values():
            free = [t for t in range(l.last + 1, end + 1) if t not in l.bumps]
            while len(free) < final - l.level:
                end += 1
                free = [t for t in range(l.last + 1, end + 1) if t not in l.bumps]
        for l in alive.values():
            self.catch_up(l, final, end + 1)
        self.slot(end)
        self.final_level = final
        self.final = {sig: (l.line, final) for sig, l in alive.items()}

    def move_ok(self, lane, goal, s, m):
        """A move at slot s crosses lines between; avoid crossing another move's span there."""
        lo, hi = sorted((lane.line, goal))
        for mv in self.slot(s).moves:
            a, b = sorted((mv.a, mv.b))
            if not (hi < a or lo > b):
                return False
        return True

    # --- reports -------------------------------------------------------------
    def stats(self):
        nops = sum(len(s.ops) for s in self.slots)
        lines = 1 + max(max(iv for iv in self.line_use), 0)
        return dict(slots=len(self.slots), ops=nops, levels=self.final_level, lines=lines,
                    bumps=sum(len(l.bumps) for l in self.lanes.values()),
                    moves=sum(len(s.moves) for s in self.slots))
