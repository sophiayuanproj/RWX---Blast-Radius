"""Record, per test, what every comparison in sqlglot evaluated to.

Build-time only. Every single-operator comparison inside a sqlglot function
(``a < b``, ``x is None``, ``k in d`` ...) is rewritten in a private copy of
the pinned source into a call that evaluates it exactly as Python would and
remembers, for the running test, how that comparison would have come out
under each comparison operator, plus the operand values themselves when they
are plain literals. Each test runs alone in a freshly forked process, so a
test's record holds every comparison it needs, caches included.

At selection time a patch that only changes one comparison (its operator, or
one constant operand) cannot affect a test whose recorded comparisons all
come out the same before and after the edit.

Operands whose comparison could run user code (a custom __eq__, __lt__,
__contains__ ...) are not evaluated under other operators: the site is marked
unmeasured for that test and the test stays selected.

usage: value_record.py SQLGLOT_ROOT TESTS_JSON OUT_DIR [JOBS]
"""

import ast
import enum
import hashlib
import json
import os
import pickle
import shutil
import signal
import sys
import tempfile
import time

OPS = ("Lt", "LtE", "Gt", "GtE", "Eq", "NotEq", "Is", "IsNot", "In", "NotIn")
CAP = 16  # distinct records per (test, site); more marks the site unmeasured
PLAIN = (type(None), bool, int, float, str, bytes)


# ------------------------------------------------------------- rewriting


class _Rewrite(ast.NodeTransformer):
    def __init__(self, path, sites):
        self.path = path
        self.sites = sites
        self.depth = 0

    def _func(self, node):
        self.depth += 1
        self.generic_visit(node)
        self.depth -= 1
        return node

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _func

    def visit_Compare(self, node):
        self.generic_visit(node)
        if self.depth == 0 or len(node.ops) != 1:
            return node
        op = type(node.ops[0]).__name__
        if op not in OPS:
            return node
        sid = len(self.sites)
        self.sites.append((self.path, node.lineno, node.col_offset, node.end_lineno, node.end_col_offset, op))
        call = ast.Call(
            func=ast.Name(id="__brv__", ctx=ast.Load()),
            args=[ast.Constant(sid), ast.Constant(OPS.index(op)), node.left, node.comparators[0]],
            keywords=[],
        )
        return ast.copy_location(call, node)


def rewrite_tree(root, dst):
    """Copy root/sqlglot to dst/sqlglot with comparisons instrumented."""
    sites = []
    src_pkg = os.path.join(root, "sqlglot")
    for dp, dn, fn in os.walk(src_pkg):
        dn[:] = [d for d in dn if d != "__pycache__"]
        rel = os.path.relpath(dp, root)
        os.makedirs(os.path.join(dst, rel), exist_ok=True)
        for f in fn:
            sp = os.path.join(dp, f)
            dp_ = os.path.join(dst, rel, f)
            if not f.endswith(".py"):
                shutil.copy2(sp, dp_)
                continue
            text = open(sp, encoding="utf-8").read()
            relf = os.path.join(rel, f)
            try:
                tree = ast.parse(text)
                tree = _Rewrite(relf, sites).visit(tree)
                ast.fix_missing_locations(tree)
                code = compile(tree, sp, "exec")
            except Exception:  # noqa: BLE001
                shutil.copy2(sp, dp_)
                continue
            # keep the source file (tracebacks, inspect) and ship the code as a .pyc
            shutil.copy2(sp, dp_)
            import importlib.util
            import marshal

            cache = importlib.util.cache_from_source(dp_)
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            st = os.stat(dp_)
            data = bytearray(importlib.util.MAGIC_NUMBER)
            data += (0).to_bytes(4, "little")
            data += (int(st.st_mtime) & 0xFFFFFFFF).to_bytes(4, "little")
            data += (st.st_size & 0xFFFFFFFF).to_bytes(4, "little")
            data += marshal.dumps(code)
            with open(cache, "wb") as fh:
                fh.write(data)
    return sites


# ------------------------------------------------------------- recording


_BUILTIN_CMP = {getattr(t, n) for t in (object, str, int, float, bytes, tuple) for n in
                ("__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__")}


