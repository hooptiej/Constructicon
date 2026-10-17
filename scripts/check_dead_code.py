"""List code nothing refers to (#546): the reference scan, as a script. REPORT ONLY.

Two lists, read from the files alone (AST + word scan; no server, no DB, no imports of the app):

  * Python: every module-level function, class and UPPER_CASE constant defined in core/ or web/
    whose name appears nowhere else in the code. "Elsewhere" means any Python file under core/,
    web/, mcp_server/ or scripts/ (not scripts/archive/: those refuse to run), any template or
    other file under web/ (.html, .js, .md, .css) and the repo-root scripts, as a whole word,
    including inside strings and comments (so a getattr("name") lookup or a Jinja global counts),
    but not inside the definition's own lines. A name used only by tests and checks
    (scripts/test_*, scripts/check_*, scripts/_testenv.py) is listed separately as "test-only".
  * CSS: every class selector in web/static/style.css and web/static/css/*.css that no template,
    JS file or Python file under core/, web/ or mcp_server/ mentions. A class built from a string
    ('audit-log-status-' + x, `curator-severity-${s}`) counts as used when its prefix up to a "-"
    appears in the code followed by a quote, "${" or "{{".

Things a word scan can't see are skipped, not listed: names starting and ending with "__", functions
with a decorator (routes, middleware hooks, MCP tools: the framework calls them), and the ALLOW list
below (each entry with its reason). A common word as a name (get, run) always looks used; that is the
scan's blind spot, not a promise.

It never fails the suite on purpose: the point is the visible list, not a merge gate. It ends with
an "OK: report only ..." line that run_checks.py reads as a pass. Read the list before a cleanup PR,
then grep each name yourself (an HTML attribute, a test fixture or another branch may still want it).

Usage:  python scripts/check_dead_code.py [--root DIR] [--quiet]
"""
import argparse
import ast
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFINE_IN = ("core", "web")

# "relative/path.py:name": reason. Entry points and names something outside this repo calls.
ALLOW: dict[str, str] = {
    "web/app.py:app": "the ASGI entry point (uvicorn web.app:app)",
}

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CSS_CLASS = re.compile(r"\.(-?[_a-zA-Z][_a-zA-Z0-9-]*)")
_CSS_TOKEN = re.compile(r"[A-Za-z0-9_-]+")


def _rel(p: Path, root: Path) -> str:
    return p.relative_to(root).as_posix()


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _is_test_file(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return rel.startswith("scripts/") and (name.startswith(("test_", "check_")) or name == "_testenv.py")


def reference_files(root: Path) -> list[Path]:
    """Every file a Python name could be used from."""
    out = set()
    for d in ("core", "web", "mcp_server", "scripts"):
        base = root / d
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if not p.is_file() or "__pycache__" in p.parts:
                continue
            rel = _rel(p, root)
            if rel.startswith("scripts/archive/"):
                continue
            if p.suffix in (".py", ".html", ".js", ".md", ".css", ".sh", ".json"):
                out.add(p)
    out.update(p for p in root.glob("*.py") if p.is_file())
    return sorted(out)


def _definitions(tree: ast.Module):
    """(name, kind, first line, last line) for every module-level def worth checking."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.decorator_list:
                continue   # a route, a hook, a registered tool: the framework calls it
            first = node.lineno
            yield node.name, ("class" if isinstance(node, ast.ClassDef) else "function"), first, node.end_lineno
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name) and t.id.isupper() and len(t.id) > 1:
                    yield t.id, "constant", node.lineno, node.end_lineno


def scan_python(root: Path):
    """Returns (unreferenced, test_only): lists of (rel path, line, kind, name)."""
    files = reference_files(root)
    real, tests = Counter(), Counter()
    lines_of = {}
    for p in files:
        rel = _rel(p, root)
        text = _read(p)
        lines_of[rel] = text.splitlines()
        (tests if _is_test_file(rel) else real).update(_WORD.findall(text))

    unref, test_only = [], []
    for d in DEFINE_IN:
        base = root / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            rel = _rel(p, root)
            try:
                tree = ast.parse(_read(p), filename=rel)
            except SyntaxError as e:
                print(f"note: {rel} does not parse ({e.msg}); skipped")
                continue
            for name, kind, first, last in _definitions(tree):
                if name.startswith("__") and name.endswith("__"):
                    continue
                if f"{rel}:{name}" in ALLOW:
                    continue
                own = sum(len(re.findall(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", ln))
                          for ln in lines_of.get(rel, [])[first - 1:last])
                if real[name] - own > 0:
                    continue
                (test_only if tests[name] > 0 else unref).append((rel, first, kind, name))
    return unref, test_only


def css_files(root: Path) -> list[Path]:
    out = [root / "web" / "static" / "style.css"]
    out += sorted((root / "web" / "static" / "css").glob("*.css"))
    return [p for p in out if p.is_file()]


def scan_css(root: Path):
    """Returns a list of (rel css path, line, class) that nothing in the code mentions."""
    code = []
    for d in ("core", "web", "mcp_server"):
        base = root / d
        if base.is_dir():
            code += [p for p in base.rglob("*") if p.is_file() and p.suffix in (".py", ".html", ".js")
                     and "__pycache__" not in p.parts]
    text = "\n".join(_read(p) for p in code)
    tokens = set(_CSS_TOKEN.findall(text))
    dead, seen = [], set()
    for css in css_files(root):
        src = _read(css)
        stripped = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), src, flags=re.S)
        # selectors only: the text before each "{" (declarations hold values like .5em, not classes)
        for m in re.finditer(r"([^{}]*)\{", stripped):
            sel = m.group(1)
            if sel.strip().startswith("@"):
                continue
            line = stripped.count("\n", 0, m.start(1) + len(sel) - len(sel.lstrip())) + 1
            for c in _CSS_CLASS.findall(sel):
                if c in seen or re.fullmatch(r"\d.*", c):
                    continue
                seen.add(c)
                if c in tokens or _built_from_string(c, text):
                    continue
                dead.append((_rel(css, root), line, c))
    return dead


def _built_from_string(cls: str, text: str) -> bool:
    parts = cls.split("-")
    for i in range(len(parts) - 1, 0, -1):
        prefix = "-".join(parts[:i]) + "-"
        if re.search(re.escape(prefix) + r"(?:['\"`]|\$\{|\{\{)", text):
            return True
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", type=Path, default=ROOT, help="repo root to scan (default: this checkout)")
    ap.add_argument("--quiet", action="store_true", help="print only the counts")
    args = ap.parse_args(argv)
    root = args.root.resolve()

    unref, test_only = scan_python(root)
    dead_css = scan_css(root)
    if not args.quiet:
        print(f"Python names with no reference outside their own definition ({len(unref)}):")
        for rel, line, kind, name in unref:
            print(f"  unreferenced  {rel}:{line}  {kind} {name}")
        print(f"\nPython names used only by tests and checks ({len(test_only)}):")
        for rel, line, kind, name in test_only:
            print(f"  test-only     {rel}:{line}  {kind} {name}")
        print(f"\nCSS classes no template, JS or Python file mentions ({len(dead_css)}):")
        for rel, line, cls in dead_css:
            print(f"  unused class  {rel}:{line}  .{cls}")
        print()
    print(f"OK: report only, not a gate: {len(unref)} unreferenced, {len(test_only)} test-only, "
          f"{len(dead_css)} unused CSS classes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
