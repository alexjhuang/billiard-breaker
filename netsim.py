"""Gate-level simulator for hw.lower netlists. Enforces the physical rules:
a data signal is used once and is gone afterwards; multi-control reads need
controls that are never on together."""


def run(net, inputs):
    """inputs: dict input name -> 0/1. Returns dict output name -> 0/1."""
    val, dead = {}, set()
    for name, s in net.inputs.items():
        val[s] = inputs[name]
    for op in net.ops:
        for c in op.ctrls:
            assert c in val and c not in dead, ('control not live', op)
        on = [val[c] for c in op.ctrls]
        assert sum(on) <= 1 or len(on) == 1, ('exclusive controls both on', op)
        if op.data is None:
            d = 1
        else:
            assert op.data in val and op.data not in dead, ('data not live', op)
            d = val[op.data]
            dead.add(op.data)
        x = max(on)
        if op.a is not None:
            val[op.a] = d & x
        if op.b is not None:
            val[op.b] = d & (1 - x)
    return {name: val[s] for name, s in net.outputs.items()}


def stats(net):
    """Gate count and the most signals alive at once (lanes the layout needs)."""
    last = {}
    for i, op in enumerate(net.ops):
        for s in op.ctrls + ((op.data,) if op.data else ()):
            last[s] = i
    for s in net.outputs.values():
        last[s] = len(net.ops)
    born = {s: -1 for s in net.inputs.values()}
    for i, op in enumerate(net.ops):
        for s in (op.a, op.b):
            if s is not None:
                born[s] = i
    live, peak = 0, 0
    events = sorted([(b, 1) for s, b in born.items() if s in last] + [(last[s], -1) for s in last if s in born])
    for _, d in events:
        live += d
        peak = max(peak, live)
    return {'gates': len(net.ops), 'peak_live': peak,
            'power': sum(op.data is None for op in net.ops),
            'max_ctrls': max(len(op.ctrls) for op in net.ops)}
