"""Replay for sqlglot's TPC-H / TPC-DS executor tests.

Those tests do three things per query:
  front end : parse, optimize, transpile to DuckDB  (cheap, sqlglot)
  executor  : plan + run the optimized query in sqlglot's Python executor
  expected  : run the original query in DuckDB      (expensive, no sqlglot)

When a patch can only change the front end (per-region coverage recorded at the
pinned commit), the test is run here with the front end real and the other two
parts memoized from recordings made at the pinned commit. Recordings are keyed
by what decides their result: a structural digest of the optimized expression
(plus the table names) for the executor, and the exact SQL text for DuckDB.
Inputs the pinned commit never produced are executed for real.
"""

import hashlib
import pickle

REPLAY_TESTS = (
    "tests/test_executor.py::TestExecutor::test_execute_tpcds",
    "tests/test_executor.py::TestExecutor::test_execute_tpch",
    "tests/test_executor.py::TestExecutor::test_optimized_tpch",
)
TEST_MODULE = "tests.test_executor"
TEST_FILE = "tests/test_executor.py"

_SKIP_SLOTS = {"parent", "arg_key", "index", "_hash", "__weakref__", "__dict__"}


class Diverge(BaseException):
    """An input the pinned commit never produced: the outcome is unknown."""


def expr_digest(e):
    """Structural digest of a sqlglot expression tree, computed without
    calling any sqlglot code (attribute and slot reads only)."""
    h = hashlib.blake2b(digest_size=16)
    memo = {}

    def walk(v, depth):
        if depth > 4000:
            h.update(b"<deep>")
            return
        t = type(v)
        mod = getattr(t, "__module__", "") or ""
        if v is None or isinstance(v, (bool, int, float, str, bytes)):
            h.update(f"{t.__name__}:{v!r};".encode("utf-8", "surrogatepass"))
            return
        if isinstance(v, (list, tuple)):
            h.update(f"{t.__name__}[".encode())
            for x in v:
                walk(x, depth + 1)
            h.update(b"]")
            return
        if isinstance(v, dict):
            h.update(b"{")
            for k in sorted(v, key=repr):
                h.update(repr(k).encode("utf-8", "surrogatepass"))
                walk(v[k], depth + 1)
            h.update(b"}")
            return
        if isinstance(v, (set, frozenset)):
            h.update(b"set" + repr(sorted(repr(x) for x in v)).encode("utf-8", "surrogatepass"))
            return
        if mod.startswith("sqlglot"):
            i = id(v)
            if i in memo:
                h.update(f"<ref{memo[i]}>".encode())
                return
            memo[i] = len(memo)
            h.update(f"<{mod}.{t.__qualname__}".encode())
            if hasattr(v, "name") and hasattr(t, "__members__"):  # enum member
                h.update(f".{v.name}>".encode())
                return
            names = []
            for k in t.__mro__:
                sl = k.__dict__.get("__slots__", ())
                if isinstance(sl, str):
                    sl = (sl,)
                names.extend(sl)
            d = getattr(v, "__dict__", None)
            if isinstance(d, dict):
                names.extend(d)
            for n in sorted(set(names)):
                if n in _SKIP_SLOTS:
                    continue
                try:
                    x = object.__getattribute__(v, n)
                except AttributeError:
                    continue
                h.update(n.encode())
                walk(x, depth + 1)
            h.update(b">")
            return
        h.update(f"<{mod}.{t.__qualname__}:{v!r}>".encode("utf-8", "surrogatepass"))

    walk(e, 0)
    return h.hexdigest()


def exec_key(expression, executor):
    tables = getattr(executor, "tables", None)
    names = ""
    try:
        names = ",".join(sorted(str(k) for k in tables.mapping)) if hasattr(tables, "mapping") else repr(tables)[:0]
    except Exception:  # noqa: BLE001
        names = ""
    return expr_digest(expression) + "|" + names


class _FakeResult:
    def __init__(self, df):
        self._df = df

    def fetchdf(self):
        return self._df.copy()

    df = fetchdf


