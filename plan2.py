"""Space-time schedule: every op gets a slot (where along the loop) and a level
(when). A lane at level l rests at row R0 - 32*l; its "virtual level" is
l + line (lines are 32 apart in x-y and numbered downward), which is what the
x-phase timing of rising (bottom-fed) messengers sees.

Rules the geometry (compile2.py) relies on:
- top-fed op (falling messenger, data below its controls): all its lanes at
  one level l; bottom-fed op (rising messenger, the mirror image, data above):
  all its lanes at one virtual level. In the op's own frame everything leaves
  one level later;
- an idle lane may take one catch-up bump per slot (+1 level);
- slot 0 only holds the external inputs and drops last frame's extra outputs;
- ops sharing a slot: a top-fed op's level must not be the level of another
  op's activated lanes, a bottom-fed op's virtual level not the virtual level
  of another op's activated lanes (rows/phases 32k apart never meet);
- a lane owns its line from birth to death;
- next-state lanes end on their state lines, all at the final level.
"""
from collections import namedtuple
from hw.lower import Op

OpAt = namedtuple('OpAt', 'op orient level need line out_lines land drops')
# orient 'top' | 'bot'; level: the op's level (top) or virtual level (bot);
# need: {sig: real level when activated} for its input lanes and power lane ('power');
# line: data/power line; out_lines {sig: line}; land (sig, line) for a kept B


class Lane:
    __slots__ = ('sig', 'line', 'level', 'last', 'bumps', 'died')

    def __init__(self, sig, line, level, last):
        self.sig, self.line, self.level, self.last = sig, line, level, last
        self.bumps, self.died = set(), None

    def vlevel(self):
        return self.level + self.line


class Slot:
    def __init__(self):
        self.ops, self.inputs, self.drop_extra = [], [], False
        self.R, self.V = set(), set()             # levels of activated lanes
        self.top_levels, self.bot_levels = set(), set()

    def fits(self, orient, level, real, virt, max_ops):
        if len(self.ops) >= max_ops or real & self.top_levels or virt & self.bot_levels:
            return False
        return level not in (self.R if orient == 'top' else self.V)


