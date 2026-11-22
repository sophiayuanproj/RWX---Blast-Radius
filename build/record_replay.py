"""Record executor/DuckDB results of the replayable tests at the pinned
commit, then check that replaying them passes and time it.

usage: record_replay.py SQLGLOT_ROOT OUT_PKL           (record + verify)
       record_replay.py SQLGLOT_ROOT OUT_PKL --verify  (verify only)
"""

import os
import pickle
import subprocess
import sys
import time
import unittest

root, out = sys.argv[1], sys.argv[2]
verify_only = "--verify" in sys.argv
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, root)
os.chdir(root)

import brpool  # noqa: E402
import brreplay  # noqa: E402

brpool.install()
devnull = os.open(os.devnull, os.O_WRONLY)
os.dup2(devnull, 1)


def run(mode, data, light=False):
    mod = __import__(brreplay.TEST_MODULE, fromlist=["*"])
    hooks = brreplay.Hooks(data, mode, budget=0.0 if light else float("inf"))
    if light:
        brreplay.light_setup(mod.TestExecutor)
    tests = [getattr(mod, "TestExecutor")(tid.rsplit("::", 1)[1]) for tid in brreplay.REPLAY_TESTS]
    cpu = {}

    class R(unittest.TestResult):
        def startTest(self, t):
            super().startTest(t)
            hooks.install(t)
            self.t0 = time.process_time()

        def stopTest(self, t):
            cpu[t.id()] = time.process_time() - self.t0
            hooks.uninstall()
            super().stopTest(t)

        def addSubTest(self, t, s, e):
            if e is not None:
                self.failures.append((t, str(e[1])[:300]))

    r = R()
    t0 = time.process_time()
    unittest.TestSuite(tests).run(r)
    setup = time.process_time() - t0 - sum(cpu.values())
    return r, cpu, setup


if not verify_only:
    data = {"exec": {}, "duck": {}}
    r, cpu, setup = run("record", data)
    assert not r.errors and not r.failures, (r.errors, r.failures)
    with open(out, "wb") as f:
        pickle.dump(data, f)
    print(f"recorded {len(data['exec'])} executor results, {len(data['duck'])} duckdb results; cpu {cpu}",
          file=sys.stderr)
    # verify in a fresh process: digests must not depend on the process
    r = subprocess.run([sys.executable, __file__, root, out, "--verify"])
    if r.returncode:
        os.remove(out)  # replay is only used when it reproduces the pinned commit
    sys.exit(r.returncode)

with open(out, "rb") as f:
    data = pickle.load(f)
r, cpu, setup = run("replay", data)
print("replay errors", [(str(t), e[:300]) for t, e in r.errors][:3], "failures",
      [(str(t), e[:300]) for t, e in r.failures][:3], file=sys.stderr)
print(f"replay cpu {cpu} setup {setup:.2f}", file=sys.stderr)
ok = not r.errors and not r.failures
data["cpu"] = {tid: cpu.get(tid.replace("/", ".").replace(".py::", ".").replace("::", "."), 0.0)
               for tid in brreplay.REPLAY_TESTS}
data["setup_cpu"] = setup
if ok:
    # once more with the light class setup and no real fallbacks
    del sys.modules[brreplay.TEST_MODULE]
    r2, cpu2, setup2 = run("replay", data, light=True)
    print("light replay errors", r2.errors[:2], "failures", r2.failures[:2], f"cpu {cpu2} setup {setup2:.2f}",
          file=sys.stderr)
    ok = not r2.errors and not r2.failures
    data["cpu"] = {tid: cpu2.get(tid.replace("/", ".").replace(".py::", ".").replace("::", "."), 0.0)
                   for tid in brreplay.REPLAY_TESTS}
    data["light_setup_cpu"] = setup2
# compact the per-query line sets: (file, line) -> file index * 1_000_000 + line
files = sorted({f for v in data.get("fe", {}).values() for f, _l in v["lines"]}
               | {f for v in data.get("fe_outer", {}).values() for f, _l in v})
fidx = {f: i for i, f in enumerate(files)}


def enc(lines):
    return frozenset(fidx[f] * 1_000_000 + l for f, l in lines)


if data.get("fe") and not data.get("fe_files"):
    data["fe"] = {q: dict(v, lines=enc(v["lines"])) for q, v in data["fe"].items()}
    data["fe_outer"] = {k: enc(v) for k, v in data.get("fe_outer", {}).items()}
    data["fe_files"] = files
with open(out, "wb") as f:
    pickle.dump(data, f)
sys.exit(0 if ok else 1)
