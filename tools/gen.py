"""Generate varied mutation patches against a sqlglot checkout.

usage: gen.py SQLGLOT_CHECKOUT OUT_DIR N SEED
Writes OUT_DIR/commits/<id>.patch and OUT_DIR/kinds.json.
"""

import ast
import json
import os
import random
import re
import string
import subprocess
import sys

root, out, n, seed = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
rng = random.Random(seed)
os.makedirs(os.path.join(out, "commits"), exist_ok=True)


def git(*a):
    return subprocess.run(["git", *a], cwd=root, capture_output=True, text=True, check=True).stdout


def files(prefix, ext):
    res = []
    for dp, dn, fn in os.walk(os.path.join(root, prefix)):
        dn[:] = [d for d in dn if d != "__pycache__"]
        for f in fn:
            if f.endswith(ext):
                res.append(os.path.relpath(os.path.join(dp, f), root))
    return sorted(res)


PKG = files("sqlglot", ".py")
TESTS = files("tests", ".py")
FIXT = files("tests/fixtures", ".sql")


def read(p):
    return open(os.path.join(root, p), encoding="utf-8").read()


def write(p, s):
    open(os.path.join(root, p), "w", encoding="utf-8").write(s)


def compiles(s):
    try:
        compile(s, "x", "exec")
        return True
    except SyntaxError:
        return False


SWAPS = [(" == ", " != "), (" != ", " == "), (" and ", " or "), (" or ", " and "), (" is not ", " is "),
         (" is None", " is not None"), (" < ", " <= "), (" > ", " >= "), (" + 1", " + 2"), (" - 1", " - 2"),
         ("True", "False"), ("False", "True"), (" not in ", " in "), ("if not ", "if "), ("[0]", "[1]"),
         ("[-1]", "[0]"), ("min(", "max("), ("max(", "min("), ("upper()", "lower()"), ("lower()", "upper()")]


def m_swap(paths=PKG):
    for _ in range(200):
        p = rng.choice(paths)
        s = read(p)
        lines = s.split("\n")
        i = rng.randrange(len(lines))
        line = lines[i]
        if line.strip().startswith("#") or '"""' in line:
            continue
        opts = [(a, b) for a, b in SWAPS if a in line]
        if not opts:
            continue
        a, b = rng.choice(opts)
        idxs = [m.start() for m in re.finditer(re.escape(a), line)]
        k = rng.choice(idxs)
        lines[i] = line[:k] + b + line[k + len(a):]
        ns = "\n".join(lines)
        if compiles(ns):
            write(p, ns)
            return f"swap {a.strip()}->{b.strip()} {p}:{i+1}"
    return None


def m_comment(paths=PKG):
    p = rng.choice(paths)
    s = read(p)
    lines = s.split("\n")
    for _ in range(100):
        i = rng.randrange(len(lines))
        indent = re.match(r"\s*", lines[i]).group(0)
        text = rng.choice(["# TODO: revisit", "# Cheap check first", "# noqa", "# type: ignore",
                           "# pragma: no cover", "# Kept for backwards compatibility", "#: doc comment"])
        cand = lines[:i] + [indent + text] + lines[i:]
        ns = "\n".join(cand)
        if compiles(ns):
            write(p, ns)
            return f"comment {p}:{i+1}"
    return None


def m_fixture():
    p = rng.choice(FIXT)
    s = read(p)
    lines = s.split("\n")
    for _ in range(100):
        i = rng.randrange(len(lines))
        line = lines[i]
        if not line.strip():
            continue
        kind = rng.random()
        if kind < 0.6 and " " in line:
            words = line.split(" ")
            j = rng.randrange(len(words))
            words[j] = words[j] + rng.choice(["1", "_x", ""]) if rng.random() < 0.7 else words[j].upper()
            nl = " ".join(words)
        elif kind < 0.8:
            nl = "-- " + line if not line.startswith("--") else line[3:]
        else:
            nl = line + " "
        if nl == line:
            continue
        lines[i] = nl
        write(p, "\n".join(lines))
        return f"fixture {p}:{i+1}"
    return None


