"""Fail on silent exception handlers (#551).

An ``except`` whose body is only ``pass`` / ``continue`` / ``break`` /
``return <constant-ish>`` (no logging call, no raise) swallows the failure with
no trace. Either log it (``log.warning(..., exc_info=...)`` / ``_log.warning``)
or let it fail properly (``raise AppError``).

Scans core/, web/, mcp_server/. Exit 1 listing every offender.

Escape hatches, both explicit (prefer logging over either):
  * a ``# silent-ok: <reason>`` comment on the ``except`` line or inside its body, for a
    handler that is pure control flow or input validation (a parse that returns None
    for a malformed value, a sentinel exception used to unwind, a cleanup race);
  * ``ALLOW`` below, ``"path:function": reason``, for code another owner is changing.

Usage:  python scripts/check_no_silent_except.py [--list]
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN = ("core", "web", "mcp_server")

# "relative/path.py:enclosing_function": reason. Keep empty unless truly needed.
ALLOW: dict[str, str] = {
    # core/storage.py was being reworked on another branch when #551 item 5 landed, so it was left
    # untouched to avoid a merge fight. TODO: replace with a log call or a `# silent-ok:` comment
    # and delete this entry.
    "core/storage.py:exif_upright": "storage.py owned by another in-flight change",
}

LOG_NAMES = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}


def _is_trivial_value(node) -> bool:
    if node is None or isinstance(node, (ast.Constant, ast.Name)):
        return True
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_is_trivial_value(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return not node.keys
    if isinstance(node, ast.UnaryOp):
        return _is_trivial_value(node.operand)
    return False


def _has_log_call(stmts) -> bool:
    for s in stmts:
        for n in ast.walk(s):
            if isinstance(n, ast.Call):
                f = n.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                if name in LOG_NAMES or name.startswith("_log"):
                    return True
            if isinstance(n, ast.Raise):
                return True
    return False


def _is_silent(body) -> bool:
    if _has_log_call(body):
        return False
    for s in body:
        if isinstance(s, (ast.Pass, ast.Continue, ast.Break)):
            continue
        if isinstance(s, ast.Return) and _is_trivial_value(s.value):
            continue
        if isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant):
            continue
        if isinstance(s, ast.Assign) and _is_trivial_value(s.value):
            continue
        return False
    return True


def find_silent(path: Path):
    rel = path.relative_to(ROOT).as_posix()
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    tree = ast.parse(text, filename=rel)
    out = []

    def marked(h):
        end = max(getattr(n, "end_lineno", h.lineno) for n in h.body)
        for i in range(h.lineno - 1, min(end, len(lines))):
            tag = lines[i].partition("# silent-ok:")[2].strip()
            if tag:
                return True
        return False

    def visit(node, func):
        for child in ast.iter_child_nodes(node):
            f = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else func
            if isinstance(child, ast.ExceptHandler) and _is_silent(child.body) and not marked(child):
                if f"{rel}:{func}" not in ALLOW:
                    out.append((rel, child.lineno, func))
            visit(child, f)

    visit(tree, "<module>")
    return out


def main() -> int:
    bad = []
    for d in SCAN:
        for p in sorted((ROOT / d).rglob("*.py")):
            bad.extend(find_silent(p))
    for rel, line, func in bad:
        print(f"{rel}:{line}: silent except in {func}()")
    if bad:
        print(f"\n{len(bad)} silent except handler(s). Log with context or raise AppError.")
        return 1
    print("OK: no silent except handlers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
