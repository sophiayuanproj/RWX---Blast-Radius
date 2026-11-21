"""Turn the tracer's raw buckets into the selector's maps.

usage: build_maps.py TRACE_PKL TESTS_JSON SQLGLOT_ROOT OUT_PKL [IMPORTS_JSON]
"""

import json
import os
import pickle
import sys
import types
from collections import defaultdict

trace_pkl, tests_json, root, out_pkl = sys.argv[1:5]
imports_json = sys.argv[5] if len(sys.argv) > 5 else None
local_json = sys.argv[6] if len(sys.argv) > 6 else None
static_json = sys.argv[7] if len(sys.argv) > 7 else None

raws = [pickle.load(open(p, "rb")) for p in trace_pkl.split(",")]
raw = raws[0]
buckets = {}
for r in raws:
    for k, b in r["buckets"].items():
        if k not in buckets:
            buckets[k] = {f: set(v) for f, v in b.items()}
        else:
            for f, v in b.items():
                buckets[k][f] |= v
# code objects whose per-test coverage depends on test order (lazy loading,
# shared state): their candidates widen to every test running code of the
# same enclosing scope
order_sensitive = set()
if len(raws) > 1:
    def per_test_codes(r):
        out = defaultdict(set)
        for k, b in r["buckets"].items():
            ph, _, rest = k.partition(":")
            if ph in ("setup", "call", "teardown"):
                out[rest] |= b["codes"]
        return out
    pts = [per_test_codes(r) for r in raws]
    from collections import Counter
    cnt = Counter()
    for t in pts[0]:
        for other in pts[1:]:
            for c in pts[0][t] ^ other.get(t, set()):
                cnt[c] += 1
    order_sensitive = {c for c, n in cnt.items() if n >= 3}
    print("order sensitive codes:", sorted(order_sensitive), file=sys.stderr)
tests = json.load(open(tests_json))["tests"]
num = {t["id"]: t["number"] for t in tests}


def cls_of(nodeid):
    return nodeid.rsplit("::", 1)[0]


# per-test aggregation ---------------------------------------------------------
FIELDS = ("codes", "reads", "opens", "lists", "lines", "keys")
for b in buckets.values():
    b.setdefault("lines", set())
    b.setdefault("keys", set())
per_test = defaultdict(lambda: {f: set() for f in FIELDS})
class_fixture = defaultdict(lambda: {f: set() for f in FIELDS})
collect = {}
outer = {}  # front-end coverage of replayable tests (see brreplay)
execr = {}  # executor coverage of replayable tests
data_region = {}  # table-data preparation inside replayable tests' calls
unknown = set()
import dis  # noqa: E402

line_tests_pre = defaultdict(int)
for key, b in buckets.items():
    phase, _, rest = key.partition(":")
    if phase in ("call", "outer", "execr", "data") and rest in num:
        for fl in b.get("lines", ()):
            line_tests_pre[fl] |= 1 << num[rest]
for key, b in buckets.items():
    phase, _, rest = key.partition(":")
    if phase in ("setup", "call", "teardown"):
        if rest not in num:
            unknown.add(rest)
            continue
        dst = per_test[num[rest]]
        for f in dst:
            dst[f] |= b[f]
        if phase in ("setup", "teardown"):
            cf = class_fixture[cls_of(rest)]
            for f in cf:
                cf[f] |= b[f]
    elif phase == "collect":
        collect[rest] = b
    elif phase == "outer":
        outer[rest] = b
    elif phase == "execr":
        execr[rest] = b
    elif phase == "data":
        data_region[rest] = b
    elif phase not in ("<preimport>", "<import>", "<postimport>", "<between>") and not key.startswith("<"):
        raise SystemExit(f"unhandled trace bucket {key!r}: coverage would be lost")
if unknown:
    print("tests not in tests.json:", sorted(unknown), file=sys.stderr)

