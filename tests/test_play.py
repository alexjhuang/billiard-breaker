"""play.py: no game logic in the frontend, and its frames match the oracle."""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
FORBIDDEN = {'oracle', 'netsim', 'hw', 'hw.logic', 'hw.game', 'hw.lower', 'plan', 'compile',
             'game_machine', 'debug', 'cyl', 'sim', 'physics', 'layout'}


def test_frontend_imports_no_game_logic():
    code = 'import sys, play; print(" ".join(sorted(sys.modules)))'
    mods = set(subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.split())
    assert not (mods & FORBIDDEN), mods & FORBIDDEN
    src = (ROOT / 'play.py').read_text()
    assert 'oracle' not in src
    for word in ('step(', 'brick_at', 'PADDLE', 'win(', 'lose('):
        assert word not in src, word


def test_play_frames_match_oracle():
    sys.path.insert(0, str(ROOT))
    import play
    import hardware
    import fastsim
    import oracle
    runner = fastsim.Runner(hardware.load())
    keys = [(0, 0, 1)] + [(k % 3 == 0, k % 4 == 1, 0) for k in range(12)]
    st = None
    for n, (l, r, z) in enumerate(keys):
        runner.feed(dict(left=int(l), right=int(r), reset=int(z)))
        shown = play.decode(runner.outputs[n])
        if st is not None:
            assert shown == st, (n, shown, st)
        st = oracle.step(shown if st is None else st, l, r, z)  # frame 0 shows the empty world


def test_play_runs_in_a_terminal():
    from test_screen import play_smoke
    code, out = play_smoke('play.py')
    assert code == 0, (code, out[-500:])
    assert b'lives' in out
