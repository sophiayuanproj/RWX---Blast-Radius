"""Stable fingerprints of sqlglot's module globals and class attributes.

Imported in a process that has sqlglot (pristine or patched) on sys.path.
fingerprint_all() returns {key: hash}, where keys are
  "G|module|name"          module global
  "A|module|Qual.name|attr" class attribute (own __dict__ only)
  "B|module|Qual"          class bases / metaclass
Function values are fingerprinted by identity of their code (file, qualname,
plus defaults, closure and annotations), not by their bytecode: bodies are
compared separately, by code-object diff.
"""

import enum
import hashlib
import importlib
import importlib.util
import pkgutil
import re
import sys
import types

sys.setrecursionlimit(max(sys.getrecursionlimit(), 5000))

_ATOMS = (str, int, float, complex, bool, bytes, type(None), type(Ellipsis))


class _FP:
    def __init__(self, unordered=False):
        self.unordered = unordered
        self.memo = {}
        self.stack = {}
        self.depth_of_cycle = []  # min stack depth referenced by a cycle, per frame

    def h(self, v, depth=0):
        if isinstance(v, _ATOMS):
            return f"{type(v).__name__}:{v!r}"
        i = id(v)
        m = self.memo.get(i)
        if m is not None:
            return m
        if i in self.stack:
            # back-reference: name the ancestor by its distance, which only
            # depends on the path from the root
            d = self.stack[i]
            if self.depth_of_cycle:
                self.depth_of_cycle[-1] = min(self.depth_of_cycle[-1], d)
            return f"<cycle:{depth - d}>"
        if depth > 150:
            if self.depth_of_cycle:
                self.depth_of_cycle[-1] = -1  # truncated: path dependent, never memoize
            return "<deep>"
        self.stack[i] = depth
        self.depth_of_cycle.append(depth + 1000)
        try:
            r = self._h(v, depth + 1)
        finally:
            del self.stack[i]
            lowest = self.depth_of_cycle.pop()
        if lowest < depth and self.depth_of_cycle:
            self.depth_of_cycle[-1] = min(self.depth_of_cycle[-1], lowest)
        if len(r) > 64:
            r = "#" + hashlib.blake2b(r.encode("utf-8", "surrogatepass"), digest_size=12).hexdigest()
        if lowest >= depth:
            # no reference to anything above us: safe to reuse anywhere
            self.memo[i] = r
        return r

    def _h(self, v, d):
        h = self.h
        if isinstance(v, enum.Enum):
            return f"E:{type(v).__module__}.{type(v).__qualname__}.{v.name}"
        if isinstance(v, type):
            return f"T:{v.__module__}.{v.__qualname__}"
        if isinstance(v, types.ModuleType):
            return f"M:{v.__name__}"
        if isinstance(v, (list, tuple)):
            parts = [h(x, d) for x in v]
            if self.unordered:
                parts.sort()
            return f"{type(v).__name__}[" + ",".join(parts) + "]"
        if isinstance(v, (set, frozenset)):
            return f"{type(v).__name__}{{" + ",".join(sorted(h(x, d) for x in v)) + "}"
        if isinstance(v, dict):
            items = sorted(h(k, d) + "=" + h(x, d) for k, x in v.items())
            return f"{type(v).__name__}(" + ",".join(items) + ")"
        if isinstance(v, types.FunctionType):
            c = v.__code__
            parts = [
                f"F:{c.co_filename.rsplit('/sqlglot/', 1)[-1]}:{c.co_qualname}",
                h(v.__defaults__, d),
                h(v.__kwdefaults__, d),
                h(getattr(v, "__annotations__", None), d),
            ]
            if v.__closure__:
                cells = []
                for cell in v.__closure__:
                    try:
                        cells.append(h(cell.cell_contents, d))
                    except ValueError:
                        cells.append("<empty>")
                parts.append("[" + ",".join(cells) + "]")
            extra = {k: x for k, x in vars(v).items() if k not in ("__wrapped__",)}
            if extra:
                parts.append(h(extra, d))
            w = getattr(v, "__wrapped__", None)
            if w is not None:
                parts.append(h(w, d))
            return "(" + "|".join(parts) + ")"
        if isinstance(v, (staticmethod, classmethod)):
            return f"{type(v).__name__}(" + h(v.__func__, d) + ")"
        if isinstance(v, property):
            return "property(" + ",".join(h(x, d) for x in (v.fget, v.fset, v.fdel)) + ")"
        if isinstance(v, types.MethodType):
            return "bound(" + h(v.__func__, d) + "," + h(v.__self__, d) + ")"
        if isinstance(v, (types.BuiltinFunctionType, types.BuiltinMethodType)):
            s = getattr(v, "__self__", None)
            own = "" if s is None or isinstance(s, types.ModuleType) else h(s, d)
            return f"BF:{getattr(v, '__module__', '')}.{v.__qualname__}({own})"
        if isinstance(v, re.Pattern):
            return f"re({v.pattern!r},{v.flags})"
        if isinstance(v, types.CodeType):
            return f"code:{v.co_qualname}"
        try:
            import functools

            if isinstance(v, functools.partial):
                return "partial(" + h(v.func, d) + "," + h(v.args, d) + "," + h(v.keywords, d) + ")"
        except Exception:
            pass
        t = type(v)
        name = f"O:{t.__module__}.{t.__qualname__}"
        tm = t.__module__ or ""
        if not (tm == "sqlglot" or tm.startswith("sqlglot.") or tm.startswith("tests")):
            # foreign object: shallow, so unrelated library state cannot leak in
            try:
                r = repr(v)
            except Exception:  # noqa: BLE001
                r = ""
            if " at 0x" in r or len(r) > 2000:
                r = re.sub(r" at 0x[0-9a-fA-F]+", "", r)[:2000]
            return name + ":" + r
        state = None
        dct = getattr(v, "__dict__", None)
        if isinstance(dct, dict):
            state = dict(dct)
        slots = []
        for k in t.__mro__:
            sl = k.__dict__.get("__slots__")
            if isinstance(sl, str):
                sl = (sl,)
            if sl:
                slots.extend(sl)
        if slots:
            state = state or {}
            for s in slots:
                if s in ("__dict__", "__weakref__"):
                    continue
                try:
                    state[s] = getattr(v, s)
                except Exception:
                    pass
        if state is not None:
            return name + h(state, d)
        r = repr(v)
        if " at 0x" in r:
            r = re.sub(r" at 0x[0-9a-fA-F]+", "", r)
        return name + ":" + r