def m_delete_stmt(paths=PKG):
    for _ in range(200):
        p = rng.choice(paths)
        s = read(p)
        try:
            tree = ast.parse(s)
        except SyntaxError:
            continue
        stmts = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef,)):
                for st in node.body:
                    if isinstance(st, (ast.Assign, ast.Expr, ast.AugAssign, ast.Return)) and st.lineno == st.end_lineno:
                        if not (isinstance(st, ast.Expr) and isinstance(getattr(st, "value", None), ast.Constant)):
                            stmts.append(st)
        if not stmts:
            continue
        st = rng.choice(stmts)
        lines = s.split("\n")
        indent = re.match(r"\s*", lines[st.lineno - 1]).group(0)
        lines[st.lineno - 1] = indent + "pass"
        ns = "\n".join(lines)
        if compiles(ns):
            write(p, ns)
            return f"delete-stmt {p}:{st.lineno}"
    return None


def m_default():
    for _ in range(200):
        p = rng.choice(PKG)
        s = read(p)
        lines = s.split("\n")
        i = rng.randrange(len(lines))
        line = lines[i]
        if "def " not in line and not re.match(r"\s+\w+: .*=", line):
            continue
        m = list(re.finditer(r"(\w+)(: [\w\[\]|. ]+)? ?= ?(None|True|False|\d+)", line))
        if not m:
            continue
        mm = rng.choice(m)
        v = mm.group(3)
        nv = {"None": "0", "True": "False", "False": "True"}.get(v, str(int(v) + 1) if v.isdigit() else v)
        lines[i] = line[: mm.start(3)] + nv + line[mm.end(3):]
        ns = "\n".join(lines)
        if compiles(ns) and ns != s:
            write(p, ns)
            return f"default {p}:{i+1}"
    return None


def m_string_const(paths=PKG):
    for _ in range(300):
        p = rng.choice(paths)
        s = read(p)
        lines = s.split("\n")
        i = rng.randrange(len(lines))
        line = lines[i]
        m = list(re.finditer(r'"([A-Z_][A-Z_ ]{1,30})"', line))
        if not m or line.strip().startswith("#"):
            continue
        mm = rng.choice(m)
        word = mm.group(1)
        nw = word[:-1] if len(word) > 2 and rng.random() < 0.5 else word + "S"
        lines[i] = line[: mm.start(1)] + nw + line[mm.end(1):]
        ns = "\n".join(lines)
        if compiles(ns):
            write(p, ns)
            return f"string {p}:{i+1}"
    return None


def m_test_edit():
    for _ in range(300):
        p = rng.choice([t for t in TESTS if "/test_" in t])
        s = read(p)
        lines = s.split("\n")
        i = rng.randrange(len(lines))
        line = lines[i]
        m = list(re.finditer(r'"(SELECT [^"]{3,})"', line))
        if not m:
            continue
        mm = rng.choice(m)
        txt = mm.group(1)
        k = rng.randrange(7, len(txt) + 1)
        nt = txt[:k] + rng.choice([" ", "1", "x", ""]) + txt[k:]
        if nt == txt:
            continue
        lines[i] = line[: mm.start(1)] + nt + line[mm.end(1):]
        ns = "\n".join(lines)
        if compiles(ns):
            write(p, ns)
            return f"test-edit {p}:{i+1}"
    return None


def m_rename_func():
    for _ in range(200):
        p = rng.choice(PKG)
        s = read(p)
        m = list(re.finditer(r"^def (_?\w+)\(", s, re.M))
        if not m:
            continue
        mm = rng.choice(m)
        name = mm.group(1)
        ns = s[: mm.start(1)] + name + "_v2" + s[mm.end(1):]
        write(p, ns)
        return f"rename-func {p}:{name}"
    return None


