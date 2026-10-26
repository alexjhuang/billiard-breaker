"""Turn a Plan into a Layout (bars + scheduled balls) and run it frame by frame.

Slot geometry (W columns per slot, u0 = slot start, rows relative to the slot's
resting row r = R0 - ROW*slot; a lane with debt d rests at r - ROW*d and every
lane leaves a slot at r - ROW - ROW*debt):
  op slot:   u0+1, u0+5, .. power columns, one per power op, 4 apart (a bar end shatters
                  balls within 2 tracks; data lands at row r-ACT_D)
             u0+8 (+4 per extra power op), 8 apart: pre-bump columns, as many as the
                  largest delay in the slot; a lane delayed d slots to its op's level
                  takes an ordinary ROW bump in the first d of them
             uA = next column: zone A: controls retime ACT_C, data ACT_D, idle lanes ROW
             op k (level l, rows rk = r - ROW*l): x+y = 2(uA+10+10k)+1 messenger
                  column at row rk-ACT_C; uA+16+10k splitter (and landing of a
                  kept B, 1.5 columns left)
             uD = uA+19+10(nops-1) zone D: everything back onto the lattice
             uD+10 drops (controls at their last use, leftovers), 4 apart
             end-8 lift zone (next-state lanes lifted early), if any
  lift slot: u0+4 down-moves, u0+16 idle bumps, u0+28 up-moves
  idle slot: W_IDLE wide, u0+4 idle bumps for lanes without debt (a bump needs
             ~8 columns; these slots only exist for debts to be paid)
Lanes with debt (after an up-move) skip their idle bump.
"""
import os
from layout import Layout
from physics import Ball, interaction

W, PITCH, R0 = 40, 40, 0
W_IDLE = int(os.environ.get('BB_WIDLE', 8))  # idle slots: bumps only, 8 columns apart
# Row lattice: every lane leaves a slot ROW rows later. Controls are delayed
# ACT_C into the messenger's row, data ACT_C+2 (one read shifts a lane 6, a
# messenger 2); everything is then put back on the lattice. ROW=28/ACT_C=10
# keeps every unplanned crossing >= 8 rows away from a shatter offset.
ROW, ACT_C = 28, 10
ACT_D = ACT_C + 2
MARGIN_TOP, MARGIN_BOT = 80, 60