# fixture files a class fixture loads into a class attribute
# (cls.X = ...("path/file.sql")...): only tests reading X depend on them
import ast as _ast

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from brlib import all_codes  # noqa: E402

setup_links = {}  # (test file, class name) -> {fixture rel path: attr}
for tf in sorted({t["file"] for t in tests}):
    try:
        tree = _ast.parse(open(os.path.join(root, tf), encoding="utf-8").read())
    except (OSError, SyntaxError):
        continue
    for node in tree.body:
        if not isinstance(node, _ast.ClassDef):
            continue
        for fn in node.body:
            if isinstance(fn, _ast.FunctionDef) and fn.name == "setUpClass":
                for st in _ast.walk(fn):
                    if isinstance(st, _ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], _ast.Attribute):
                        strs = [c.value for c in _ast.walk(st.value) if isinstance(c, _ast.Constant) and isinstance(c.value, str)]
                        for sv in strs:
                            if sv.endswith((".sql", ".json", ".csv", ".txt")):
                                setup_links.setdefault((tf, node.name), {})["tests/fixtures/" + sv] = st.targets[0].attr
attr_line_readers = defaultdict(int)  # (test file, attr) -> tests running a load of attr there
for tf in {k[0] for k in setup_links}:
    try:
        co = compile(open(os.path.join(root, tf), encoding="utf-8").read(), tf, "exec", dont_inherit=True)
    except (OSError, SyntaxError):
        continue
    wanted = {a for (f2, _c), m in setup_links.items() if f2 == tf for a in m.values()}
    for c in all_codes(co):
        for ins in dis.get_instructions(c):
            if ins.opname in ("LOAD_ATTR", "LOAD_METHOD") and ins.argval in wanted and ins.positions and ins.positions.lineno:
                attr_line_readers[(tf, ins.argval)] |= line_tests_pre.get((tf, ins.positions.lineno), 0)
linked_setup_files = {}
for (tf, cname), m in setup_links.items():
    for fx, attr in m.items():
        linked_setup_files[(f"{tf[:-3].replace('/', '.')}.{cname}", fx)] = attr_line_readers.get((tf, attr), None)
print("setup-loaded fixtures:", {k[1]: (bin(v).count("1") if v is not None else None) for k, v in linked_setup_files.items()}, file=sys.stderr)

for t in tests:
    n = t["number"]
    d = per_test[n]
    cf = class_fixture.get(cls_of(t["id"]))
    if cf:
        cname = t["file"][:-3].replace("/", ".") + "." + cls_of(t["id"]).rsplit("::", 1)[1]
        for f in d:
            if f == "opens":
                for fx in cf[f]:
                    readers = linked_setup_files.get((cname, fx))
                    if readers is None or (readers >> n) & 1:
                        d[f].add(fx)
            else:
                d[f] |= cf[f]
    cb = collect.get(t["file"])
    if cb:
        for f in d:
            d[f] |= cb[f]
    else:
        print("no collect bucket for", t["file"], file=sys.stderr)

# replayable tests: their own number gets everything, a pseudo number (after
# the real ones) gets everything except the front end
replay = {}
replay_exec = {}
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import brreplay  # noqa: E402

next_pseudo = len(tests)
for tid in (set(execr) | set(data_region)) - set(brreplay.REPLAY_TESTS):
    raise SystemExit(f"executor region traced for a test without replay: {tid}")
