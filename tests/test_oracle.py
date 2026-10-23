import random
from oracle import step, moves, INIT, win, lose, CADENCE
from state import State, pack, unpack, NBITS, POINTS

NONE = (0, 0, 0)
ALL = (1 << 32) - 1


def s(bx, by, dx, dy, px=13, bricks=ALL, lives=3, score=0, speed=3, ph=0, hits=0, small=0, screen=0):
    """A state whose ball moves this frame (speed 3 unless told otherwise)."""
    return State(bx, by, dx, dy, px, bricks, lives, score, speed, ph, hits, small, screen)


def test_walls_and_corner():
    assert step(s(31, 20, 0, 0), *NONE)[:4] == (30, 21, 1, 0)
    assert step(s(0, 20, 1, 1), *NONE)[:4] == (1, 19, 0, 1)
    assert step(s(5, 0, 0, 1), *NONE)[:4] == (6, 1, 0, 0)
    assert step(s(31, 0, 0, 1), *NONE)[:4] == (30, 1, 1, 0)


def test_brick_clear_scores_and_bounces():
    n = step(s(5, 16, 0, 1), *NONE)  # moves to (6, 15): row 3 (yellow), col 1 -> brick 25
    assert n[:4] == (6, 15, 0, 0) and n.bricks == ALL & ~(1 << 25)
    assert n.score == POINTS[3] == 1 and n.hits == 1
    n2 = step(n, *NONE)  # bounces back out, no brick there
    assert n2[:4] == (7, 16, 0, 0) and n2.bricks == n.bricks and n2.score == 1
    gap = s(5, 16, 0, 1, bricks=ALL & ~(1 << 25))
    assert step(gap, *NONE)[:4] == (6, 15, 0, 1)
    for row, y in enumerate((9, 11, 13, 15)):  # a hit in each row scores its points
        n = step(s(5, y + 1, 0, 1, score=100), *NONE)
        assert n.score == 100 + POINTS[row], row


def test_speed_steps_up():
    # 4th and 12th hits, orange and red rows
    assert step(s(5, 16, 0, 1, speed=0, hits=2), *NONE).speed == 0
    assert step(s(5, 16, 0, 1, speed=0, hits=3), *NONE).speed == 1
    assert step(s(5, 16, 0, 1, speed=1, hits=11), *NONE).speed == 2
    assert step(s(5, 12, 0, 1, speed=0), *NONE).speed == 2  # orange (row 1, y 10..11)
    assert step(s(5, 10, 0, 1, speed=0), *NONE).speed == 3  # red (row 0)
    assert step(s(5, 10, 0, 1, speed=0, hits=15), *NONE).hits == 15  # saturates
    assert step(s(5, 16, 0, 1, speed=3, hits=0), *NONE).speed == 3  # never slows down


def test_cadence():
    for speed, k in enumerate(CADENCE):
        st = s(5, 20, 0, 0, speed=speed)
        n_moves = 0
        for _ in range(8):
            if moves(st):
                n_moves += 1
                assert step(st, *NONE).by == 21
            else:
                assert step(st, *NONE)[:4] == st[:4]
            st = step(st, *NONE)._replace(bx=5, by=20, dx=0, dy=0)
            assert st.ph == (st.ph & 7)
        assert n_moves == k, speed
    # a frame without a move still moves the paddle and the clock
    st = s(5, 20, 0, 0, speed=0, ph=1)
    assert not moves(st) and step(st, 0, 1, 0) == st._replace(px=14, ph=2)


def test_paddle_edges_and_shrink():
    # ball at by=29 moving right lands at bx+1; paddle px=10 covers 10..15
    for bx_new, hit in [(9, 0), (10, 1), (15, 1), (16, 0)]:
        n = step(s(bx_new - 1, 29, 0, 0, px=10), *NONE)
        assert (n.dy, n.by) == ((1, 28) if hit else (0, 30)), bx_new
    # old paddle position is used even if the paddle moves this frame
    n = step(s(15, 29, 0, 0, px=10), 0, 1, 0)
    assert n.px == 11 and n.dy == 0
    # small paddle: px..px+2, and it may go further right
    for bx_new, hit in [(9, 0), (10, 1), (12, 1), (13, 0)]:
        n = step(s(bx_new - 1, 29, 0, 0, px=10, small=1), *NONE)
        assert (n.dy, n.by) == ((1, 28) if hit else (0, 30)), bx_new
    assert step(s(3, 3, 0, 0, px=26, small=1), 0, 1, 0).px == 27
    assert step(s(3, 3, 0, 0, px=29, small=1), 0, 1, 0).px == 29
    # the ceiling shrinks the paddle
    n = step(s(5, 0, 0, 1), *NONE)
    assert n.small == 1 and n.dy == 0


def test_ball_loss_resets_ball_speed_and_paddle_size():
    n = step(s(7, 31, 1, 0, px=4, lives=2, score=40, speed=3, hits=9, small=1, ph=3), 1, 0, 0)
    assert n == State(16, 29, 0, 1, 3, ALL, 1, 40, 0, 4, 0, 0, 0)
    dead = s(7, 31, 1, 0, lives=1)
    n = step(dead, *NONE)
    assert n.lives == 0 and lose(n)
    assert step(n, 1, 0, 0) == n


def test_second_screen_and_win():
    cleared = s(3, 3, 0, 0, bricks=0, score=128, speed=2, hits=7, small=1, ph=5)
    assert not win(cleared)
    n = step(cleared, 0, 1, 0)
    assert n == State(16, 29, 0, 1, 14, ALL, 3, 128, 0, 6, 0, 0, 1)
    won = s(3, 3, 0, 0, bricks=0, screen=1)
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
    assert NBITS == 71 and unpack(pack(INIT)) == INIT
    for _ in range(200):
        st = INIT
        for _ in range(500):
            st = step(st, rng.random() < .3, rng.random() < .3, rng.random() < .002)
            assert unpack(pack(st)) == st
            assert 0 <= st.bx < 32 and 0 <= st.by < 32 and 0 <= st.px <= 29 and 0 <= st.lives <= 3
            assert st.px <= 26 or st.small
            assert st.score <= 256 and st.hits <= 15 and st.ph < 8 and st.speed < 4
