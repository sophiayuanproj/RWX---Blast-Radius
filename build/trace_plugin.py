"""pytest plugin that records, per test, what each test touches in sqlglot.

Run from the sqlglot checkout root:
    python -m pytest -p trace_plugin -p no:cacheprovider tests

Records, per test nodeid and phase:
  * code objects started (sys.monitoring PY_START, re-armed at every boundary)
  * class attribute reads on sqlglot classes (via recording descriptors)
  * files opened and directories listed (audit hook)
Output: $TRACE_OUT (pickle).
"""

import enum
import importlib
import os
import pickle
import pkgutil
import sys
import types

import pytest

ROOT = os.getcwd()
MON = sys.monitoring
TOOL = 4
OUT = os.environ.get("TRACE_OUT", "/tmp/trace.pkl")
TRACK_ATTRS = os.environ.get("TRACK_ATTRS", "1") == "1"


class Bucket:
    __slots__ = ("codes", "reads", "opens", "lists", "lines", "keys")

    def __init__(self):
        self.keys = set()
        self.lines = set()
        self.codes = set()
        self.reads = set()
        self.opens = set()
        self.lists = set()


buckets = {}
cur = Bucket()
buckets["<preimport>"] = cur
_suspended = False


def _switch(key):
    global cur
    b = buckets.get(key)
    if b is None:
        b = buckets[key] = Bucket()
    cur = b
    MON.restart_events()


DISABLE = MON.DISABLE


def _on_start(code, offset):
    cur.codes.add(code)
    return DISABLE


_SQ_PREFIX = (os.path.join(ROOT, "sqlglot") + os.sep, os.path.join(ROOT, "tests") + os.sep)


def _on_line(code, line):
    if code.co_filename.startswith(_SQ_PREFIX):
        cur.lines.add((code.co_filename, line))
    return DISABLE


def _audit(event, args):
    if event == "open":
        path = args[0]
        if isinstance(path, (str, bytes, os.PathLike)):
            try:
                p = os.fsdecode(path)
            except Exception:
                return
            if p.endswith(".pyc"):
                return
            cur.opens.add(os.path.abspath(p))
    elif event in ("os.listdir", "os.scandir"):
        path = args[0] if args else "."
        if path is None:
            path = "."
        if isinstance(path, (str, bytes, os.PathLike)):
            try:
                cur.lists.add(os.path.abspath(os.fsdecode(path)))
            except Exception:
                pass
    elif event == "glob.glob":
        cur.lists.add("GLOB:" + str(args[0]))


class _Rec:
    """Non-data descriptor standing in for a class attribute's value."""

    __slots__ = ("v", "k")

    def __init__(self, v, k):
        self.v = v
        self.k = k

    def __get__(self, obj, typ=None):
        cur.reads.add((self.k, typ))
        return self.v


ATTR_KEYS = []  # index -> "module:qualname.attr"
ATTR_INDEX = {}  # (id(class), attr) -> index
_ALL = object()


_SUSPEND = [0]


def _kr(k, key):
    if _SUSPEND[0]:
        return
    try:
        cur.keys.add((k, key))
    except TypeError:
        cur.keys.add((k, _ALL))


def _kall(k):
    if _SUSPEND[0]:
        return
    cur.keys.add((k, _ALL))


class _RecDict(dict):
    """dict recording which keys are looked up (and when it is used whole)."""

    __slots__ = ("_k",)

    def __getitem__(self, key):
        _kr(self._k, key)
        return dict.__getitem__(self, key)

    def get(self, key, default=None):
        _kr(self._k, key)
        return dict.get(self, key, default)

    def __contains__(self, key):
        _kr(self._k, key)
        return dict.__contains__(self, key)

    def setdefault(self, key, default=None):
        _kr(self._k, key)
        return dict.setdefault(self, key, default)

    def pop(self, key, *a):
        _kr(self._k, key)
        return dict.pop(self, key, *a)

    def __setitem__(self, key, value):
        _kr(self._k, key)
        return dict.__setitem__(self, key, value)

    def __delitem__(self, key):
        _kr(self._k, key)
        return dict.__delitem__(self, key)


def _whole(name):
    base = getattr(dict, name)

    def f(self, *a, **kw):
        _kall(self._k)
        return base(self, *a, **kw)

    f.__name__ = name
    return f


for _n in ("__iter__", "keys", "items", "values", "__len__", "__eq__", "__ne__", "__repr__", "copy",
           "__or__", "__ror__", "__ior__", "__reduce_ex__", "__reversed__", "update", "popitem", "clear",
           "__bool__" if hasattr(dict, "__bool__") else "__len__"):
    setattr(_RecDict, _n, _whole(_n))


def _is_plain(v):
    if isinstance(v, (types.FunctionType, staticmethod, classmethod, property,
                      types.MemberDescriptorType, types.GetSetDescriptorType,
                      types.BuiltinFunctionType, types.MethodDescriptorType,
                      types.WrapperDescriptorType)):
        return False
    if hasattr(type(v), "__get__") and not isinstance(v, type):
        return False  # other descriptors
    return True


