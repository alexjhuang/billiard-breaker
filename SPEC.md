# Game spec (frozen at M1 — changes only via a dated DECISIONS.md entry)

The reference is `oracle.py`; this file says the same thing in words.

## State (51 bits, packed in this order, LSB first)
| field  | bits | meaning |
|--------|------|---------|
| bx     | 5 | ball x, 0..31 |
| by     | 5 | ball y, 0..31 (0 = top) |
| dx     | 1 | 0 = moving right (+1), 1 = left (−1) |
| dy     | 1 | 0 = moving down (+1), 1 = up (−1) |
| px     | 5 | paddle left edge, 0..26; paddle covers px..px+5 on row 30 |
| bricks | 32 | bit i = brick at row i>>3, column i&7 |
| lives  | 2 | 0..3 |

Inputs per frame: `left`, `right`, `reset`. Outputs: the next state, plus
`win` (bricks == 0) and `lose` (lives == 0), computed from the current state.

Bricks occupy rows y = 8..15, 4 cells wide × 2 tall: brick at cell (x, y) is
index `((y >> 1) & 3) * 8 + (x >> 2)`. Rows 8..15 so the decode is pure bit
slicing (GOAL suggested 2..9; moved to keep the circuit small).

Initial state: ball (16, 29) moving up-right, paddle at 13, all 32 bricks, 3 lives.

## Frame step (in this order)
1. `reset` → initial state.
2. If `lives == 0` or `bricks == 0`: state unchanged (game over / won).
3. Paddle: `left` only and px > 0 → px−1; `right` only and px < 26 → px+1;
   both or neither → no move.
4. Horizontal wall: moving right at bx = 31, or left at bx = 0 → flip dx.
   Then bx += ±1.
5. Vertical:
   - moving up at by = 0 → flip dy (ceiling);
   - else moving down at by = 29 and `0 <= bx_new − px_old <= 5` → flip dy
     (paddle hit; uses the new ball x and the *old* paddle position);
   - moving down at by = 31 → ball lost: ball returns to (16, 29) up-right,
     lives −1, bricks unchanged, paddle keeps its new position. Stop here.
   Then by += ±1.
6. Brick: if the new cell (bx, by) holds a brick, clear it and flip dy.
   The ball sits in the cleared cell this frame.
