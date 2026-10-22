"""Turn a Plan into a Layout (bars + scheduled balls) and run it frame by frame.

Slot geometry (W columns per slot, u0 = slot * W, rows relative to the slot's
resting row r = R0 - 32*slot; every lane leaves a slot at r - 32):
  op slot:   u0+1 power column (power data lands at row r-14)
             u0+8 zone A: controls retime 12, data retime 14, idle lanes 32
             x+y = 2(u0+18)+1 messenger column, row r-12
             u0+24 splitter (and landing of a kept B, 1.5 columns left)
             u0+27 zone D: control retime 14/-6, A 12/-6, B 18 (14 if landed)
             u0+37 drops (controls at their last use, leftovers)
  lift slot: u0+4 down-moves (retime 32), u0+16 idle bumps, u0+28 up-moves
  idle slot: u0+16 idle bumps for lanes without debt
Lanes with debt (after an up-move) skip their idle bump.
"""
from layout import Layout
from physics import Ball, interaction

W, PITCH, R0 = 40, 40, 0
MARGIN_TOP, MARGIN_BOT = 80, 60


class Machine:
    def __init__(self, plan, w=W):
        self.plan, self.W = plan, w
        nslots = len(plan.slots)
        self.u0 = [0]
        for slot in plan.slots:  # each extra drop needs its own column
            self.u0.append(self.u0[-1] + w + 4 * max(0, len(self.drop_list(slot)) - 1))
        self.U = self.u0[-1]
        self.P = self.U + 32 * nslots
        nlines = max(l for s in plan.slots for l, _ in s.live.values()) + 2
        self.C0 = 0
        self.lo = self.line_c(nlines) - MARGIN_BOT
        self.hi = self.C0 + MARGIN_TOP
        self.L = Layout(self.U, self.lo, self.hi)
        for j, slot in enumerate(plan.slots):
            getattr(self, 'slot_' + slot.kind)(j, slot)

    def drop_list(self, slot):
        """Lines dropped at the end of a slot, lowest first (a falling ball must
        not meet the drop bar of a lane below it)."""
        if slot.kind == 'input':
            lines = [self.plan.final[self.plan.net.outputs[o]][0] for o in self.plan.extra] if slot.drops else []
        elif slot.kind == 'op':
            lines = [slot.live[c][0] for c in slot.drops]
        else:
            lines = []
        return sorted(lines, reverse=True)

    def drop_u(self, j, slot, line):
        return self.u0[j] + 37 + 4 * self.drop_list(slot).index(line)

    def line_c(self, line):
        return self.C0 - PITCH * line

    def lane(self, line, row):
        c = self.line_c(line)
        return Ball('SE', 2, c + row, row)

    def rows(self, j):
        return R0 - 32 * j

    # --- slot kinds --------------------------------------------------------
    def idle_bumps(self, j, slot, skip, u):
        r = self.rows(j)
        for s, (line, debt) in slot.live.items():
            if s in skip or debt:
                continue
            self.L.retime(self.lane(line, r), u, 32)

    def slot_input(self, j, slot):
        u0, r, L = self.u0[j], self.rows(j), self.L
        for name, (s, line) in slot.inputs.items():
            ln = L.power(u0 + 1, self.line_c(line), r - 14, tag='input:' + name)
            L.retime(ln, u0 + 8, 18)
        # last frame's extra outputs sit on their final lines; drop them
        self.idle_bumps(j, slot, (), u0 + 8)
        if slot.drops:  # last frame's extra outputs sit on their final lines
            for line in self.drop_list(slot):
                L.drop(self.lane(line, r), self.drop_u(j, slot, line))

    def slot_idle(self, j, slot):
        self.idle_bumps(j, slot, (), self.u0[j] + 16)

    def slot_lift(self, j, slot):
        u0, r, L = self.u0[j], self.rows(j), self.L
        for s, (a, b, m) in slot.moves.items():
            if b > a:  # down
                L.retime(self.lane(a, r), u0 + 4, 32, self.line_c(b) - self.line_c(a))
        self.idle_bumps(j, slot, slot.moves, u0 + 16)
        for s, (a, b, m) in slot.moves.items():
            if b < a:  # up
                L.retime(self.lane(a, r), u0 + 28, 32 * m, self.line_c(b) - self.line_c(a))

    def slot_op(self, j, slot):
        u0, r, L, op = self.u0[j], self.rows(j), self.L, slot.op
        ctrl_lanes = {c: L.retime(self.lane(slot.live[c][0], r), u0 + 8, 12) for c in set(op.ctrls)}
        if op.data is None:
            R = L.power(u0 + 1, self.line_c(slot.power_line), r - 14)
        else:
            R = L.retime(self.lane(slot.data_line, r), u0 + 8, 14)
        self.idle_bumps(j, slot, set(op.ctrls) | {op.data}, u0 + 8)
        Q = L.messenger(2 * (u0 + 18) + 1, r - 12)
        # controls are read in order from the top; at most one is present
        shifted = {c: interaction(ctrl_lanes[c], Q)[1] for c in ctrl_lanes}
        Q2 = Q._replace(q=Q.q - 2)
        A = interaction(R, Q2)[1]
        uS = u0 + 24
        if op.keep == 'AB':
            bar = L.split(R, uS, 'A')                # B goes down ...
            B = self.land(R, bar, slot.land[1])      # ... and lands on its own line
            L.retime(A, u0 + 27, 12, -6)
            L.retime(B, u0 + 27, 14)
        else:
            L.split(R, uS, op.keep)
            if op.keep == 'A':
                L.retime(A, u0 + 27, 12, -6)
            else:
                L.retime(R, u0 + 27, 18)
        for c in set(op.ctrls):
            X2 = shifted[c]
            if c in slot.drops:
                L.drop(X2, self.drop_u(j, slot, slot.live[c][0]))
            else:
                L.retime(X2, u0 + 27, 14, -6)

    def land(self, B, bar, line):
        """B (unshifted R track) was turned down by splitter `bar`; catch it on `line`."""
        from physics import reflect
        sw = reflect(B, bar)
        k = sw.p + sw.q
        X = (self.line_c(line) + k - 5) // 2
        y = k - X - 1
        out = self.L.turn(sw, self.L.bar('V', y - 3, y, X))
        assert out.p - out.q == self.line_c(line), out
        return out

    # --- running -----------------------------------------------------------
    def readout_balls(self):
        """Where to look for each state/extra output at the start of a frame (t mod P)."""
        plan, out = self.plan, {}
        for name in plan.state:
            out[name] = self.lane(plan.state_line[name], R0)
        for o in plan.extra:
            out[o] = self.lane(plan.final[plan.net.outputs[o]][0], R0)
        return out

    def simulate(self, init, inputs, nframes, check=True):
        """init: {state name: bit}; inputs: per-frame {ext name: bit}.
        Returns per-frame {name: bit} read at the start of frames 0..nframes."""
        from cyl import Cyl
        L, P = self.L, self.P
        w = Cyl(self.U, self.lo, self.hi)
        w.add(set().union(*(b.cells() for b in L.bars)), wall=True)
        sched = {}
        for n in range(nframes):
            for t, b, tag in L.feeds:
                if tag.startswith('input:') and not inputs[n].get(tag[6:]):
                    continue
                sched.setdefault(t + n * P, set()).update(b.cells(t))
        look = self.readout_balls()
        for name, bit in init.items():
            if bit:
                lane = look[name]
                t = -(lane.p - lane.q) // 2
                sched.setdefault(t, set()).update(lane.cells(t))
        reads = {}
        for n in range(nframes + 1):
            for name, lane in look.items():
                t = -(lane.p - lane.q) // 2
                reads.setdefault(t + n * P, []).append((n, name, {w.canon(*c) for c in lane.cells(t)}))
        out = [dict() for _ in range(nframes + 1)]
        w.t = min(min(sched), min(reads))
        end = max(reads)
        while w.t <= end:
            if w.t in sched:
                w.add(sched[w.t])
            for n, name, cells in reads.get(w.t, ()):
                k = len(cells & w.moving)
                assert k in (0, 2) or not check, ('half a ball', name, n, w.t)
                out[n][name] = int(k == 2)
            w.step()
        assert not w.broken
        return out