for tid in brreplay.REPLAY_TESTS:
    if tid not in num or tid not in outer:
        print("no outer coverage for", tid, file=sys.stderr)
        if tid in num and tid in execr:
            for f in per_test[num[tid]]:
                per_test[num[tid]][f] |= execr[tid].get(f, set())
        continue
    n = num[tid]
    p = next_pseudo
    next_pseudo += 1
    replay[n] = p
    # what replay cannot run for real: the table data (class fixtures, the
    # module's import, ensure_tables). The rest of the test runs for real
    # during a replay, so changes there need no special care.
    keep = {f: set() for f in per_test[n]}
    cf = class_fixture.get(cls_of(tid))
    cb = collect.get(tid.split("::", 1)[0])
    cname = tid.split("::", 1)[0][:-3].replace("/", ".") + "." + tid.split("::")[1]
    for src in (cf, cb, data_region.get(tid)):
        if src:
            for f in keep:
                keep[f] |= src.get(f, set())
    # query files loaded into class attributes reach the replayed parts only
    # through their SQL, which keys the recordings
    keep["opens"] = {fx for fx in keep["opens"] if (cname, fx) not in linked_setup_files}
    per_test[p] = keep
    for f in per_test[n]:
        per_test[n][f] |= outer[tid].get(f, set())
        if tid in data_region:
            per_test[n][f] |= data_region[tid].get(f, set())
    if tid in execr:
        # a second pseudo test holds the executor region alone
        p2 = next_pseudo
        next_pseudo += 1
        replay_exec[n] = p2
        per_test[p2] = {f: set(execr[tid].get(f, set())) for f in per_test[n]}
        for f in per_test[n]:
            per_test[n][f] |= execr[tid].get(f, set())
    print(f"replay {tid}: inner codes {len(per_test[p]['codes'])}, all {len(per_test[n]['codes'])}", file=sys.stderr)

missing = [t["id"] for t in tests if not per_test[t["number"]]["codes"]]
if missing:
    print("tests with no trace:", missing[:10], len(missing), file=sys.stderr)

# inverted indexes -------------------------------------------------------------
code_tests = defaultdict(int)
line_tests = defaultdict(int)
key_tests = defaultdict(int)  # (attr key, key fp or "*") -> bits
keyany_tests = defaultdict(int)  # attr key -> bits of tests with any recorded key use
read_tests = defaultdict(int)  # owner attr key -> bits
via_tests = defaultdict(int)  # via class -> bits
attr_via_tests = defaultdict(int)  # (class read through, attribute name) -> bits
open_tests = defaultdict(int)
list_tests = defaultdict(int)
for n, d in per_test.items():
    bit = 1 << n
    for c in d["codes"]:
        code_tests[c] |= bit
    for k, via in d["reads"]:
        read_tests[k] |= bit
        if via:
            via_tests[via] |= bit
            attr_via_tests[(via, k.rsplit(".", 1)[1])] |= bit
    for p in d["opens"]:
        open_tests[p] |= bit
    for p in d["lists"]:
        list_tests[p] |= bit
    for fl in d["lines"]:
        line_tests[fl] |= bit
    for ak, kf in d["keys"]:
        key_tests[(ak, kf)] |= bit
        keyany_tests[ak] |= bit

# widen order-sensitive code objects
for c in order_sensitive:
    f, q, _l = c
    scope = q.rsplit(".", 1)[0] if "." in q else None
    bits = 0
    for (f2, q2, _l2), b in code_tests.items():
        if f2 == f and (scope is None or q2.startswith(scope + ".") or q2 == scope):
            bits |= b
    code_tests[c] |= bits

import_codes = set()
for k in ("<preimport>", "<import>"):
    if k in buckets:
        import_codes |= buckets[k]["codes"]
outside = set()
for k in ("<postimport>", "<between>"):
    if k in buckets:
        outside |= {c for c in buckets[k]["codes"] if c[0].startswith("sqlglot/")}
if outside:
    print("sqlglot code run outside tests:", len(outside), sorted(outside)[:5], file=sys.stderr)

# static code index: every code object in pristine sources ---------------------
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from brlib import all_codes, is_function_code  # noqa: E402
import dis  # noqa: E402

GLOBAL_OPS = {"LOAD_GLOBAL", "LOAD_NAME", "LOAD_FROM_DICT_OR_GLOBALS", "STORE_GLOBAL", "DELETE_GLOBAL"}
NAME_OPS = {"LOAD_GLOBAL", "LOAD_NAME", "LOAD_ATTR", "LOAD_METHOD", "LOAD_FROM_DICT_OR_GLOBALS",
            "IMPORT_FROM", "LOAD_SUPER_ATTR", "STORE_ATTR", "DELETE_ATTR", "STORE_GLOBAL", "DELETE_GLOBAL",
            "LOAD_FROM_DICT_OR_DEREF"}

