"""Lower a hw.logic DAG to switch gates (the only gate the billiard world has).

A switch op reads one or more control signals (mutually exclusive if more than
one) and switches a data signal, or a fresh power ball if data is None:
    A = data & any(ctrls),  B = data & ~any(ctrls)
Controls survive (they can be read again); data is used up. Pass 1 picks a
recipe per node, pass 2 emits ops in order, copying a signal whenever it is
about to be used up but is still wanted later.
"""
from collections import namedtuple
from hw.logic import ZERO, ONE, NOT

Op = namedtuple('Op', 'ctrls data keep a b')  # keep in 'A', 'B', 'AB'; a/b: new signal ids or None


class Netlist:
    def __init__(self):
        self.ops, self.inputs, self.outputs, self.n = [], {}, {}, 0

    def sw(self, ctrls, data, keep):
        a = b = None
        if 'A' in keep:
            self.n += 1
            a = self.n
        if 'B' in keep:
            self.n += 1
            b = self.n
        self.ops.append(Op(tuple(ctrls), data, keep, a, b))
        return a, b


def topo(outputs):
    order, seen = [], set()
    for name in sorted(outputs):
        stack = [(outputs[name], False)]
        while stack:
            node, done = stack.pop()
            if done:
                order.append(node)
            elif node not in seen:
                seen.add(node)
                stack.append((node, True))
                stack.extend((c, False) for c in reversed(node.args))
    return order


def lower(outputs):
    order = topo(outputs)
    nodes = set(order)
    assert ZERO not in nodes and ONE not in nodes, 'constant reached the netlist'

    # pass 1: recipes. and-nodes become (ctrl, data, polarity); twins share an op
    recipe, ctrl_uses, data_uses = {}, {}, {}

    def cu(e, k=1):
        ctrl_uses[e] = ctrl_uses.get(e, 0) + k

    def du(e):
        data_uses[e] = data_uses.get(e, 0) + 1

    for node in order:
        if node.op != 'and':
            continue
        x, y = node.args
        if (x.op == 'not') != (y.op == 'not'):
            n, d = (x, y) if x.op == 'not' else (y, x)
            recipe[node] = (n.args[0], d, 'B')
        elif x.op == 'not':
            recipe[node] = (x.args[0], y, 'B')  # y (a NOT) gets materialised as data
        else:
            from hw.logic import AND
            if AND(x, NOT(y)) in nodes:
                recipe[node] = (y, x, 'A')
            elif AND(y, NOT(x)) in nodes:
                recipe[node] = (x, y, 'A')
            else:
                recipe[node] = None  # decide data/ctrl in pass 2
    twin_of = {}
    for node, r in recipe.items():
        if r and r[2] == 'A':
            for other, r2 in recipe.items():
                if r2 and r2[2] == 'B' and r2[:2] == r[:2]:
                    twin_of[node], twin_of[other] = other, node

    for node in order:
        op = node.op
        if op == 'and':
            r = recipe[node]
            if r is None:
                x, y = node.args
                cu(x)
                du(y)  # provisional; pass 2 may swap roles, counts stay balanced below
            else:
                c, d, pol = r
                if node in twin_of and pol == 'B':
                    continue  # its A twin accounts for the shared op
                cu(c)
                du(d)
        elif op == 'not':
            pass  # counted where it is materialised
        elif op == 'orx':
            for x in node.args:
                cu(x)
        elif op == 'or':
            x, y = node.args
            cu(x, 2)
            du(y)
        elif op == 'xor':
            x, y = node.args
            cu(x, 2)
            cu(y)
            du(y)
    for e in outputs.values():
        du(e)
    # a NOT is materialised iff something uses it as a signal (not just as the
    # negation inside an and-recipe); it reads its child as a control
    for e in set(ctrl_uses) | set(data_uses):
        if e.op == 'not':
            cu(e.args[0])
    remaining = {e: ctrl_uses.get(e, 0) + data_uses.get(e, 0) for e in set(ctrl_uses) | set(data_uses)}

    net, sig = Netlist(), {}

    def need(e):
        if e not in sig:
            assert e.op == 'not', e
            sig[e] = net.sw([ctrl(e.args[0])], None, 'B')[1]
        return sig[e]

    def ctrl(e):
        s = need(e)
        remaining[e] -= 1
        return s

    def data(e):
        s = need(e)
        remaining[e] -= 1
        if remaining[e] > 0:
            return net.sw([s], None, 'A')[0]  # copy; the original lives on as a control
        return s

    done_b = {}
    for node in order:
        op = node.op
        if op == 'var':
            sig[node] = net.inputs[node.name] = net.n = net.n + 1
        elif op == 'and':
            if node in done_b:
                sig[node] = done_b.pop(node)
                continue
            r = recipe[node]
            if r is None:
                x, y = node.args
                c, d = (y, x) if remaining[x] <= remaining[y] else (x, y)  # data: the one wanted least
                a, _ = net.sw([ctrl(c)], data(d), 'A')
                sig[node] = a
                continue
            c, d, pol = r
            if node in twin_of:
                a, b = net.sw([ctrl(c)], data(d), 'AB')
                mine, other = (a, b) if pol == 'A' else (b, a)
                sig[node] = mine
                done_b[twin_of[node]] = other
            else:
                out = net.sw([ctrl(c)], data(d), pol)
                sig[node] = out[0] if pol == 'A' else out[1]
        elif op == 'orx':
            sig[node] = net.sw([ctrl(x) for x in node.args], None, 'A')[0]
        elif op == 'or':
            x, y = node.args
            t = net.sw([ctrl(x)], data(y), 'B')[1]
            sig[node] = net.sw([ctrl(x), t], None, 'A')[0]
        elif op == 'xor':
            x, y = node.args
            ny = net.sw([ctrl(y)], None, 'B')[1]
            t1 = net.sw([ctrl(x)], ny, 'A')[0]       # x & ~y
            t2 = net.sw([ctrl(x)], data(y), 'B')[1]  # y & ~x
            sig[node] = net.sw([t1, t2], None, 'A')[0]
    for name in sorted(outputs):
        net.outputs[name] = data(outputs[name])
    assert all(v == 0 for v in remaining.values()), [k for k, v in remaining.items() if v]
    return net