def _tag(v):
    """F: callable defined in Python (code compared separately), T: class, D: data."""
    if isinstance(v, (types.FunctionType, staticmethod, classmethod, property)):
        return "F"
    if isinstance(v, type):
        return "T"
    return "D"


def func_qualnames(v):
    """(file, qualname) of the Python functions behind an F-tagged value."""
    out = []
    fs = []
    if isinstance(v, types.FunctionType):
        fs = [v]
    elif isinstance(v, (staticmethod, classmethod)):
        fs = [v.__func__]
    elif isinstance(v, property):
        fs = [f for f in (v.fget, v.fset, v.fdel) if f is not None]
    for f in fs:
        while hasattr(f, "__wrapped__"):
            f = f.__wrapped__
        c = getattr(f, "__code__", None)
        if c is not None:
            out.append((c.co_filename, c.co_qualname))
    return out


BASE_PREIMPORT = ["unittest", "unittest.mock", "concurrent.futures", "multiprocessing", "pickle"]


def preimport(names):
    """Import third-party modules the tests use, stdout silenced. Shared by
    the build (pristine fingerprints) and the selector so both see the same
    interpreter state."""
    import os

    devnull = os.open(os.devnull, os.O_WRONLY)
    saved = os.dup(1)
    os.dup2(devnull, 1)
    try:
        for m in BASE_PREIMPORT + list(names):
            try:
                importlib.import_module(m)
            except BaseException:  # noqa: BLE001
                pass
    finally:
        os.dup2(saved, 1)
        os.close(saved)
        os.close(devnull)


def module_names(prefix="sqlglot"):
    """Every module of the package, found on disk (no imports needed)."""
    import os

    spec = importlib.util.find_spec(prefix)
    roots = list(spec.submodule_search_locations or [])
    out = [prefix]
    for root in roots:
        for dp, dn, fn in os.walk(root, followlinks=True):
            dn[:] = sorted(d for d in dn if d != "__pycache__" and os.path.exists(os.path.join(dp, d, "__init__.py")))
            rel = os.path.relpath(dp, root)
            pkg = prefix if rel == "." else prefix + "." + rel.replace(os.sep, ".")
            if pkg != prefix:
                out.append(pkg)
            for f in sorted(fn):
                if f.endswith(".py") and f != "__init__.py":
                    out.append(pkg + "." + f[:-3])
    return out


def import_all(prefix="sqlglot"):
    import importlib.util  # noqa: F401

    errors = {}
    try:
        names = module_names(prefix)
    except BaseException as e:  # noqa: BLE001
        return {prefix: repr(e)}
    for name in names:
        if name.endswith("__main__"):
            continue
        # a package that failed takes its submodules with it: do not retry
        parent = name.rsplit(".", 1)[0] if "." in name else None
        while parent is not None:
            if parent in errors:
                break
            parent = parent.rsplit(".", 1)[0] if "." in parent else None
        if parent is not None:
            errors[name] = errors[parent]
            continue
        try:
            importlib.import_module(name)
        except BaseException as e:  # noqa: BLE001
            errors[name] = repr(e)
    return errors


