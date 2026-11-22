"""Patch parsing/applying and code-object diffing."""

import inspect
import re
import types

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class FilePatch:
    def __init__(self):
        self.old_path = None
        self.new_path = None
        self.hunks = []  # (old_start, old_len, new_start, new_len, lines)
        self.binary = False
        self.mode_change = False
        self.rename = False

    @property
    def path(self):
        return self.new_path if self.new_path is not None else self.old_path

    @property
    def is_new(self):
        return self.old_path is None

    @property
    def is_deleted(self):
        return self.new_path is None

    def __repr__(self):
        return f"FilePatch({self.old_path}->{self.new_path}, {len(self.hunks)} hunks)"


def _strip(p):
    p = p.strip()
    if "\t" in p:
        p = p.split("\t", 1)[0]
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape").encode("latin-1").decode("utf-8", "replace")
    if p == "/dev/null":
        return None
    if p[:2] in ("a/", "b/"):
        p = p[2:]
    return p


def parse_patch(text):
    files = []
    cur = None
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("diff --git "):
            cur = FilePatch()
            files.append(cur)
            m = re.match(r"diff --git (\"?a/.*?\"?) (\"?b/.*\"?)$", line)
            if m:
                cur.old_path = _strip(m.group(1))
                cur.new_path = _strip(m.group(2))
            i += 1
            continue
        if line.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
            if cur is None or cur.hunks:
                cur = FilePatch()
                files.append(cur)
            cur.old_path = _strip(line[4:])
            cur.new_path = _strip(lines[i + 1][4:])
            i += 2
            continue
        if cur is not None:
            if line.startswith("new file mode"):
                cur.old_path = None
            elif line.startswith("deleted file mode"):
                cur.new_path = None
            elif line.startswith("rename from "):
                cur.old_path = line[len("rename from "):]
                cur.rename = True
            elif line.startswith("rename to "):
                cur.new_path = line[len("rename to "):]
                cur.rename = True
            elif line.startswith("old mode") or line.startswith("new mode"):
                cur.mode_change = True
            elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
                cur.binary = True
            m = HUNK_RE.match(line)
            if m:
                os_, ol, ns, nl = m.groups()
                ol = 1 if ol is None else int(ol)
                nl = 1 if nl is None else int(nl)
                body = []
                i += 1
                o = ol
                nn = nl
                while i < n and (o > 0 or nn > 0):
                    l = lines[i]
                    if l.startswith("\\"):
                        body.append(l)
                        i += 1
                        continue
                    tag = l[:1] if l else " "
                    body.append(tag + l[1:] if l else " ")
                    if tag == " ":
                        o -= 1
                        nn -= 1
                    elif tag == "-":
                        o -= 1
                    elif tag == "+":
                        nn -= 1
                    else:
                        break
                    i += 1
                while i < n and lines[i].startswith("\\"):
                    body.append(lines[i])
                    i += 1
                cur.hunks.append((int(os_), ol, int(ns), nl, body))
                continue
        i += 1
    return files


class PatchError(Exception):
    pass


def apply_hunks(old_text, hunks):
    """Apply hunks to old_text (str). Returns new text."""
    if old_text is None:
        old_text = ""
    old_lines = old_text.splitlines(keepends=True)
    out = []
    pos = 0  # index into old_lines
    offset_total = 0
    for os_, ol, ns, nl, body in hunks:
        old_chunk = []
        new_chunk = []
        no_nl_old = no_nl_new = False
        last = None
        for l in body:
            if l.startswith("\\"):
                if last == "-":
                    no_nl_old = True
                elif last == "+":
                    no_nl_new = True
                else:
                    no_nl_old = no_nl_new = True
                continue
            tag, content = l[0], l[1:]
            last = tag
            if tag in " -":
                old_chunk.append(content)
            if tag in " +":
                new_chunk.append(content)
        start = os_ - 1 if ol > 0 else os_
        # locate (allow small fuzz in position)
        def matches(at):
            if at < 0 or at + len(old_chunk) > len(old_lines):
                return False
            for k, c in enumerate(old_chunk):
                if old_lines[at + k].rstrip("\r\n") != c.rstrip("\r\n"):
                    return False
            return True

        at = start + offset_total
        if not matches(at):
            found = None
            for delta in range(1, 200):
                for cand in (at - delta, at + delta):
                    if cand >= pos and matches(cand):
                        found = cand
                        break
                if found is not None:
                    break
            if found is None:
                raise PatchError(f"hunk at {os_} does not apply")
            offset_total += found - at
            at = found
        out.extend(old_lines[pos:at])
        for k, c in enumerate(new_chunk):
            last_line = k == len(new_chunk) - 1
            if last_line and no_nl_new:
                out.append(c)
            else:
                out.append(c + "\n")
        pos = at + len(old_chunk)
        if no_nl_old and not no_nl_new and pos >= len(old_lines):
            pass
    out.extend(old_lines[pos:])
    return "".join(out)


