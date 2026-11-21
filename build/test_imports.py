"""For each test file, the sqlglot (and tests.*) modules importing it loads."""
import json, os, subprocess, sys
root = sys.argv[1]; out = sys.argv[2]
tests = json.load(open(sys.argv[3]))["tests"]
files = sorted({t["file"] for t in tests})
res = {}
code = "import sys,importlib,json; importlib.import_module(sys.argv[1]); print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] in ('sqlglot','tests'))))"
for f in files:
    mod = f[:-3].replace("/", ".")
    r = subprocess.run([sys.executable, "-c", code, mod], cwd=root, capture_output=True, text=True, env={**os.environ, "PYTHONPATH": root})
    try:
        res[f] = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        print("fail", f, r.stderr[-300:], file=sys.stderr); res[f] = []
json.dump(res, open(out, "w"))