def _safe(v, depth=0):
    """Comparing v (and anything inside it) runs no user code."""
    t = type(v)
    if t in PLAIN:
        return True
    if depth > 2:
        return False
    if t in (tuple, frozenset, list, set):
        return len(v) <= 64 and all(_safe(x, depth + 1) for x in v)
    if t is dict:
        return len(v) <= 64 and all(_safe(k, depth + 1) for k in v)
    return _safe_type(t)


def _safe_type(t):
    """==, !=, <, <=, >, >= and hashing on values of type t run no user code."""
    if t in PLAIN:
        return True
    if issubclass(t, enum.Enum):
        return all(getattr(t, n) in _BUILTIN_CMP for n in ("__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__")) \
            and getattr(t, "__hash__", None) in (object.__hash__, str.__hash__, int.__hash__, enum.Enum.__hash__)
    return (all(getattr(t, n) is getattr(object, n) for n in ("__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__"))
            and getattr(t, "__hash__", None) is object.__hash__
            and type(t) is type)


def _container_ok(b):
    """`x in b` runs no user code (given a safe x)."""
    t = type(b)
    if t in (str, bytes):
        return True
    if t in (tuple, list, set, frozenset):
        return len(b) <= 64 and all(_safe_type(type(x)) and type(x) not in (tuple,) or _safe(x) for x in b)
    if t is dict:
        return len(b) <= 64 and all(_safe(k) for k in b)
    return False


def _plain(v, depth=0):
    t = type(v)
    if t in PLAIN:
        return not (t is float and v != v)
    return t is tuple and depth < 3 and len(v) <= 16 and all(_plain(x, depth + 1) for x in v)


def _outcomes(a, b):
    out = []
    contain = _container_ok(b)
    for k, f in enumerate((lambda: a < b, lambda: a <= b, lambda: a > b, lambda: a >= b, lambda: a == b,
                           lambda: a != b, lambda: a is b, lambda: a is not b, lambda: a in b, lambda: a not in b)):
        if k >= 8 and not contain:
            out.append("?")  # containment would run user code: unknown
            continue
        try:
            r = f()
            out.append(bool(r) if type(r) is bool else "X")
        except Exception as e:  # noqa: BLE001
            out.append("E:" + type(e).__name__)
    return tuple(out)


