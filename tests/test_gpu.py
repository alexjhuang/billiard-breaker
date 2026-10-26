"""web/gpu.js (the WebGPU engine of the static site) == web/engine.js: same states,
flags and recorded balls, frame for frame. Runs tests/gpu_check.html in headless
Chromium on its software WebGPU adapter (slow: ~2 min a frame), so it needs node
and a Chromium (CHROME, or playwright's under ~/.cache); skipped otherwise."""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

ROOT = pathlib.Path(__file__).resolve().parent.parent
FLAGS = ['--enable-unsafe-webgpu', '--enable-features=Vulkan', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader']


def chromium():
    c = os.environ.get('CHROME')
    if c and os.path.exists(c):
        return c
    for p in sorted(pathlib.Path.home().glob('.cache/ms-playwright/chromium-*/chrome-linux/chrome')):
        return str(p)
    return shutil.which('chromium') or shutil.which('google-chrome')


def test_gpu_engine_matches_js_engine():
    node = shutil.which('node')
    if not node or not chromium():
        print('    skipped: needs node and a Chromium')
        return
    sys.path.insert(0, str(ROOT))
    import web
    with tempfile.TemporaryDirectory() as d:
        web.export(d)
        shutil.copy(ROOT / 'tests' / 'gpu_check.html', d)
        srv = ThreadingHTTPServer(('127.0.0.1', 0), partial(Quiet, directory=d))
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            url = f'http://127.0.0.1:{srv.server_address[1]}/gpu_check.html?frames=2'
            out = subprocess.run([node, str(ROOT / 'tests' / 'cdp_title.js'), url, '300', *FLAGS],
                                 capture_output=True, text=True, timeout=900, env={**os.environ, 'CHROME': chromium()}).stdout
        finally:
            srv.shutdown()
    res = json.loads(out.strip().splitlines()[-1])
    assert res.get('ok'), res
    assert res['frames'] == 2 and res['alive'] > 0, res
    print(f"    {res['gpu']}: {res['frames']} frames identical to engine.js ({res['msPerFrame']} ms each on the software adapter)")
