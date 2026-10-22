"""Find where a compiled machine first goes wrong.

Runs one frame of the CA and, at every slot boundary, compares each live
lane (where the plan says it is) with the value the plan's ops give it.
Reports the first slot with a wrong lane or a stray particle.
"""
from cyl import Cyl


def plan_values(plan, inputs):
    """Evaluate the plan's ops (including inserted copies) like netsim."""
    val = dict()
    for name, s in plan.net.inputs.items():
        if name in inputs:
            val[s] = inputs[name]
    for slot in plan.slots:
        op = slot.op
        if op is None:
            continue
        x = max(val[c] for c in op.ctrls)
        d = 1 if op.data is None else val[op.data]
        if op.a is not None:
            val[op.a] = d & x
        if op.b is not None:
            val[op.b] = d & (1 - x)
    return val


def first_fault(m, state, inputs, frame_inputs=None):
    """state/inputs for frame 0. Returns None if the frame is clean, else a report dict."""
    plan, L = m.plan, m.L
    env = {**state, **inputs}
    val = plan_values(plan, env)
    w = Cyl(m.U, m.lo, m.hi)
    w.add(set().union(*(b.cells() for b in L.bars)), wall=True)
    sched = {}
    for t, b, tag in L.feeds:
        if tag.startswith('input:') and not inputs.get(tag[6:]):
            continue
        sched.setdefault(t, set()).update(b.cells(t))
    look = m.readout_balls()
    for name, bit in state.items():
        if bit:
            lane = look[name]
            t = -(lane.p - lane.q) // 2
            sched.setdefault(t, set()).update(lane.cells(t))
    checks = {}
    for j, slot in enumerate(plan.slots):
        u0, r = m.u0[j], m.rows(j)
        for s, (line, debt) in slot.live.items():
            lane = m.lane(line, r - 32 * debt)
            t = u0 - (lane.p + lane.q) // 2
            checks.setdefault(t, []).append((j, s, line, lane, t))
    w.t = min(min(sched), min(checks))
    end = max(checks)
    while w.t <= end:
        if w.t in sched:
            w.add(sched[w.t])
        for j, s, line, lane, t in checks.get(w.t, ()):
            cells = {w.canon(*c) for c in lane.cells(t)}
            k = len(cells & w.moving)
            want = val.get(s, 0)
            if k != 2 * want:
                return dict(slot=j, kind=plan.slots[j].kind, sig=s, line=line, want=want, got=k,
                            op=plan.slots[j].op, prev=plan.slots[j - 1] if j else None, t=w.t)
        w.step()
        if w.broken:
            return dict(broken=True, t=w.t)
    return None
