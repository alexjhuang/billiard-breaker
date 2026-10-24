// node tests/cdp_title.js URL SECONDS FLAGS... : load URL in headless Chromium (the playwright
// build under ~/.cache/ms-playwright if CHROME is not set), print document.title after SECONDS.
const { spawn } = require('child_process');
const [url, secs = '5', ...flags] = process.argv.slice(2);
const CH = process.env.CHROME || process.env.HOME + "/.cache/ms-playwright/chromium-1134/chrome-linux/chrome";
const port = 9333 + Math.floor(Math.random() * 100);
const chrome = spawn(CH, ['--headless=new', '--no-sandbox', '--disable-gpu-sandbox', `--remote-debugging-port=${port}`, ...flags, 'about:blank'], { stdio: ['ignore', 'ignore', 'pipe'] });
let err = ''; chrome.stderr.on('data', d => { err += d; });
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  let targets;
  for (let i = 0; i < 50; i++) { try { targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); break; } catch { await sleep(200); } }
  const page = targets.find(t => t.type === 'page');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise(r => ws.onopen = r);
  let id = 0; const pending = new Map();
  const send = (method, params = {}) => new Promise(res => { ws.send(JSON.stringify({ id: ++id, method, params })); pending.set(id, res); });
  ws.onmessage = e => { const m = JSON.parse(e.data); if (m.id) pending.get(m.id)(m.result); else if (m.method === 'Runtime.exceptionThrown') console.log('EXCEPTION', m.params.exceptionDetails.exception?.description); else if (m.method === 'Runtime.consoleAPICalled') console.log('console', m.params.args.map(a => a.value ?? a.description).join(' ')); };
  await send('Runtime.enable'); await send('Page.enable');
  await send('Page.navigate', { url });
  await sleep(1000 * +secs);
  const r = await send('Runtime.evaluate', { expression: 'document.title', returnByValue: true });
  console.log(r.result.value);
  if (process.env.SHOWERR) console.log(err.split('\n').filter(l => /gpu|vulkan|dawn|webgpu/i.test(l)).slice(0, 10).join('\n'));
  ws.close(); chrome.kill();
})().catch(e => { console.error(e); chrome.kill(); process.exit(1); });
