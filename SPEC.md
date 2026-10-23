# Game spec (frozen at M1; changed 2026-10-23 for the arcade rules, see DECISIONS.md)

The reference is `oracle.py`; this file says the same thing in words.

## State (71 bits, packed in this order, LSB first)
| field  | bits | meaning |
|--------|------|---------|
| bx     | 5 | ball x, 0..31 |
| by     | 5 | ball y, 0..31 (0 = top) |
| dx     | 1 | 0 = moving right (+1), 1 = left (−1) |
| dy     | 1 | 0 = moving down (+1), 1 = up (−1) |
| px     | 5 | paddle left edge, 0..26 (0..29 when small); the paddle covers px..px+5 (px..px+2) on row 30 |
| bricks | 32 | bit i = brick at row i>>3, column i&7 |
| lives  | 2 | 0..3 |
| score  | 9 | 0..256 |
| speed  | 2 | 0..3: the ball moves on 1/2, 5/8, 3/4, all of the frames |
| ph     | 3 | frame counter mod 8, for the cadence |
| hits   | 4 | bricks hit with this ball, saturating at 15 |
| small  | 1 | the paddle is 3 wide |
| screen | 1 | 0 = first wall of bricks, 1 = second |

Inputs per frame: `left`, `right`, `reset`. Outputs: the next state, plus
`win` (bricks == 0 and screen == 1) and `lose` (lives == 0), computed from
the current state.

Bricks occupy rows y = 8..15, 4 cells wide × 2 tall: brick at cell (x, y) is
index `((y >> 1) & 3) * 8 + (x >> 2)`. Rows 8..15 so the decode is pure bit
slicing (GOAL suggested 2..9; moved to keep the circuit small). Brick rows
score 7, 5, 3, 1 points from the top (red, orange, green, yellow, as in the
arcade game, which has two rows of each).

Initial state: ball (16, 29) moving up-right, paddle at 13, all 32 bricks,
3 lives, everything else 0.

## Frame step (in this order)
1. `reset` → initial state.
2. If `lives == 0` or (`bricks == 0` and `screen == 1`): state unchanged
   (game over / won).
3. Paddle: `left` only and px > 0 → px−1; `right` only and px < 26 (29 when
   small) → px+1; both or neither → no move. `ph` += 1 (mod 8).
4. If `bricks == 0` (first wall cleared): all 32 bricks return, `screen` = 1,
   the ball is served again at (16, 29) up-right with `speed`, `hits`,
   `small` = 0; score and lives stay. Stop here.
5. Cadence: let r = `ph` with its 3 bits reversed. The ball moves this frame
   iff r < (4, 5, 6, 8)[speed]. If it does not, stop here (only the paddle
   and `ph` changed).
6. Horizontal wall: moving right at bx = 31, or left at bx = 0 → flip dx.
   Then bx += ±1.
7. Vertical:
   - moving up at by = 0 → flip dy (ceiling) and `small` = 1: the ball got
     through the bricks, the paddle shrinks to 3 wide (px..px+2);
   - else moving down at by = 29 and `0 <= bx_new − px_old < width` (6, or
     3 when small) → flip dy (paddle hit; uses the new ball x and the *old*
     paddle position);
   - moving down at by = 31 → ball lost: ball returns to (16, 29) up-right,
     lives −1, `speed`, `hits`, `small` = 0; bricks, score and screen
     unchanged, paddle keeps its new position. Stop here.
   Then by += ±1.
8. Brick: if the new cell (bx, by) holds a brick, clear it, flip dy, add the
   row's points to `score`, `hits` += 1 (saturating at 15), and raise
   `speed` to at least: 3 for a red (top) row brick, 2 for an orange one or
   the 12th hit, 1 for the 4th hit. The ball sits in the cleared cell this
   frame.
