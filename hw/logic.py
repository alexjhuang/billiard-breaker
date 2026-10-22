"""Tiny boolean-expression DAG: the game logic is written with this, then
lowered to switch gates by hw/lower.py. Nodes are hash-consed tuples and
constants are folded as nodes are built."""

_table = {}


class E:
    __slots__ = ('op', 'args', 'name', 'uid')

    def __new__(cls, op, args=(), name=None):
        key = (op, args, name)
        if key not in _table:
            obj = super().__new__(cls)
            obj.op, obj.args, obj.name, obj.uid = op, args, name, len(_table)
            _table[key] = obj
        return _table[key]

    def __and__(a, b):
        return AND(a, b)

    def __or__(a, b):
        return OR(a, b)

    def __xor__(a, b):
        return XOR(a, b)

    def __invert__(a):
        return NOT(a)

    def __repr__(self):
        return self.name if self.op == 'var' else f'{self.op}{self.args}'


def _uid(e):
    return e.uid


ZERO, ONE = E('const', name='0'), E('const', name='1')


def var(name):
    return E('var', name=name)


def NOT(a):
    if a is ZERO:
        return ONE
    if a is ONE:
        return ZERO
    if a.op == 'not':
        return a.args[0]
    return E('not', (a,))


def AND(a, b):
    if ZERO in (a, b):
        return ZERO
    if a is ONE:
        return b
    if b is ONE:
        return a
    if a is b:
        return a
    if NOT(a) is b:
        return ZERO
    return E('and', tuple(sorted((a, b), key=_uid)))


def OR(a, b):
    if ONE in (a, b):
        return ONE
    if a is ZERO:
        return b
    if b is ZERO:
        return a
    if a is b:
        return a
    if NOT(a) is b:
        return ONE
    return E('or', tuple(sorted((a, b), key=_uid)))


def ORX(*xs):
    """OR of signals the caller promises are never true together."""
    xs = tuple(x for x in xs if x is not ZERO)
    if not xs:
        return ZERO
    if len(xs) == 1:
        return xs[0]
    assert ONE not in xs
    return E('orx', xs)


def XOR(a, b):
    if a is ZERO:
        return b
    if b is ZERO:
        return a
    if a is ONE:
        return NOT(b)
    if b is ONE:
        return NOT(a)
    if a is b:
        return ZERO
    return E('xor', tuple(sorted((a, b), key=_uid)))


def MUX(s, a, b):
    """s ? a : b"""
    return ORX(AND(s, a), AND(NOT(s), b))


def evaluate(outputs, env):
    """outputs: dict name -> E; env: dict var name -> 0/1. Checks ORX promises."""
    memo = {ZERO: 0, ONE: 1}

    def ev(e):
        if e in memo:
            return memo[e]
        if e.op == 'var':
            v = env[e.name]
        elif e.op == 'not':
            v = 1 - ev(e.args[0])
        elif e.op == 'and':
            v = ev(e.args[0]) & ev(e.args[1])
        elif e.op == 'or':
            v = ev(e.args[0]) | ev(e.args[1])
        elif e.op == 'xor':
            v = ev(e.args[0]) ^ ev(e.args[1])
        elif e.op == 'orx':
            vals = [ev(x) for x in e.args]
            assert sum(vals) <= 1, ('orx promise broken', e)
            v = max(vals)
        memo[e] = v
        return v

    import sys
    sys.setrecursionlimit(100000)
    return {k: ev(e) for k, e in outputs.items()}