def module_classes(mod):
    """Classes defined in mod, including nested ones, as (qualname, cls)."""
    out = []
    seen = set()
    modname = mod.__name__

    def visit(cls):
        if id(cls) in seen:
            return
        seen.add(id(cls))
        out.append((cls.__qualname__, cls))
        for v in list(cls.__dict__.values()):
            if (
                isinstance(v, type)
                and v.__module__ == modname
                and v.__qualname__.startswith(cls.__qualname__ + ".")
            ):
                visit(v)

    for v in list(vars(mod).values()):
        if isinstance(v, type) and v.__module__ == modname:
            visit(v)
    return out


def fingerprint_modules(modnames, unordered=False, only=None):
    fp = _FP(unordered)
    out = {}
    for modname in modnames:
        mod = sys.modules.get(modname)
        if mod is None:
            continue
        for name, v in list(vars(mod).items()):
            if name in ("__builtins__", "__cached__", "__file__", "__spec__", "__loader__", "__path__"):
                continue
            k = f"G|{modname}|{name}"
            if only is None or k in only:
                out[k] = (_tag(v), fp.h(v))
        for qual, cls in module_classes(mod):
            bases = ",".join(f"{b.__module__}.{b.__qualname__}" for b in cls.__bases__)
            if only is not None:
                pass
            else:
                out[f"B|{modname}|{qual}"] = ("B", bases + "|" + f"{type(cls).__module__}.{type(cls).__qualname__}")
            for name, v in list(cls.__dict__.items()):
                if name in ("__dict__", "__weakref__", "__module__", "__qualname__", "__firstlineno__", "__static_attributes__"):
                    continue
                k = f"A|{modname}|{qual}|{name}"
                if only is None or k in only:
                    out[k] = (_tag(v), fp.h(v))
    return out


def keyed_fp(v, fp=None):
    """{key fp: value fp} for a plain dict, else None."""
    if type(v) is not dict:
        return None
    fp = fp or _FP()
    out = {}
    for k, x in v.items():
        out[fp.h(k)] = fp.h(x)
    return out


def keyed_modules(modnames):
    """keyed_fp of every plain-dict class attribute."""
    fp = _FP()
    out = {}
    for modname in modnames:
        mod = sys.modules.get(modname)
        if mod is None:
            continue
        for qual, cls in module_classes(mod):
            for name, v in list(cls.__dict__.items()):
                if type(v) is dict and not (name.startswith("__") and name.endswith("__")):
                    out[f"A|{modname}|{qual}|{name}"] = keyed_fp(v, fp)
    return out


def sqlglot_modules():
    return sorted(m for m in sys.modules if m == "sqlglot" or m.startswith("sqlglot."))


if __name__ == "__main__":
    import os
    import pickle

    _maps = os.environ.get("BR_MAPS_PKL")
    if _maps:
        with open(_maps, "rb") as f:
            preimport(pickle.load(f).get("thirdparty", ()))
    errs = import_all()
    if errs:
        print("import errors", errs, file=sys.stderr)
    import os

    runs = []
    fps = fingerprint_modules(sqlglot_modules())
    unstable = set()
    # keys whose value depends on the hash seed (loop variables over sets)
    for seed in sys.argv[2:]:
        import subprocess

        r = subprocess.run([sys.executable, __file__, "-"], env={**os.environ, "PYTHONHASHSEED": seed},
                           capture_output=True)
        other = pickle.loads(r.stdout)
        unstable |= {k for k in set(fps) | set(other) if fps.get(k) != other.get(k)}
    if sys.argv[1] == "-":
        sys.stdout.buffer.write(pickle.dumps(fps))
    elif sys.argv[1] == "-u":
        sys.stdout.buffer.write(pickle.dumps(fingerprint_modules(sqlglot_modules(), unordered=True)))
    else:
        uf = fingerprint_modules(sqlglot_modules(), unordered=True, only=unstable)
        still = set()
        for seed in sys.argv[2:]:
            r = subprocess.run([sys.executable, __file__, "-u"], env={**os.environ, "PYTHONHASHSEED": seed},
                               capture_output=True)
            other = pickle.loads(r.stdout)
            still |= {k for k in uf if uf[k] != other.get(k)}
        if still:
            print("STILL UNSTABLE", sorted(still), file=sys.stderr)
        with open(sys.argv[1], "wb") as f:
            pickle.dump({"fps": fps, "unstable": unstable, "unordered": uf, "still": still,
                         "keyed": keyed_modules(sqlglot_modules())}, f)
        print(len(fps), "fingerprints,", len(unstable), "unstable", sorted(unstable), file=sys.stderr)
