// node tests/engine_frames.js SITE_DIR FRAMES [wasm|wasm4] : run web/engine.js (or
// web/wasm.js, on 1 or 4 threads) on an exported site's hardware; print one JSON line per frame
// (inputs, state, flags, pictures) for tests/test_engine.py to compare.
const fs = require('fs'), path = require('path'), zlib = require('zlib');
const { Machine } = require(path.join(__dirname, '..', 'web', 'engine.js'));
const { WasmMachine } = require(path.join(__dirname, '..', 'web', 'wasm.js'));
const [dir, frames, engine] = process.argv.slice(2);
const meta = JSON.parse(fs.readFileSync(path.join(dir, 'machine.json')));
const buf = zlib.gunzipSync(fs.readFileSync(path.join(dir, 'walls.bin.gz')));
const squares = new Uint16Array(buf.buffer, buf.byteOffset, buf.length / 2);
(async () => {
const M = engine === 'wasm' ? await new WasmMachine(meta, squares, fs.readFileSync(path.join(dir, 'bbm.wasm'))).init()
  : engine === 'wasm4' ? await new WasmMachine(meta, squares, fs.readFileSync(path.join(dir, 'bbmt.wasm')), { threads: 4, epoch: 40 }).init() : new Machine(meta, squares);
for (let k = 0; k < +frames; k++) {
  const inp = { left: k % 7 === 0 ? 1 : 0, right: k % 5 === 0 ? 1 : 0, reset: k === 0 || k % 400 === 399 ? 1 : 0 };
  // pictures every frame, like the worker's schedule (a 16-step clip first, then single steps)
  const P = meta.P, traces = k % 3 === 0 ? [[16, 16], [148, 1], [297, 1]] : k % 3 === 1 ? [[446, 1], [594, 1], [743, 1]] : [[5000, 16], [20000, 1], [P - 1, 3]];
  const r = M.frame(inp, traces);
  console.log(JSON.stringify({ n: k, inp, state: r.state, win: r.win, lose: r.lose,
                               pics: r.pics.map(q => ({ ph: q.ph, counts: q.counts, cells: Array.from(q.raw) })) }));
}
})();
