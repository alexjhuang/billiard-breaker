"""End-to-end differential test (GOAL.md): the billiard machine vs oracle.py,
bit-exact, 20 input sequences x 1000 frames, plus 2 bot games played through both
walls of bricks to a win (the bot wins at frame ~2190)."""
import multiprocessing as mp
import difftest


def test_machine_matches_oracle_20_x_1000_frames_and_wins():
    jobs = [(s, 1000, False) for s in range(20)] + [(s, 2400, False) for s in (1000, 1001)]
    with mp.Pool(len(jobs)) as p:
        res = p.map(difftest.run, jobs)
    bad = [r[:2] for r in res if not r[1].startswith('ok')]
    assert not bad, bad
    assert all('(0 wins' not in r[1] for r in res[20:]), res[20:]  # the bot games reach a win
