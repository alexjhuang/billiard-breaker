"""Run every test_* function in tests/test_*.py. Plain asserts, no framework."""
import importlib.util, pathlib, sys, time, traceback

root = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(root.parent))
only = sys.argv[1:]  # optional substrings to filter test names
failed = passed = 0
for path in sorted(root.glob('test_*.py')):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in sorted(n for n in dir(mod) if n.startswith('test_')):
        full = f'{path.stem}.{name}'
        if only and not any(s in full for s in only):
            continue
        t = time.time()
        try:
            getattr(mod, name)()
            passed += 1
            print(f'ok   {full} ({time.time() - t:.1f}s)')
        except Exception:
            failed += 1
            print(f'FAIL {full}')
            traceback.print_exc()
print(f'{passed} passed, {failed} failed')
sys.exit(1 if failed else 0)
