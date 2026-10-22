"""Draw the compiled machine as PNG images (stdlib only).

python3 docs/render.py  ->  docs/frame.png (whole loop, downsampled) and
docs/slot.png (one gate slot, around its lanes). Coordinates are uv: u along the
loop (lanes move right), v up the band; wall cells are drawn dark, the lines
lanes use are faint.
"""
import os
import struct
import sys
import zlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import game_machine  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def png(path, w, h, pixels):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    raw = b''.join(b'\x00' + bytes(pixels[3 * w * y:3 * w * (y + 1)]) for y in range(h))
    with open(path, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
                + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))


def draw(m, u_lo, u_hi, scale, path, v_lo=None, v_hi=None):
    """Cells with u in [u_lo, u_hi) (and v in [v_lo, v_hi)) at `scale` uv units per pixel."""
    v_lo = m.lo / 2 if v_lo is None else v_lo
    v_hi = m.hi / 2 if v_hi is None else v_hi
    w = int((u_hi - u_lo) / scale) + 1
    h = int((v_hi - v_lo) / scale) + 1
    px = bytearray([255]) * (3 * w * h)
    for line in range(0, 400):
        c = m.line_c(line)
        if c < m.lo:
            break
        y = int((v_hi - c / 2) / scale)
        if not 0 <= y < h:
            continue
        for x in range(w):
            i = 3 * (y * w + x)
            px[i:i + 3] = b'\xe8\xee\xf8'
    for b in m.L.bars:
        for x, yy in b.cells():
            u, v = (x + yy) / 2, (x - yy) / 2
            if u_lo <= u < u_hi and v_lo <= v < v_hi:
                X, Y = int((u - u_lo) / scale), int((v_hi - v) / scale)
                if 0 <= X < w and 0 <= Y < h:
                    i = 3 * (Y * w + X)
                    px[i:i + 3] = b'\x1d\x2b\x53'
    png(path, w, h, px)
    return w, h


if __name__ == '__main__':
    m = game_machine.load()
    print('frame.png', draw(m, 0, m.U, 12, os.path.join(HERE, 'frame.png')))
    j = next(j for j, s in enumerate(m.plan.slots) if s.kind == 'op' and s.op.keep == 'AB')
    s = m.plan.slots[j]
    lines = [s.live[c][0] for c in s.op.ctrls] + [s.data_line, s.land[1]]
    v_top, v_bot = m.line_c(min(lines)) / 2 + 30, m.line_c(max(lines)) / 2 - 30
    print('slot.png', draw(m, m.u0[j] - 2, m.u0[j + 1] + 2, 0.25, os.path.join(HERE, 'slot.png'), v_bot, v_top))