def _install_attr_tracking():
    seen = set()
    count = 0

    def visit(cls, modname):
        nonlocal count
        if id(cls) in seen:
            return
        seen.add(id(cls))
        if issubclass(cls, enum.Enum) or issubclass(cls, BaseException):
            return
        for name, v in list(cls.__dict__.items()):
            if name.startswith("__") and name.endswith("__"):
                continue
            if isinstance(v, type) and v.__module__ == modname and v.__qualname__.startswith(cls.__qualname__ + "."):
                visit(v, modname)
            if not _is_plain(v):
                continue
            k = len(ATTR_KEYS)
            ATTR_KEYS.append(f"{modname}:{cls.__qualname__}.{name}")
            if type(v) is dict and os.environ.get("TRACK_KEYS", "1") == "1":
                rd = _RecDict(v)
                rd._k = k
                v = rd
            ATTR_INDEX[(id(cls), name)] = k
            try:
                setattr(cls, name, _Rec(v, k))
                count += 1
            except (TypeError, AttributeError):
                ATTR_KEYS[k] = None

    for modname, mod in list(sys.modules.items()):
        if not modname.startswith("sqlglot") or mod is None:
            continue
        for v in list(vars(mod).values()):
            if isinstance(v, type) and v.__module__ == modname:
                visit(v, modname)
    print(f"[trace] tracking {count} class attributes", file=sys.stderr)


def _install_dispatch_tracking():
    """Generator dispatch tables are built by iterating TRANSFORMS; record the
    lookups into them as key reads of the TRANSFORMS that supplied them."""
    import sqlglot.generator as g

    orig = g._build_dispatch

    def build(cls):
        _SUSPEND[0] += 1
        try:
            d = orig(cls)
        finally:
            _SUSPEND[0] -= 1
        owner = next((c for c in cls.__mro__ if "TRANSFORMS" in c.__dict__), None)
        k = ATTR_INDEX.get((id(owner), "TRANSFORMS")) if owner is not None else None
        if k is None:
            return d
        rd = _RecDict(d)
        rd._k = k
        return rd

    g._build_dispatch = build


def _preimport():
    import sqlglot

    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if m.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(m.name)
        except Exception as e:  # pragma: no cover
            print("[trace] import failed", m.name, e, file=sys.stderr)


def pytest_configure(config):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import brpool

    brpool.install()
    MON.use_tool_id(TOOL, "brtrace")
    MON.register_callback(TOOL, MON.events.PY_START, _on_start)
    MON.register_callback(TOOL, MON.events.LINE, _on_line)
    MON.set_events(TOOL, MON.events.PY_START | MON.events.LINE)
    sys.addaudithook(_audit)
    _switch("<import>")
    _preimport()
    if TRACK_ATTRS:
        _install_attr_tracking()
        _install_dispatch_tracking()
    _switch("<postimport>")


def pytest_collection_modifyitems(session, config, items):
    if os.environ.get("TRACE_REVERSE") == "1":
        items.reverse()


@pytest.hookimpl(hookwrapper=True)
def pytest_pycollect_makemodule(module_path, parent):
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    path = getattr(collector, "path", None)
    if path is not None and str(path).endswith(".py"):
        _switch("collect:" + os.path.relpath(str(path), ROOT))
    yield
    _switch("<between>")


_CACHES = []


def _clear_caches():
    if not _CACHES:
        for modname, mod in list(sys.modules.items()):
            if modname.startswith("sqlglot") and mod is not None:
                for k, v in list(vars(mod).items()):
                    if "CACHE" in k.upper() and isinstance(v, dict):
                        _CACHES.append(v)
    for c in _CACHES:
        c.clear()


def _phase(item, phase):
    if phase == "setup":
        _clear_caches()
    _switch(f"{phase}:{item.nodeid}")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _phase(item, "setup")
    yield
    _switch("<between>")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _phase(item, "call")
    yield
    _switch("<between>")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    _phase(item, "teardown")
    yield
    _switch("<between>")


_KFP = None


def _keyfp(key):
    global _KFP
    if _KFP is None:
        import brfp

        _KFP = brfp._FP()
    return _KFP.h(key)


def _cname(t):
    if t is None:
        return None
    return f"{getattr(t, '__module__', '?')}:{getattr(t, '__qualname__', '?')}"


def _code_key(code):
    return (code.co_filename, code.co_qualname, code.co_firstlineno)


def pytest_sessionfinish(session, exitstatus):
    MON.set_events(TOOL, 0)
    out = {}
    for key, b in buckets.items():
        codes = set()
        for c in b.codes:
            f = c.co_filename
            if f.startswith(ROOT):
                codes.add((os.path.relpath(f, ROOT), c.co_qualname, c.co_firstlineno))
        out[key] = {
            "codes": codes,
            "reads": {(ATTR_KEYS[k], _cname(t)) for k, t in b.reads if ATTR_KEYS[k]},
            "opens": {os.path.relpath(p, ROOT) for p in b.opens if p.startswith(ROOT)},
            "lists": {(os.path.relpath(p, ROOT) if p.startswith(ROOT) else p) for p in b.lists},
            "lines": {(os.path.relpath(f, ROOT), ln) for f, ln in b.lines},
            "keys": {(ATTR_KEYS[k], "*" if key is _ALL else _keyfp(key)) for k, key in b.keys if ATTR_KEYS[k]},
        }
    outcomes = {}
    with open(OUT, "wb") as f:
        pickle.dump({"buckets": out, "attr_keys": ATTR_KEYS}, f)
    print(f"[trace] wrote {OUT} with {len(out)} buckets", file=sys.stderr)