def m_syntax():
    p = rng.choice(PKG)
    s = read(p)
    idx = [m.start() for m in re.finditer(r"\)", s)]
    k = rng.choice(idx)
    write(p, s[:k] + s[k + 1:])
    return f"syntax {p}"


def m_dict_entry():
    """Delete one line of a dict in a class body."""
    for _ in range(300):
        p = rng.choice([x for x in PKG if "/parsers/" in x or "/generators/" in x or "/dialects/" in x or "tokens" in x])
        s = read(p)
        lines = s.split("\n")
        i = rng.randrange(len(lines))
        line = lines[i]
        if not re.match(r"\s{8,}(exp\.\w+|\"[^\"]+\"|TokenType\.\w+): .*,$", line):
            continue
        ns = "\n".join(lines[:i] + lines[i + 1:])
        if compiles(ns):
            write(p, ns)
            return f"dict-entry-del {p}:{i+1}"
    return None


def m_helper():
    return m_swap(["tests/helpers.py"]) or m_delete_stmt(["tests/helpers.py"])


def m_new_method():
    """Override a parent generator method in a dialect generator with a changed copy."""
    gens = [x for x in PKG if "/generators/" in x and not x.endswith("__init__.py")]
    for _ in range(50):
        p = rng.choice(gens)
        s = read(p)
        m = re.search(r"^class (\w+)\(.*\):\n", s, re.M)
        if not m:
            continue
        name = rng.choice(["cast_sql", "alias_sql", "column_sql", "limit_sql", "ordered_sql", "not_sql"])
        if f"def {name}(" in s:
            continue
        body = (f"    def {name}(self, expression):\n        return super().{name}(expression).replace('  ', ' ')\n\n")
        if rng.random() < 0.5:
            body = f"    def {name}(self, expression):\n        return super().{name}(expression) + ''\n\n"
        ins = m.end()
        # after docstring/first line of class body
        ns = s[:ins] + body + s[ins:]
        if compiles(ns):
            write(p, ns)
            return f"new-method {p}:{name}"
    return None


def m_module_const():
    for _ in range(300):
        p = rng.choice(PKG)
        s = read(p)
        lines = s.split("\n")
        i = rng.randrange(len(lines))
        line = lines[i]
        mm = re.match(r"^([A-Z_][A-Z_0-9]+)(: [^=]+)? = (.+)$", line)
        if not mm:
            continue
        val = mm.group(3)
        if re.match(r"^-?\d+$", val):
            nv = str(int(val) + 1)
        elif val in ("True", "False"):
            nv = "False" if val == "True" else "True"
        elif val.startswith('"') and val.endswith('"'):
            nv = val[:-1] + 'x"'
        else:
            continue
        lines[i] = line[: mm.start(3)] + nv
        ns = "\n".join(lines)
        if compiles(ns):
            write(p, ns)
            return f"module-const {p}:{i+1}"
    return None


OPS = [(m_swap, 6), (m_comment, 2), (m_fixture, 3), (m_delete_stmt, 3), (m_default, 2), (m_string_const, 3),
       (m_test_edit, 2), (m_rename_func, 1), (m_syntax, 1), (m_dict_entry, 3), (m_helper, 1), (m_new_method, 2),
       (m_module_const, 2)]
kinds = {}
made = 0
while made < n:
    git("checkout", "-q", ".")
    ops = [o for o, w in OPS for _ in range(w)]
    k = 1 if rng.random() < 0.8 else 2
    descs = []
    for _ in range(k):
        d = rng.choice(ops)()
        if d:
            descs.append(d)
    patch = git("diff")
    if not descs or not patch.strip():
        continue
    cid = "m" + "".join(rng.choice(string.ascii_lowercase + string.digits) for _ in range(3))
    open(os.path.join(out, "commits", cid + ".patch"), "w").write(patch)
    kinds[cid] = descs
    made += 1
git("checkout", "-q", ".")
json.dump(kinds, open(os.path.join(out, "kinds.json"), "w"), indent=1)
print("made", made)
