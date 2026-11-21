"""CPU seconds each test takes under the selector's in-process runner."""
import json, os, sys, time, unittest
root, tests_json, out = sys.argv[1:4]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import brpool
brpool.install()
sys.path.insert(0, root); os.chdir(root)
tests = json.load(open(tests_json))["tests"]
only = os.environ.get("ONLY_FILE")
if only:
    tests = [t for t in tests if t["file"] == only]
cpu = {}
devnull = os.open(os.devnull, os.O_WRONLY); os.dup2(devnull, 1)
t_import = {}
gap = {}
last = [None]
class R(unittest.TestResult):
    def startTest(self, t):
        super().startTest(t); self.t0 = time.process_time()
        cls = t.id().rsplit(".", 1)[0]
        if cls not in gap:
            gap[cls] = self.t0 - last[0]
    def stopTest(self, t):
        super().stopTest(t); last[0] = time.process_time(); cpu[num[t.id()]] = last[0] - self.t0
num = {}
suite = unittest.TestSuite()
for t in tests:
    f, _, rest = t["id"].partition("::")
    mod = f[:-3].replace("/", ".")
    t0 = time.process_time()
    m = __import__(mod, fromlist=["*"])
    if mod not in t_import:
        t_import[mod] = time.process_time() - t0
    obj = m
    parts = rest.split("::")
    for p in parts[:-1]:
        obj = getattr(obj, p)
    tc = obj(parts[-1]); num[tc.id()] = t["number"]; suite.addTest(tc)
r = R(); t0 = time.process_time(); last[0] = t0; suite.run(r)
print("total", time.process_time() - t0, "errors", len(r.errors), len(r.failures), file=sys.stderr)
for e in (r.errors + r.failures)[:5]: print(e[0], e[1][-500:], file=sys.stderr)
json.dump({"cpu": cpu, "import": t_import, "class_setup": gap}, open(out, "w"))