class Plan2:
    def __init__(self, net, state, ext=(), extra_out=(), max_ops=6, max_rise=2):
        self.net, self.state, self.ext, self.extra = net, dict(state), list(ext), list(extra_out)
        self.max_ops, self.max_rise = max_ops, max_rise
        self.lanes, self.slots, self.n = {}, [], net.n
        self.outputs = dict(net.outputs)
        self.uses = {}
        for op in net.ops:
            for s in op.ctrls + ((op.data,) if op.data else ()):
                self.uses[s] = self.uses.get(s, 0) + 1
        for s in self.outputs.values():
            self.uses[s] = self.uses.get(s, 0) + 1
        self.state_line = {name: i for i, name in enumerate(sorted(state))}
        self.ext_line = {name: len(state) + i for i, name in enumerate(self.ext)}
        self.reserved = set(self.state_line.values()) | set(self.ext_line.values())
        self.target = {self.outputs[o]: self.state_line[i] for i, o in self.state.items()}
        self.held = {}  # line -> sig
        self.build()

    def slot(self, s):
        while len(self.slots) <= s:
            self.slots.append(Slot())
        return self.slots[s]

    def lane(self, sig, line, level, s):
        assert line not in self.held, ('line taken', line)
        l = self.lanes[sig] = Lane(sig, line, level, s)
        self.held[line] = sig
        return l

    def nearest_free(self, line, step):
        """Closest free line in direction step (lines may go negative: above line 0)."""
        i = line + step
        while i in self.held or i in self.reserved:
            i += step
        return i

    def idle_slots(self, l, s):
        return [t for t in range(max(l.last + 1, 1), s) if t not in l.bumps]

    def new_sig(self):
        self.n += 1
        return self.n

    # --- schedule ------------------------------------------------------------
    def build(self):
        net = self.net
        for name, line in self.state_line.items():
            self.lane(net.inputs[name], line, 0, 0)
        for name in self.ext:
            self.slot(0).inputs.append((name, net.inputs[name], self.ext_line[name]))
            self.lane(net.inputs[name], self.ext_line[name], 0, 0)
        self.slot(0).drop_extra = True
        for op in net.ops:
            self.do_op(op)
        self.finish()

    def do_op(self, op, force_line=None):
        ctrls = [self.lanes[c] for c in dict.fromkeys(op.ctrls)]
        hi, lo = min(l.line for l in ctrls), max(l.line for l in ctrls)
        data = self.lanes[op.data] if op.data is not None else None
        single = len(ctrls) == 1  # a rising messenger needs its controls at one virtual level
        if data is not None and data.line <= lo and (data.line >= hi or hi - data.line > self.max_rise or not single):
            # data among the controls, or so far above them that a rising messenger
            # would make it wait a level per line: copy it below the controls instead
            copy = Op((op.data,), None, 'A', self.new_sig(), None)
            self.uses[copy.a] = 1
            self.do_op(copy, force_line=self.nearest_free(lo, +1))
            op = op._replace(data=copy.a)
            data = self.lanes[op.data]
        if data is not None:
            line = data.line
        elif force_line is not None:
            line = force_line
        else:
            line = self.target_line(op)
            if line is not None and (hi - line > self.max_rise or (line < hi and not single)):
                line = None  # far above: a rising messenger would drag the controls' timing along
            if line is None:
                below, above = self.nearest_free(lo, +1), self.nearest_free(hi, -1)
                line = above if single and hi - above <= self.max_rise and hi - above <= below - lo else below
        orient = 'top' if line > lo else 'bot'
        lanes = ctrls + ([data] if data else [])
        lines = {l.sig: l.line for l in lanes}
        lines['power'] = line
        # earliest slot and level where every lane can be caught up
        if orient == 'top':
            level = max(l.level for l in lanes)
            need = {k: level for k in lines}
        else:
            level = max(l.vlevel() for l in lanes)
            need = {k: level - ln for k, ln in lines.items()}
        if data is not None:
            del need['power'], lines['power']
        s = max(1, max(l.last for l in lanes) + 1)
        while True:
            if all(need[l.sig] - l.level <= len(self.idle_slots(l, s)) for l in lanes):
                real = set(need.values())
                virt = {need[k] + lines[k] for k in need}
                if self.slot(s).fits(orient, level, real, virt, self.max_ops):
                    break
            s += 1
        for l in lanes:
            k = need[l.sig] - l.level
            if k:
                l.bumps.update(self.idle_slots(l, s)[-k:])
            l.level = need[l.sig]
        sl = self.slot(s)
        out, land = {}, None
        if op.keep == 'AB':
            bl = self.nearest_free(line, +1 if orient == 'top' else -1)
            out, land = {op.a: line, op.b: bl}, (op.b, bl)
        else:
            out = {op.a if op.keep == 'A' else op.b: line}
        for c in op.ctrls:
            self.uses[c] -= 1
        if data:
            self.uses[data.sig] -= 1
        drops = []
        for l in ctrls:
            if self.uses[l.sig] == 0:
                drops.append(l.sig)
                l.died = s
                del self.held[l.line]
            else:
                l.level, l.last = need[l.sig] + 1, s
        if data:
            data.died = s
            del self.held[data.line]
        sl.ops.append(OpAt(op, orient, level, need, line, out, land, drops))
        sl.R |= real
        sl.V |= virt
        (sl.top_levels if orient == 'top' else sl.bot_levels).add(level)
        for sig, ln in out.items():
            # in the op's own frame every output leaves one level later
            lv = level + 1 if orient == 'top' else level + 1 - ln
            self.lane(sig, ln, lv, s)

    def target_line(self, op):
        """A power op making a next-state value writes it onto its state line if free."""
        for sig in (op.a, op.b):
            if sig in self.target and self.target[sig] not in self.held:
                return self.target[sig]
        return None

    def finish(self):
        # next-state values not on their state lines: copy them there
        for sig, line in sorted(self.target.items(), key=lambda kv: kv[1]):
            if self.lanes[sig].line == line:
                continue
            assert line not in self.held, ('state line still busy', line)
            new = self.new_sig()
            self.uses[sig] += 0  # the output reference is now the copy's read
            self.uses[new] = 1
            self.do_op(Op((sig,), None, 'A', new, None), force_line=line)
            self.target[new] = self.target.pop(sig)
            for o, s in list(self.outputs.items()):
                if s == sig:
                    self.outputs[o] = new
        alive = {sig: l for sig, l in self.lanes.items() if l.died is None}
        keep = set(self.target) | {self.outputs[o] for o in self.extra}
        stray = [s for s in alive if s not in keep]
        assert not stray, ('lanes left over', stray)
        final = max(l.level for l in alive.values())
        end = max([l.last for l in alive.values()] + [len(self.slots) - 1])
        while any(final - l.level > len(self.idle_slots(l, end + 1)) for l in alive.values()):
            end += 1
        for l in alive.values():
            k = final - l.level
            if k:
                l.bumps.update(self.idle_slots(l, end + 1)[-k:])
            l.level = final
        self.slot(end)
        self.final_level = final
        self.final = {sig: l.line for sig, l in alive.items()}

    def stats(self):
        return dict(slots=len(self.slots), ops=sum(len(s.ops) for s in self.slots),
                    levels=self.final_level,
                    lines=(min(l.line for l in self.lanes.values()), max(l.line for l in self.lanes.values())),
                    bumps=sum(len(l.bumps) for l in self.lanes.values()),
                    max_ops=max(len(s.ops) for s in self.slots), top=sum(o.orient == 'top' for s in self.slots for o in s.ops))