def changed_old_lines(hunks):
    """Old-file line numbers touched by hunks (deleted lines, or the lines
    around an insertion). A block that replaces lines one for one, each new
    line with the old line's indentation, touches only the old lines."""
    lines = set()

    def ind(t):
        return len(t) - len(t.lstrip(" \t"))

    for os_, ol, ns, nl, body in hunks:
        ln = os_ if ol > 0 else os_ + 1
        body = [l for l in body if l and not l.startswith("\\")]
        k = 0
        while k < len(body):
            t = body[k][0]
            if t == " ":
                ln += 1
                k += 1
                continue
            # a block of consecutive changes
            dels, adds, start = [], [], ln
            while k < len(body) and body[k][0] in "-+":
                (dels if body[k][0] == "-" else adds).append(body[k][1:])
                k += 1
            for x in range(len(dels)):
                lines.add(start + x)
            ln = start + len(dels)
            if adds:
                same = len(adds) == len(dels) and all(
                    ind(a) == ind(d) and a.strip() and d.strip() for a, d in zip(adds, dels))
                if not same:
                    lines.add(max(ln - 1, 1))
                    lines.add(ln)
    return lines


# ---------------------------------------------------------------- code diff

CO_OPTIMIZED = inspect.CO_OPTIMIZED


def is_function_code(co):
    return bool(co.co_flags & CO_OPTIMIZED)


def _children(co):
    return [c for c in co.co_consts if isinstance(c, types.CodeType)]


def _const_sig(c):
    if isinstance(c, types.CodeType):
        return ("<code>", c.co_qualname)
    if isinstance(c, (tuple, frozenset)):
        return (type(c).__name__, tuple(_const_sig(x) for x in c)) if isinstance(c, tuple) else (
            "frozenset", tuple(sorted(repr(_const_sig(x)) for x in c)))
    return (type(c).__name__, repr(c))


def _doc_index(co):
    """0 if co_consts[0] is a function docstring (a str no instruction loads:
    it only becomes __doc__, which no test reads), else None."""
    if not is_function_code(co) or not co.co_consts or type(co.co_consts[0]) is not str:
        return None
    import dis

    for ins in dis.get_instructions(co):
        if ins.opcode in dis.hasconst and ins.arg == 0:
            return None
    return 0


def shallow_sig(co):
    cls_body = (not is_function_code(co)) and "__firstlineno__" in co.co_names
    # only a leading str constant can be a docstring: skip the bytecode scan otherwise
    doc = _doc_index(co) if co.co_consts and type(co.co_consts[0]) is str else None
    return (
        co.co_code,
        co.co_names,
        co.co_varnames,
        co.co_freevars,
        co.co_cellvars,
        co.co_argcount,
        co.co_posonlyargcount,
        co.co_kwonlyargcount,
        co.co_flags,
        co.co_stacksize,
        co.co_exceptiontable,
        tuple(
            ("<doc>",) if i == doc else
            ("<firstlineno>",) if (cls_body and type(c) is int and c == co.co_firstlineno) else _const_sig(c)
            for i, c in enumerate(co.co_consts)
        ),
    )


def _keyed_children(co):
    out = {}
    counts = {}
    for c in _children(co):
        q = c.co_qualname
        k = counts.get(q, 0)
        counts[q] = k + 1
        out[(q, k)] = c
    return out


def code_diff(old_co, new_co):
    """Compare two code-object trees.

    Returns (changed, line_only):
      changed: list of (old_code, kind) where kind is 'self' (own bytecode
               differs) or 'removed' (no counterpart in new).
      added:   list of new code objects without an old counterpart
      line_only: True if trees are equal modulo line numbers
    """
    changed = []
    added = []
    lines_moved = [False]

    def walk(o, n):
        if shallow_sig(o) != shallow_sig(n):
            changed.append((o, "self"))
        if o.co_firstlineno != n.co_firstlineno or o.co_linetable != n.co_linetable:
            lines_moved[0] = True
        oc = _keyed_children(o)
        nc = _keyed_children(n)
        for k, c in oc.items():
            if k in nc:
                walk(c, nc[k])
            else:
                changed.append((c, "removed"))
                _all(c, changed)
        for k, c in nc.items():
            if k not in oc:
                added.append(c)

    def _all(c, acc):
        for ch in _children(c):
            acc.append((ch, "removed"))
            _all(ch, acc)

    walk(old_co, new_co)
    return changed, added, lines_moved[0]


def all_codes(co):
    out = [co]
    for c in _children(co):
        out.extend(all_codes(c))
    return out