name_tests = defaultdict(int)
name_global_tests = defaultdict(int)  # (file, name) -> tests running a global load of name there
name_attr_tests = defaultdict(int)  # name -> tests running an attribute load of name anywhere
lazy_import_tests = defaultdict(int)
lazy_mod = ""
class_code_tests = defaultdict(int)
code_info = {}
for base in ("sqlglot", "tests"):
    for dp, dn, fn in os.walk(os.path.join(root, base)):
        dn[:] = [d for d in dn if d != "__pycache__"]
        for f in fn:
            if not f.endswith(".py"):
                continue
            full = os.path.join(dp, f)
            rel = os.path.relpath(full, root)
            try:
                co = compile(open(full, encoding="utf-8").read(), rel, "exec", dont_inherit=True)
            except SyntaxError:
                continue
            for c in all_codes(co):
                key = (rel, c.co_qualname, c.co_firstlineno)
                bits = code_tests.get(key, 0)
                if bits and is_function_code(c):
                    # imports run inside functions: module -> tests running them
                    last_const = None
                    for ins in dis.get_instructions(c):
                        if ins.opname == "LOAD_CONST":
                            last_const = ins.argval
                        if ins.opname == "IMPORT_NAME" and isinstance(ins.argval, str):
                            name = ins.argval
                            if isinstance(last_const, int) and last_const > 0:
                                pkg = rel[:-3].replace("/", ".").rsplit(".", last_const)[0]
                                name = pkg + ("." + name if name else "")
                            ln = ins.positions.lineno if ins.positions else None
                            lb = line_tests.get((rel, ln)) if ln else None
                            tb = bits if lb is None else (lb & bits)
                            parts = name.split(".")
                            for i in range(1, len(parts) + 1):
                                lazy_import_tests[".".join(parts[:i])] |= tb
                            lazy_mod = name
                        elif ins.opname == "IMPORT_FROM" and isinstance(ins.argval, str):
                            # `from pkg import submodule`
                            ln = ins.positions.lineno if ins.positions else None
                            lb = line_tests.get((rel, ln)) if ln else None
                            tb = bits if lb is None else (lb & bits)
                            lazy_import_tests[lazy_mod + "." + ins.argval] |= tb
                if bits:
                    if is_function_code(c) and key not in order_sensitive:
                        # who ran the lines that load each name
                        seen_names = set()
                        for ins in dis.get_instructions(c):
                            if ins.opname in NAME_OPS and isinstance(ins.argval, str):
                                ln = ins.positions.lineno if ins.positions else None
                                lb = line_tests.get((rel, ln), None) if ln else None
                                tb = bits if lb is None else (lb & bits)
                                name_tests[ins.argval] |= tb
                                if ins.opname in GLOBAL_OPS:
                                    name_global_tests[(rel, ins.argval)] |= tb
                                else:
                                    name_attr_tests[ins.argval] |= tb
                                seen_names.add(ins.argval)
                        for nm in c.co_names:
                            if nm not in seen_names:
                                name_tests[nm] |= bits
                                name_global_tests[(rel, nm)] |= bits
                                name_attr_tests[nm] |= bits
                    else:
                        for nm in c.co_names:
                            name_tests[nm] |= bits
                            name_global_tests[(rel, nm)] |= bits
                            name_attr_tests[nm] |= bits
                    if is_function_code(c) and "." in c.co_qualname:
                        q = c.co_qualname
                        # every enclosing class-ish prefix
                        parts = q.split(".")
                        for i in range(1, len(parts)):
                            if parts[i - 1] == "<locals>":
                                break
                            class_code_tests[(rel, ".".join(parts[:i]))] |= bits