def run_record(root, tests, out_dir, jobs):
    tmp = tempfile.mkdtemp(prefix="brv_")
    sites = rewrite_tree(root, tmp)
    os.symlink(os.path.join(root, "tests"), os.path.join(tmp, "tests"))
    for extra in ("setup.cfg", "pyproject.toml", "conftest.py"):
        if os.path.exists(os.path.join(root, extra)):
            os.symlink(os.path.join(root, extra), os.path.join(tmp, extra))
    print(f"rewrote {len(sites)} comparison sites", file=sys.stderr)
    sys.path.insert(0, tmp)
    os.chdir(tmp)
    os.environ.setdefault("SKIP_INTEGRATION", "0")

    import builtins
    import operator
    import unittest

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import brpool

    brpool.install()

    fns = (operator.lt, operator.le, operator.gt, operator.ge, operator.eq, operator.ne,
           operator.is_, operator.is_not, lambda a, b: a in b, lambda a, b: a not in b)
    rec = {}  # site -> set of records, or "U"

    plain_t = frozenset(PLAIN)
    type_safe = {}
    U = "U"

    def brv(sid, op, a, b):
        r = fns[op](a, b)
        cur = rec.get(sid)
        if cur is U:
            return r
        ta = type(a)
        tb = type(b)
        # a cheap key that fixes every outcome: the values for literals and
        # hashable enum members, identity otherwise
        if ta in plain_t and tb in plain_t:
            k = (a, b)
        else:
            sa = type_safe.get(ta)
            if sa is None:
                sa = type_safe[ta] = ta not in (tuple, list, dict, set, frozenset) and _safe_type(ta)
            sb = type_safe.get(tb)
            if sb is None:
                sb = type_safe[tb] = tb not in (tuple, list, dict, set, frozenset) and _safe_type(tb)
            if sa and sb:
                k = (ta, tb, a is b, a if ta in plain_t or issubclass(ta, enum.Enum) else None,
                     b if tb in plain_t or issubclass(tb, enum.Enum) else None)
            elif (sa or ta in (tuple, frozenset)) and (sb or tb in (tuple, frozenset)):
                try:
                    k = (a, b)
                    hash(k)
                except TypeError:
                    rec[sid] = U
                    return r
                if not (_safe(a) and _safe(b)):
                    rec[sid] = U
                    return r
            else:
                # containers we would have to scan, or user-defined comparisons
                if (ta in (list, dict, set) or tb in (list, dict, set)) and _safe(a) and _safe(b):
                    k = None
                else:
                    rec[sid] = U
                    return r
        if cur is None:
            cur = rec[sid] = {}
        if k is not None and k in cur:
            return r
        if len(cur) >= CAP:
            rec[sid] = U
            return r
        item = (_outcomes(a, b), (a, b) if _plain(a) and _plain(b) else None)
        cur[k if k is not None else ("#", len(cur))] = item
        return r

    builtins.__brv__ = brv
    import sqlglot  # noqa: F401  (instrumented package, imported once)

    # unittest names for the pytest ids
    loader = unittest.TestLoader()
    work = []
    for t in tests:
        tid = t["id"]
        file, _, rest = tid.partition("::")
        mod = file[:-3].replace("/", ".")
        work.append((t["number"], tid, mod, rest.replace("::", ".")))
    mods = sorted({w[2] for w in work})
    for m in mods:
        try:
            __import__(m)
        except Exception:  # noqa: BLE001
            pass
    rec.clear()  # import-time comparisons belong to no test

    results = {}
    running = {}
    pending = list(work)
    t0 = time.time()
    while pending or running:
        while pending and len(running) < jobs:
            num, tid, mod, name = pending.pop(0)
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(r)
                ok = False
                try:
                    signal.alarm(120)
                    devnull = os.open(os.devnull, os.O_RDWR)
                    os.dup2(devnull, 0)  # a test reading stdin must not wait
                    os.dup2(devnull, 1)
                    os.dup2(devnull, 2)
                    rec.clear()
                    suite = loader.loadTestsFromName(name, sys.modules[mod])
                    res = unittest.TestResult()
                    suite.run(res)
                    ok = res.testsRun > 0 and res.wasSuccessful()
                except BaseException:  # noqa: BLE001
                    ok = False
                payload = pickle.dumps({"ok": ok, "rec": {k: (v if v == "U" else frozenset(v.values())) for k, v in rec.items()}})
                with os.fdopen(w, "wb") as fh:
                    fh.write(payload)
                os._exit(0)
            os.close(w)
            running[pid] = (r, tid, bytearray())
        pid, _status = os.wait()
        r, tid, buf = running.pop(pid)
        with os.fdopen(r, "rb") as fh:
            data = fh.read()
        try:
            d = pickle.loads(data)
            results[tid] = d["rec"] if d["ok"] else None
        except Exception:  # noqa: BLE001
            results[tid] = None
    print(f"recorded {sum(1 for v in results.values() if v is not None)}/{len(results)} tests "
          f"in {time.time() - t0:.0f}s", file=sys.stderr)

    # shard by source file: site key -> {test id: records or "U"}
    by_file = {}
    for tid, recs in results.items():
        if recs is None:
            continue
        for sid, v in recs.items():
            path, l0, c0, l1, c1, op = sites[sid]
            by_file.setdefault(path, {}).setdefault((l0, c0, l1, c1), {})[tid] = v
    os.makedirs(out_dir, exist_ok=True)
    for path, d in by_file.items():
        name = hashlib.sha1(path.encode()).hexdigest()[:16] + ".pkl"
        with open(os.path.join(out_dir, name), "wb") as fh:
            pickle.dump(d, fh, protocol=pickle.HIGHEST_PROTOCOL)
    with open(os.path.join(out_dir, "index.pkl"), "wb") as fh:
        pickle.dump({"recorded": sorted(t for t, v in results.items() if v is not None),
                     "sites": {(s[0], s[1], s[2], s[3], s[4]): s[5] for s in sites}}, fh)
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    root, tests_json, out_dir = sys.argv[1], sys.argv[2], sys.argv[3]
    jobs = int(sys.argv[4]) if len(sys.argv) > 4 else 4
    if tests_json.endswith(".pkl"):
        ids = pickle.load(open(tests_json, "rb"))["test_ids"]
        tests = [{"id": t, "number": i} for i, t in enumerate(ids)]
    else:
        tests = json.load(open(tests_json))["tests"]
    run_record(os.path.abspath(root), tests, out_dir, jobs)