class _Conn:
    """Stands in for a DuckDB connection: answers queries from recordings
    (replay) or runs and records them (record)."""

    def __init__(self, real, store, mode, hooks=None):
        self._real = real
        self._store = store
        self._mode = mode
        self._hooks = hooks

    def execute(self, sql, *a, **kw):
        key = sql if isinstance(sql, str) else repr(sql)
        if self._mode == "replay":
            if key in self._store:
                return _FakeResult(pickle.loads(self._store[key]))
            # a query the pinned commit never ran: run it for real
            if self._real is None:
                raise Diverge("duckdb query not recorded")
            with self._hooks.spending():
                df = self._real.execute(sql, *a, **kw).fetchdf()
            return _FakeResult(df)
        r = self._real.execute(sql, *a, **kw)
        df = r.fetchdf()
        self._store[key] = pickle.dumps(df)
        return _FakeResult(df)

    def __getattr__(self, name):
        return getattr(self._real, name)


class Hooks:
    """Installs the executor and DuckDB stand-ins for one test at a time."""

    def __init__(self, data, mode, budget=float("inf")):
        self.data = data  # {"exec": {key: bytes}, "duck": {sql: bytes}}
        self.mode = mode
        self.budget = budget  # cpu seconds allowed for real fallbacks per test
        self.spent = 0.0
        self.active = False
        self._saved = []
        self._last = []

    def spending(self):
        import contextlib
        import time

        hooks = self

        @contextlib.contextmanager
        def cm():
            if hooks.spent > hooks.budget:
                raise Diverge("replay fallback budget spent")
            t0 = time.process_time()
            try:
                yield
            finally:
                hooks.spent += time.process_time() - t0

        return cm()

    def install(self, test):
        import sys

        import sqlglot.executor as ex
        import sqlglot.executor.python as py

        hooks = self
        last = self._last
        orig_opt = ex.optimize

        def optimize(*a, **kw):
            r = orig_opt(*a, **kw)
            last.append(r)
            return r

        orig_exec = py.PythonExecutor.execute

        def execute(self_, plan):
            expr = last.pop() if last else None
            key = exec_key(expr, self_) if expr is not None else None
            store = hooks.data["exec"]
            if hooks.mode == "replay":
                if key is not None and key in store:
                    return pickle.loads(store[key])
                # an input the pinned commit never produced: execute it (the
                # executor's own code is unchanged when replay is used)
                with hooks.spending():
                    return orig_exec(self_, plan)
            r = orig_exec(self_, plan)
            if key is not None:
                store[key] = pickle.dumps(r)
            return r

        self._saved = [(ex, "optimize", ex.optimize), (py.PythonExecutor, "execute", orig_exec)]
        ex.optimize = optimize
        py.PythonExecutor.execute = execute
        cls = type(test)
        for attr in ("tpch_conn", "tpcds_conn"):
            if attr in cls.__dict__:
                real = cls.__dict__[attr]
                self._saved.append((cls, attr, real))
                setattr(cls, attr, _Conn(real, self.data["duck"], self.mode, self))
        self.spent = 0.0
        self.active = True

    def uninstall(self):
        for obj, name, val in reversed(self._saved):
            setattr(obj, name, val)
        self._saved = []
        self._last.clear()
        self.active = False


def light_setup(test_cls):
    """setUpClass for TestExecutor when every one of its tests is replayed and
    nothing is executed for real: the queries and table names are loaded as
    usual, the table rows and DuckDB views (only the executor and DuckDB read
    them, and both are answered from recordings) are not."""
    import sys

    mod = sys.modules[test_cls.__module__]
    saved = (test_cls.__dict__["setUpClass"], test_cls.__dict__["tearDownClass"])

    def setUpClass(cls):
        cls.tpch_conn = None
        cls.tpcds_conn = None
        cls.tpch_tables = {t: mod.Table(columns=c, rows=[]) for t, c in mod.TPCH_SCHEMA.items()}
        cls.tpcds_tables = {t: mod.Table(columns=c, rows=[]) for t, c in mod.TPCDS_SCHEMA.items()}
        cls.cache = {}
        cls.tpch_sqls = list(mod.load_sql_fixture_pairs("optimizer/tpc-h/tpc-h.sql"))
        cls.tpcds_sqls = list(mod.load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))

    def tearDownClass(cls):
        pass

    test_cls.setUpClass = classmethod(setUpClass)
    test_cls.tearDownClass = classmethod(tearDownClass)

    def restore():
        test_cls.setUpClass, test_cls.tearDownClass = saved

    return restore
