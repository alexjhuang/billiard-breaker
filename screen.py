"""Terminal rendering and input loop shared by play_oracle.py and play.py.

No game rules here: it only draws a state and turns keys into input bits.
"""
import curses
import time
from state import W, H, PADDLE_Y, PADDLE_W, brick_at


def lines(s, win, lose, fps=None):
    rows = []
    for y in range(H):
        row = ''
        for x in range(W):
            i = brick_at(x, y)
            if (x, y) == (s.bx, s.by):
                row += '()'
            elif y == PADDLE_Y and s.px <= x < s.px + PADDLE_W:
                row += '=='
            elif i is not None and s.bricks >> i & 1:
                row += '[]'
            else:
                row += '  '
        rows.append('|' + row + '|')
    rows.append('+' + '-' * 2 * W + '+')
    msg = 'YOU WIN' if win else 'GAME OVER' if lose else ''
    rate = f'  {fps:.1f} fps' if fps is not None else ''
    rows.append(f' lives {s.lives}  bricks {bin(s.bricks).count("1")}{rate}  {msg}')
    rows.append(' a/d or arrows: move   r: reset   q: quit')
    return rows


def run(stepper, fps=15):
    """stepper(left, right, reset) -> (state, win, lose) for the next frame."""
    curses.wrapper(_loop, stepper, fps)


def _loop(scr, stepper, fps):
    curses.curs_set(0)
    scr.nodelay(True)
    left_keys = {curses.KEY_LEFT, ord('a')}
    right_keys = {curses.KEY_RIGHT, ord('d')}
    frame = stepper(0, 0, 1)
    deadline = t0 = time.monotonic()
    n = 0
    while True:
        n += 1
        scr.erase()
        try:
            for i, line in enumerate(lines(*frame, fps=n / max(time.monotonic() - t0, 1e-9))):
                scr.addstr(i, 0, line)
        except curses.error:
            scr.erase()
            scr.addstr(0, 0, f'terminal too small: need {2 * W + 2}x{H + 3}')
        scr.refresh()
        deadline += 1 / fps
        time.sleep(max(0.0, deadline - time.monotonic()))
        keys = set()
        while (k := scr.getch()) != -1:
            keys.add(k)
        if ord('q') in keys:
            return
        frame = stepper(bool(keys & left_keys), bool(keys & right_keys), ord('r') in keys)
