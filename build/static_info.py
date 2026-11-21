"""Static facts about the pinned checkout used by the selector:
  rdeps: module -> modules that import it at module level (transitively)
  base_import: modules loaded by `import sqlglot`
  thirdparty: non-sqlglot modules the test files import (pre-imported once)
"""
import ast, json, os, subprocess, sys
root, tests_json, out = sys.argv[1:4]

def modname(rel):
    m = rel[:-3].replace("/", ".")
    return m[:-9] if m.endswith(".__init__") else m

mods = {}
for dp, dn, fn in os.walk(os.path.join(root, "sqlglot")):
    dn[:] = [d for d in dn if d != "__pycache__"]
    for f in fn:
        if f.endswith(".py"):
            rel = os.path.relpath(os.path.join(dp, f), root)
            mods[modname(rel)] = rel

deps = {m: set() for m in mods}
for m, rel in mods.items():
    tree = ast.parse(open(os.path.join(root, rel)).read())
    pkg = m if rel.endswith("__init__.py") else m.rsplit(".", 1)[0]
    def add(name):
        # the module and its parents (importing a.b.c runs a and a.b)
        parts = name.split(".")
        for i in range(1, len(parts) + 1):
            n = ".".join(parts[:i])
            if n in mods and n != m:
                deps[m].add(n)
    def visit(node, top):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue  # lazy imports run later, not at import time
            if isinstance(ch, ast.Import):
                for a in ch.names:
                    add(a.name)
            elif isinstance(ch, ast.ImportFrom):
                if ch.level:
                    base = pkg.split(".")
                    base = base[: len(base) - (ch.level - 1)]
                    name = ".".join(base + ([ch.module] if ch.module else []))
                else:
                    name = ch.module or ""
                add(name)
                for a in ch.names:
                    add(name + "." + a.name)
            visit(ch, top)
    visit(tree, True)

rdeps = {m: set() for m in mods}
for m, ds in deps.items():
    for d in ds:
        rdeps[d].add(m)
closure = {}
for m in mods:
    seen = set(); stack = [m]
    while stack:
        x = stack.pop()
        for y in rdeps.get(x, ()):
            if y not in seen:
                seen.add(y); stack.append(y)
    closure[m] = sorted(seen)

code = "import sys,json; import sqlglot; print(json.dumps(sorted(m for m in sys.modules if m.startswith('sqlglot'))))"
r = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, env={**os.environ, "PYTHONPATH": root})
base_import = json.loads(r.stdout.strip().splitlines()[-1])

tests = json.load(open(tests_json))["tests"]
files = sorted({t["file"] for t in tests})
code = ("import sys,json,importlib\nbefore=set(sys.modules)\n"
        "for f in sys.argv[1:]:\n    importlib.import_module(f[:-3].replace('/','.'))\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] not in ('sqlglot','tests') and m not in before)))")
r = subprocess.run([sys.executable, "-c", code] + files, cwd=root, capture_output=True, text=True, env={**os.environ, "PYTHONPATH": root})
third = json.loads(r.stdout.strip().splitlines()[-1])
# keep top-level-ish modules; importing them pulls in the rest
tops = sorted({m for m in third if not m.startswith("_")})
json.dump({"rdeps": closure, "base_import": base_import, "thirdparty": tops, "modfile": mods}, open(out, "w"))
print(len(mods), "modules;", len(base_import), "loaded by import sqlglot;", len(tops), "third-party", file=sys.stderr)