def enclosing_scopes(root):
    """Map code object -> parent code object."""
    parent = {}

    def walk(c):
        for ch in _children(c):
            parent[ch] = c
            walk(ch)

    walk(root)
    return parent


# ------------------------------------------------------- statement mapping

import ast as _ast

_COMPOUND = (_ast.If, _ast.For, _ast.AsyncFor, _ast.While, _ast.With, _ast.AsyncWith, _ast.Try,
             _ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef, _ast.Match)
try:
    _COMPOUND = _COMPOUND + (_ast.TryStar,)
except AttributeError:  # pragma: no cover
    pass


class StmtIndex:
    """Statement spans of a source file, for mapping changed lines to the
    lines whose execution decides whether the change can matter."""

    def __init__(self, src):
        self.tree = _ast.parse(src)
        self.nodes = []  # (lineno, end_lineno, node, parent)
        self.code_lines = set()

        def visit(node, parent):
            for ch in _ast.iter_child_nodes(node):
                if isinstance(ch, (_ast.stmt, _ast.ExceptHandler, _ast.match_case)):
                    start = ch.lineno if not isinstance(ch, _ast.match_case) else ch.pattern.lineno
                    end = ch.end_lineno if not isinstance(ch, _ast.match_case) else ch.body[-1].end_lineno
                    decos = getattr(ch, "decorator_list", None)
                    if decos:
                        start = min(start, min(d.lineno for d in decos))
                    self.nodes.append((start, end, ch, node))
                    visit(ch, ch)
                elif isinstance(ch, (_ast.expr, _ast.arguments, _ast.withitem, _ast.keyword, _ast.alias,
                                     _ast.comprehension, _ast.arg, _ast.pattern)):
                    continue
                else:
                    visit(ch, parent)

        visit(self.tree, None)
        for start, end, n, _p in self.nodes:
            if not isinstance(n, _COMPOUND + (_ast.ExceptHandler, _ast.match_case)):
                self.code_lines.update(range(start, end + 1))
            else:
                self.code_lines.update(range(start, self._header_end(n, start) + 1))

    @staticmethod
    def _header_end(n, start):
        body = getattr(n, "body", None)
        if body:
            return max(start, body[0].lineno - 1) if not isinstance(body[0], _ast.match_case) else max(start, body[0].pattern.lineno - 1)
        return start

    def innermost(self, line):
        best = None
        for start, end, n, p in self.nodes:
            if start <= line <= end:
                if best is None or (start >= best[0] and end <= best[1]):
                    best = (start, end, n, p)
        return best

    def deciding_lines(self, line, func_start, func_end, _depth=0):
        """Lines such that: if none of them ran, the change on `line` could
        not have been reached. None means 'use the whole function'."""
        hit = self.innermost(line)
        if hit is None:
            return None
        start, end, n, parent = hit
        if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef, _ast.Lambda)) and start <= func_start <= end and not (start > func_start):
            # the function itself (signature, decorators or a gap at its top level)
            if line in self.code_lines and line <= self._header_end(n, n.lineno):
                return None
            return self._gap(line, n, start, end, func_start, func_end, _depth)
        if isinstance(n, _ast.Expr) and isinstance(n.value, _ast.Constant) and isinstance(n.value.value, str):
            return None  # docstring-like constant: no line events
        if isinstance(n, (_ast.ExceptHandler, _ast.match_case)):
            hend = self._header_end(n, start)
            if line <= hend:
                # matching runs whenever the enclosing try/match is reached
                ps = parent.lineno
                return set(range(ps, parent.end_lineno + 1))
            return self._gap(line, n, start, end, func_start, func_end, _depth)
        if isinstance(n, _COMPOUND):
            hend = self._header_end(n, start)
            if line <= hend:
                return set(range(start, hend + 1))
            return self._gap(line, n, start, end, func_start, func_end, _depth)
        return set(range(start, end + 1))

    def _gap(self, line, n, start, end, func_start, func_end, depth):
        """A blank/comment/keyword line inside a compound statement: decided
        by the nearest code lines around it."""
        if depth > 3:
            return set(range(start, end + 1))
        out = set()
        lo = max(start, func_start)
        hi = min(end, func_end)
        prev = next(( l for l in range(line - 1, lo - 1, -1) if l in self.code_lines), None)
        nxt = next((l for l in range(line + 1, hi + 1) if l in self.code_lines), None)
        for l in (prev, nxt):
            if l is None:
                continue
            d = self.deciding_lines(l, func_start, func_end, depth + 1)
            if d is None:
                return None
            out |= d
        if not out:
            return None
        return out


def code_line_span(co):
    lines = [l for _s, _e, l in co.co_lines() if l is not None]
    for c in _children(co):
        lines.extend(code_line_span(c))
    return lines
