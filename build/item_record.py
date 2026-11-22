"""Record, per test, the sqlglot lines each fixture item makes it run.

Build-time only. Many costly tests loop over a fixture file (optimizer fixture
pairs, function-annotation examples, identity SQL), checking one item at a
time. Each such test runs alone in a freshly forked process with the fixture
loaders wrapped, so the lines run while an item is being processed are
credited to that item, and the lines run outside any item to the test.

At run time a local check of such a test only feeds it the items whose lines
meet the change (when the change is known line by line and misses the
test's own lines), which makes checking it a fraction of its full cost.

usage: item_record.py SQLGLOT_ROOT MAPS_PKL OUT_DIR [JOBS]
"""

import os
import pickle
import signal
import sys
import time

TEST_FILES = ("tests/test_optimizer.py", "tests/test_transpile.py", "tests/test_serde.py")
LOADERS = ("load_sql_fixture_pairs", "load_sql_fixtures")


class _Lines:
    """sqlglot lines run, credited to the innermost open block (an item) or
    to the test when no item is open."""

    TOOL = 4

    def __init__(self):
        self.stack = [set()]  # [test lines, item lines...]

    def start(self):
        mon = sys.monitoring
        stack = self.stack

        def on_line(code, line):
            f = code.co_filename
            k = f.rfind("/sqlglot/")
            if k >= 0:
                stack[-1].add((f[k + 1:], line))
            return mon.DISABLE

        mon.use_tool_id(self.TOOL, "items")
        mon.register_callback(self.TOOL, mon.events.LINE, on_line)
        mon.set_events(self.TOOL, mon.events.LINE)
        mon.restart_events()

    def push(self):
        self.stack.append(set())
        sys.monitoring.restart_events()

    def pop(self):
        s = self.stack.pop()
        sys.monitoring.restart_events()
        return s


def main():
    root, maps_pkl, out = sys.argv[1], sys.argv[2], sys.argv[3]
    jobs = int(sys.argv[4]) if len(sys.argv) > 4 else 4
    ids = pickle.load(open(maps_pkl, "rb"))["test_ids"]
    work = [t for t in ids if t.split("::")[0] in TEST_FILES]
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, root)
    os.chdir(root)
    os.environ.setdefault("SKIP_INTEGRATION", "0")
    import unittest

    import brpool

    brpool.install()
    import tests.helpers as helpers

    mods = {}
    for t in work:
        m = t.split("::")[0][:-3].replace("/", ".")
        if m not in mods:
            try:
                mods[m] = __import__(m, fromlist=["*"])
            except Exception:  # noqa: BLE001
                mods[m] = None
    loader = unittest.TestLoader()
    import tempfile

    spool = tempfile.mkdtemp(prefix="bri_")
    results = {}
    running = {}
    pending = list(work)
    t0 = time.time()
    while pending or running:
        while pending and len(running) < jobs:
            tid = pending.pop(0)
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(r)
                payload = None
                try:
                    signal.alarm(120)
                    devnull = os.open(os.devnull, os.O_RDWR)
                    os.dup2(devnull, 0)
                    os.dup2(devnull, 1)
                    os.dup2(devnull, 2)
                    file, _, rest = tid.partition("::")
                    mod = mods[file[:-3].replace("/", ".")]
                    lines = _Lines()
                    items = {}  # fixture name -> [(lines, cpu)]
                    used = []

                    def wrap(orig):
                        def gen(name, *a, **kw):
                            seq = items.setdefault(name, [])
                            used.append(name)
                            it = orig(name, *a, **kw)
                            while True:
                                try:
                                    item = next(it)
                                except StopIteration:
                                    return
                                lines.push()
                                t1 = time.process_time()
                                try:
                                    yield item
                                finally:
                                    seq.append((frozenset(lines.pop()), time.process_time() - t1))
                        gen.__name__ = orig.__name__
                        return gen

                    for nm in LOADERS:
                        orig = getattr(helpers, nm)
                        w_ = wrap(orig)
                        setattr(helpers, nm, w_)
                        if hasattr(mod, nm):
                            setattr(mod, nm, w_)
                    lines.start()
                    suite = loader.loadTestsFromName(rest.replace("::", "."), mod)
                    res = unittest.TestResult()
                    t1 = time.process_time()
                    suite.run(res)
                    total = time.process_time() - t1
                    sys.monitoring.set_events(lines.TOOL, 0)
                    if res.testsRun and res.wasSuccessful() and items and len(set(used)) == len(used):
                        payload = {"outer": frozenset(lines.stack[0]), "items": items, "cpu": total}
                except BaseException:  # noqa: BLE001
                    payload = None
                os.close(w)
                with open(os.path.join(spool, "%d.pkl" % os.getpid()), "wb") as fh:
                    fh.write(pickle.dumps(payload))
                os._exit(0)
            os.close(w)
            running[pid] = (r, tid)
        pid, _ = os.wait()
        r, tid = running.pop(pid)
        os.close(r)
        f = os.path.join(spool, "%d.pkl" % pid)
        try:
            with open(f, "rb") as fh:
                d = pickle.loads(fh.read())
            os.remove(f)
        except Exception:  # noqa: BLE001
            d = None
        if d is not None:
            results[tid] = d
    # compact (file, line) pairs: file index * 1_000_000 + line
    files = sorted({f for d in results.values() for f, _ in d["outer"]}
                   | {f for d in results.values() for seq in d["items"].values() for s, _ in seq for f, _ in s})
    fidx = {f: i for i, f in enumerate(files)}

    def enc(s):
        return frozenset(fidx[f] * 1_000_000 + line for f, line in s)

    # per test, one small file: lines every item runs, and for the other lines
    # the items that run them
    import hashlib

    os.makedirs(out, exist_ok=True)
    index = {}
    for tid, d in results.items():
        fx = {}
        for name, seq in d["items"].items():
            sets = [enc(s) for s, _c in seq]
            common = frozenset.intersection(*sets) if sets else frozenset()
            inv = {}
            for i, st in enumerate(sets):
                for line in st - common:
                    inv.setdefault(line, []).append(i)
            fx[name] = {"n": len(sets), "all": common, "inv": {k: tuple(v) for k, v in inv.items()},
                        "cpu": [c for _s, c in seq]}
        rec = {"outer": enc(d["outer"]), "cpu": d["cpu"], "fixtures": fx}
        name = hashlib.sha1(tid.encode()).hexdigest()[:16] + ".pkl"
        with open(os.path.join(out, name), "wb") as fh:
            pickle.dump(rec, fh, protocol=pickle.HIGHEST_PROTOCOL)
        index[tid] = name
    with open(os.path.join(out, "index.pkl"), "wb") as fh:
        pickle.dump({"files": files, "tests": index}, fh, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"item lines for {len(index)} of {len(work)} fixture-loop tests in {time.time() - t0:.0f}s",
          file=sys.stderr)


if __name__ == "__main__":
    main()