# class hierarchy from pristine import ------------------------------------------
sys.path.insert(0, root)
import brfp  # noqa: E402

brfp.import_all()
reg = brfp.class_registry()
descendants = {}
class_file = {}
for modname in brfp.sqlglot_modules():
    mod = sys.modules[modname]
    f = getattr(mod, "__file__", None)
    rel = os.path.relpath(f, root) if f else None
    for qual, cls in brfp.module_classes(mod):
        key = f"{modname}:{qual}"
        class_file[key] = rel
        seen = []
        stack = [cls]
        ids = set()
        while stack:
            c = stack.pop()
            for s in type.__subclasses__(c) if isinstance(c, type) else []:
                if id(s) not in ids:
                    ids.add(id(s))
                    seen.append(reg.get(id(s), f"{s.__module__}:{s.__qualname__}"))
                    stack.append(s)
        descendants[key] = seen

touch = {}
for key in class_file:
    bits = via_tests.get(key, 0)
    rel = class_file[key]
    qual = key.split(":", 1)[1]
    bits |= class_code_tests.get((rel, qual), 0)
    # reads of attributes owned by the class
    touch[key] = bits
prefix_reads = defaultdict(int)
for k, bits in read_tests.items():
    owner = k.rsplit(".", 1)[0]
    prefix_reads[owner] |= bits
for key in touch:
    touch[key] |= prefix_reads.get(key, 0)

file_tests = defaultdict(int)
for t in tests:
    file_tests[t["file"]] |= 1 << t["number"]

test_imports = json.load(open(imports_json)) if imports_json else {}
module_import_tests = defaultdict(int)
for tf, mods in test_imports.items():
    for m in mods:
        module_import_tests[m] |= file_tests.get(tf, 0)

local = json.load(open(local_json)) if local_json else {"cpu": [None] * len(tests), "import": {}, "class_setup": {}}
_intern = {}


def intern(d):
    return {k: _intern.setdefault(v, v) for k, v in d.items()}


# per file: line -> bits
lines_by_file = defaultdict(dict)
for (f, ln), b in line_tests.items():
    lines_by_file[f][ln] = _intern.setdefault(b, b)
maps = {
    "line_tests": dict(lines_by_file),
    "local_cpu": local["cpu"],
    "class_setup": local["class_setup"],
    "module_import_cpu": local["import"],
    "n_tests": len(tests),
    "code_tests": intern(code_tests),
    "read_tests": dict(read_tests),
    "touch": touch,
    "descendants": descendants,
    "class_file": class_file,
    "name_tests": dict(name_tests),
    "open_tests": dict(open_tests),
    "list_tests": dict(list_tests),
    "import_codes": import_codes,
    "file_tests": dict(file_tests),
    "module_import_tests": dict(module_import_tests),
    "test_ids": [t["id"] for t in tests],
    "tracked": {k for k in raw["attr_keys"] if k},
    "order_sensitive": order_sensitive,
    "key_tests": intern(key_tests),
    "lazy_import_tests": dict(lazy_import_tests),
    "replay": replay,
    "attr_via_tests": intern(attr_via_tests),
    "replay_exec": replay_exec,
    "name_global_tests": intern(name_global_tests),
    "name_attr_tests": intern(name_attr_tests),
    "keyany_tests": intern(keyany_tests),
}
if static_json:
    st = json.load(open(static_json))
    maps["rdeps"] = st["rdeps"]
    maps["base_import"] = set(st["base_import"])
    maps["thirdparty"] = [m for m in st["thirdparty"] if "." not in m and m not in ("cython_runtime", "distutils")]
with open(out_pkl, "wb") as f:
    pickle.dump(maps, f, protocol=pickle.HIGHEST_PROTOCOL)
print("codes", len(code_tests), "reads", len(read_tests), "names", len(name_tests),
      "opens", len(open_tests), "size", os.path.getsize(out_pkl), file=sys.stderr)
