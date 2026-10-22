"""Sparse BBMCA on a diagonal cylinder: the world the game runs in.

Cells (x, y) and (x + U, y + U) are the same cell, so balls moving SE/NW
circle round forever (one lap = U steps). Across the other diagonal the world
is a band: only cells with v2 = x - y in [lo, hi] exist. Anything that steps
outside is deleted (the sinks); the frontend adds balls at the edges (the
sources). Same block rule as sim.py. Only occupied cells are stored.

Static walls are kept apart from moving particles: a block is only updated if
it holds a moving particle, which is exact because every block of a static
wall is already in an 'unchanged' state. If an update moves a wall cell the
wall is broken and `broken` is set (a layout bug, never expected).
"""

ROT180 = {(0, 0): (1, 1), (1, 0): (0, 1), (0, 1): (1, 0), (1, 1): (0, 0)}
DIAG, ANTI = frozenset({(0, 0), (1, 1)}), frozenset({(1, 0), (0, 1)})


class Cyl:
    def __init__(self, U, lo, hi):
        assert U % 2 == 0
        self.U, self.lo, self.hi, self.t = U, lo, hi, 0
        self.walls, self.moving, self.broken = set(), set(), False

    def canon(self, x, y):
        k = (x + y) // (2 * self.U)
        return x - k * self.U, y - k * self.U

    def inside(self, x, y):
        return self.lo <= x - y <= self.hi

    def add(self, cells, wall=False):
        target = self.walls if wall else self.moving
        target.update(self.canon(x, y) for x, y in cells if self.inside(x, y))

    def cells(self):
        return self.walls | self.moving

    def step(self, n=1):
        for _ in range(n):
            self._step(self.t & 1)
            self.t += 1

    def _step(self, o):
        U, walls = self.U, self.walls
        blocks = {}
        for x, y in self.moving:
            rx, ry = (x - o) & 1, (y - o) & 1
            tx, ty = x - rx, y - ry
            k = (tx + ty) // (2 * U)
            blocks.setdefault((tx - k * U, ty - k * U), set()).add((rx, ry))
        moved = set()
        for (tx, ty), s in blocks.items():
            wall_here = set()
            for rx in (0, 1):
                for ry in (0, 1):
                    if self.canon(tx + rx, ty + ry) in walls:
                        wall_here.add((rx, ry))
            full = s | wall_here
            if len(full) == 1:
                new = {ROT180[next(iter(full))]}
            elif full == DIAG:
                new = ANTI
            elif full == ANTI:
                new = DIAG
            else:
                new = full
            if not wall_here <= new:
                self.broken = True
            for rx, ry in new - wall_here:
                x, y = self.canon(tx + rx, ty + ry)
                if self.inside(x, y):
                    moved.add((x, y))
        self.moving = moved
