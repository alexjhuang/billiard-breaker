import random
from oracle import step, INIT, win, lose
from state import State, pack, unpack, NBITS

NONE = (0, 0, 0)
ALL = (1 << 32) - 1


def s(bx, by, dx, dy, px=13, bricks=ALL, lives=3):
    return State(bx, by, dx, dy, px, bricks, lives)


def test_walls_and_corner():
    assert step(s(31, 20, 0, 0), *NONE)[:4] == (30, 21, 1, 0)
    assert step(s(0, 20, 1, 1), *NONE)[:4] == (1, 19, 0, 1)
    assert step(s(5, 0, 0, 1), *NONE)[:4] == (6, 1, 0, 0)
    assert step(s(31, 0, 0, 1), *NONE)[:4] == (30, 1, 1, 0)


def test_brick_clear_and_bounce():
    n = step(s(5, 16, 0, 1), *NONE)  # moves to (6, 15): row 3, col 1 -> brick 25
    assert n[:4] == (6, 15, 0, 0) and n.bricks == ALL & ~(1 << 25)
    n2 = step(n, *NONE)  # bounces back out, no brick there
    assert n2[:4] == (7, 16, 0, 0) and n2.bricks == n.bricks
    gap = s(5, 16, 0, 1, bricks=ALL & ~(1 << 25))
    assert step(gap, *NONE)[:4] == (6, 15, 0, 1)


def test_paddle_edges():
    # ball at by=29 moving right lands at bx+1; paddle px=10 covers 10..15
    for bx_new, hit in [(9, 0), (10, 1), (15, 1), (16, 0)]:
        n = step(s(bx_new - 1, 29, 0, 0, px=10), *NONE)
        assert (n.dy, n.by) == ((1, 28) if hit else (0, 30)), bx_new
    # old paddle position is used even if the paddle moves this frame
    n = step(s(15, 29, 0, 0, px=10), 0, 1, 0)
    assert n.px == 11 and n.dy == 0


def test_ball_loss_and_game_over():
    n = step(s(7, 31, 1, 0, px=4, lives=2), 1, 0, 0)
    assert n == State(16, 29, 0, 1, 3, ALL, 1)
    dead = s(7, 31, 1, 0, lives=1)
    n = step(dead, *NONE)
    assert n.lives == 0 and lose(n)
    assert step(n, 1, 0, 0) == n
    won = s(3, 3, 0, 0, bricks=0)
    assert win(won) and step(won, 0, 1, 0) == won
    assert step(won, 0, 0, 1) == INIT


def test_paddle_moves_and_clamps():
    assert step(s(3, 3, 0, 0, px=5), 1, 0, 0).px == 4
    assert step(s(3, 3, 0, 0, px=5), 0, 1, 0).px == 6
    assert step(s(3, 3, 0, 0, px=5), 1, 1, 0).px == 5
    assert step(s(3, 3, 0, 0, px=0), 1, 0, 0).px == 0
    assert step(s(3, 3, 0, 0, px=26), 0, 1, 0).px == 26


def test_pack_roundtrip_and_invariants():
    rng = random.Random(1)
    assert NBITS == 51 and unpack(pack(INIT)) == INIT
    for _ in range(200):
        st = INIT
        for _ in range(500):
            st = step(st, rng.random() < .3, rng.random() < .3, rng.random() < .002)
            assert unpack(pack(st)) == st
            assert 0 <= st.bx < 32 and 0 <= st.by < 32 and 0 <= st.px <= 26 and 0 <= st.lives <= 3
