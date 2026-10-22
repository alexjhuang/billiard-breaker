// node tests/engine_frames.js SITE_DIR FRAMES [traceFrame] : run web/engine.js
// on an exported site's hardware; print one JSON line per frame (inputs,
// state, flags, pictures) for tests/test_engine.py to compare.
const fs = require('fs'), path = require('path'), zlib = require('zlib');
const { Machine } = require(path.join(__dirname, '..', 'web', 'engine.js'));
const [dir, frames, traceAt = '3'] = process.argv.slice(2);
const meta = JSON.parse(fs.readFileSync(path.join(dir, 'machine.json')));
const buf = zlib.gunzipSync(fs.readFileSync(path.join(dir, 'walls.bin.gz')));
const M = new Machine(meta, new Uint16Array(buf.buffer, buf.byteOffset, buf.length / 2));
for (let k = 0; k < +frames; k++) {
  const inp = { left: k % 7 === 0 ? 1 : 0, right: k % 5 === 0 ? 1 : 0, reset: k === 0 || k % 400 === 399 ? 1 : 0 };
  const r = M.frame(inp, k === +traceAt ? [[5000, 16], [20000, 1], [43000, 3]] : []);
  console.log(JSON.stringify({ n: k, inp, state: r.state, win: r.win, lose: r.lose,
                               pics: r.pics.map(q => ({ ph: q.ph, counts: q.counts, cells: Array.from(q.raw) })) }));
}