class Machine:
    ROW = ROW

    def __init__(self, plan, w=W):
        self.plan, self.W = plan, w
        nslots = len(plan.slots)
        self.u0, self.uend = [0], []
        for j, slot in enumerate(plan.slots):  # each extra op 10 columns, extra power op 4, extra drop 4; lifts a zone
            end = self.u0[-1] + (W_IDLE if slot.kind == 'idle' else w) + 4 * max(0, len(self.drop_list(slot)) - 1) \
                + (10 if slot.kind == 'op' and slot.moves else 0) + self.extra_width(slot)
            self.uend.append(end)
            self.u0.append(end)
        self.U = self.u0[-1]
        self.P = self.U + ROW * nslots
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

    def extra_width(self, slot):
        if slot.kind != 'op':
            return 0
        npow = sum(p.op.data is None for p in slot.ops)
        npre = max([d for p in slot.ops for d in p.delays.values()], default=0)
        return 10 * (len(slot.ops) - 1) + 4 * max(0, npow - 1) + 8 * npre

    def pre_u(self, j, slot):
        """The pre-bump columns, one per slot of delay."""
        npow = sum(p.op.data is None for p in slot.ops)
        npre = max([d for p in slot.ops for d in p.delays.values()], default=0)
        u = self.u0[j] + 8 + 4 * max(0, npow - 1)
        return [u + 8 * i for i in range(npre)]

    def zone_a(self, j, slot):
        pre = self.pre_u(j, slot)
        return pre[-1] + 8 if pre else self.u0[j] + 8 + 4 * max(0, sum(p.op.data is None for p in slot.ops) - 1)

    def drop_u(self, j, slot, line):
        base = self.zone_a(j, slot) + 29 + 10 * (len(slot.ops) - 1) if slot.kind == 'op' else self.u0[j] + 37
        return base + 4 * self.drop_list(slot).index(line)

    def line_c(self, line):
        return self.C0 - PITCH * line

    def lane(self, line, row):
        c = self.line_c(line)
        return Ball('SE', 2, c + row, row)

    def rows(self, j):
        return R0 - ROW * j

    # --- slot kinds --------------------------------------------------------
    def idle_bumps(self, j, slot, skip, u):
        r = self.rows(j)
        for s, (line, debt) in slot.live.items():
            if s in skip or debt:
                continue
            self.L.retime(self.lane(line, r), u, ROW)

    def slot_input(self, j, slot):
        u0, r, L = self.u0[j], self.rows(j), self.L
        for name, (s, line) in slot.inputs.items():
            ln = L.power(u0 + 1, self.line_c(line), r - ACT_D, tag='input:' + name)
            L.retime(ln, u0 + 8, ROW - ACT_D)
        # last frame's extra outputs sit on their final lines; drop them
        self.idle_bumps(j, slot, (), u0 + 8)
        if slot.drops:  # last frame's extra outputs sit on their final lines
            for line in self.drop_list(slot):
                L.drop(self.lane(line, r), self.drop_u(j, slot, line))

    def slot_idle(self, j, slot):
        self.idle_bumps(j, slot, (), self.u0[j] + 4)

    def slot_lift(self, j, slot):
        u0, r, L = self.u0[j], self.rows(j), self.L
        for s, (a, b, m) in slot.moves.items():
            if b > a:  # down
                L.retime(self.lane(a, r), u0 + 4, ROW, self.line_c(b) - self.line_c(a))
        self.idle_bumps(j, slot, slot.moves, u0 + 16)
        for s, (a, b, m) in slot.moves.items():
            if b < a:  # up
                L.retime(self.lane(a, r), u0 + 28, ROW * m, self.line_c(b) - self.line_c(a))

    def slot_op(self, j, slot):
        u0, r, L = self.u0[j], self.rows(j), self.L
        uA, n = self.zone_a(j, slot), len(slot.ops)
        uD = uA + 19 + 10 * (n - 1)
        used, npow = set(), 0

        def lane_at(sig):  # a lane where it rests at the start of this slot
            line, debt = slot.live[sig]
            return self.lane(line, r - ROW * debt)

        pre = self.pre_u(j, slot)

        def delayed(sig, p):  # the lane after its pre-bumps: one ROW bump per slot of delay
            lane = lane_at(sig)
            for i in range(p.delays.get(sig, 0)):
                lane = L.retime(lane, pre[i], ROW)
            return lane

        for k, p in enumerate(slot.ops):
            op, rk = p.op, r - ROW * p.level
            ctrl_lanes = {c: L.retime(delayed(c, p), uA, ACT_C) for c in set(op.ctrls)}
            if op.data is None:
                R = L.power(u0 + 1 + 4 * npow, self.line_c(p.power_line), rk - ACT_D)
                npow += 1
            else:
                R = L.retime(delayed(op.data, p), uA, ACT_D)
            Q = L.messenger(2 * (uA + 10 + 10 * k) + 1, rk - ACT_C)
            # controls are read in order from the top; at most one is present
            shifted = {c: interaction(ctrl_lanes[c], Q)[1] for c in ctrl_lanes}
            Q2 = Q._replace(q=Q.q - 2)
            A = interaction(R, Q2)[1]
            uS = uA + 16 + 10 * k
            if op.keep == 'AB':
                bar = L.split(R, uS, 'A')                # B goes down ...
                B = self.land(R, bar, p.land[1])         # ... and lands on its own line
                L.retime(A, uD, ROW - ACT_D - 6, -6)
                L.retime(B, uD, ROW - ACT_D - 4)
            else:
                L.split(R, uS, op.keep)
                if op.keep == 'A':
                    L.retime(A, uD, ROW - ACT_D - 6, -6)
                else:
                    L.retime(R, uD, ROW - ACT_D)
            for c in set(op.ctrls):
                X2 = shifted[c]
                if c in p.drops:
                    L.drop(X2, self.drop_u(j, slot, slot.live[c][0]))
                else:
                    L.retime(X2, uD, ROW - ACT_C - 6, -6)
            used |= set(op.ctrls) | ({op.data} if op.data else set())
        self.idle_bumps(j, slot, used | set(slot.moves), uA)
        uL = self.uend[j] - 8  # lift zone at the end of the slot
        for s, (a, b, m) in slot.moves.items():
            L.retime(self.lane(a, r), uL, ROW * m, self.line_c(b) - self.line_c(a))

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
