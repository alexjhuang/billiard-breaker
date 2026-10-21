import os, pathlib, pty, select, sys, time
import oracle, screen

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_lines_draws_init():
    rows = screen.lines(oracle.INIT, False, False)
    field = ''.join(rows[:32])
    assert field.count('[]') == 32 * 8 and field.count('()') == 1 and field.count('==') == 6
    assert rows[29][1 + 2 * 16:1 + 2 * 16 + 2] == '()'


def play_smoke(script):
    """Run a curses frontend in a pty, press keys, quit; return exit status."""
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(ROOT)
        os.environ.update(TERM='xterm', LINES='40', COLUMNS='80')
        os.execv(sys.executable, [sys.executable, script])
    out = b''
    for key in b'dddaaar' + b'q':
        time.sleep(0.15)
        os.write(fd, bytes([key]))
        while select.select([fd], [], [], 0.05)[0]:
            try:
                out += os.read(fd, 65536)
            except OSError:
                break
    deadline = time.time() + 10
    while time.time() < deadline:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            return os.waitstatus_to_exitcode(status), out
        time.sleep(0.05)
    os.kill(pid, 9)
    return 'timeout', out


def test_play_oracle_runs():
    code, out = play_smoke('play_oracle.py')
    assert code == 0, (code, out[-500:])
    assert b'lives' in out