def fan_out(net, max_reads=4):
    """Bound how often any one lane is read: a control read costs the lane a
    level, so a signal read k times serialises k ops. Signals used more than
    max_reads times get a balanced tree of copies right after they are made,
    and their uses are spread over the leaves. Returns a new Netlist."""
    uses = {}
    for i, op in enumerate(net.ops):
        for s in op.ctrls:
            uses.setdefault(s, []).append(i)
        if op.data is not None:
            uses.setdefault(op.data, []).append(i)
    for name, s in net.outputs.items():
        uses.setdefault(s, []).append(len(net.ops))
    busy = {s: u for s, u in uses.items() if len(u) > max_reads}
    out = Netlist()
    out.inputs, out.n = dict(net.inputs), net.n
    rename = {}  # (op index, old signal) -> leaf signal

    def tree(s):
        """Copy ops making leaves for s; returns leaf signals (s itself is one)."""
        k = len(busy[s])
        nleaves = -(-k // max_reads)
        leaves = [s]
        while len(leaves) < nleaves:  # every leaf so far makes one copy: breadth first
            grown = []
            for leaf in leaves:
                if len(leaves) + len(grown) >= nleaves:
                    break
                grown.append(out.sw([leaf], None, 'A')[0])
            leaves += grown
        # leaf j serves the uses in its share; copy ops already read some leaves once
        assign, per = {}, -(-k // len(leaves))
        for j, i in enumerate(busy[s]):
            assign[i] = leaves[min(j // per, len(leaves) - 1)]
        return assign

    def made(s):
        if s in busy:
            for i, leaf in tree(s).items():
                rename[i, s] = leaf

    for s in net.inputs.values():
        made(s)
    for i, op in enumerate(net.ops):
        ctrls = tuple(rename.get((i, c), c) for c in op.ctrls)
        data = rename.get((i, op.data), op.data) if op.data is not None else None
        out.ops.append(Op(ctrls, data, op.keep, op.a, op.b))
        for s in (op.a, op.b):
            if s is not None:
                made(s)
    for name, s in net.outputs.items():
        out.outputs[name] = rename.get((len(net.ops), s), s)
    return out


def depth(net):
    """Critical path in levels: an op needs all inputs ready; a read control is
    ready again one level later, outputs one level later."""
    level = {s: 0 for s in net.inputs.values()}
    for op in net.ops:
        ins = list(op.ctrls) + ([op.data] if op.data else [])
        l = max(level[s] for s in ins)
        for s in (op.a, op.b):
            if s is not None:
                level[s] = l + 1
        for c in op.ctrls:
            level[c] = l + 1
    return max(level[s] for s in net.outputs.values())
