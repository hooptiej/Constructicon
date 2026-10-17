#!/usr/bin/env python3
"""No hard-coded "Hobby / Hobbies" where a user can read it (#700).

An install can call a hobby something else (Admin > Install, `hobby_label` / `hobby_label_plural`,
core/hobby_label.py), so every word a user reads comes from the label: `hobby_label.one` / `.many` /
`.one_lc` / `.many_lc` / `.url(slug)` / `.index_url` in templates (web/common.py), `HobbyLabel.*` in JS
(web/static/js/hobby-label.js), `install_config.hobby_label()` in Python. This check (text and AST
only: no server, no DB) fails when one of these hard-codes the word (any case):

  * web/templates/*.html: visible text between tags, the visible attributes (title, aria-label,
    placeholder, alt, label), string literals inside {{ ... }} / {% ... %}, and string literals in
    inline <script> blocks;
  * web/static/js/*.js: string literals (and template-literal text);
  * web/**/*.py: string literals (docstrings excepted);
  * anywhere above: a page link written as the fixed old path (href="/hobbies", '/hobby/' + slug):
    links use hobby_label.index_url / .url(slug) (the old paths only redirect).

What is NOT a hit (identifiers and plumbing): comments (HTML, Jinja, JS, Python), ids, classes and
other attributes, a word joined to - _ / : # . = (`hobby-status-select`, `/api/hobby/`, `hobby:<slug>`,
`hobby_id`), and a string literal that is one lower-case token with no space (`'hobby'`, a kind or
type key). Out of scope on purpose: core/ (internal error text and identifiers; the card faces read
the label), mcp_server/ (tool descriptions are relabelled when the MCP starts), CLAUDE.md and the
tests. web/static/js/hobby-label.js holds the default words and is skipped.

A deliberate exception goes in ALLOW below as (file, exact text) with a reason.

    python scripts/check_hobby_label.py           # FAILED: N / OK
    python scripts/check_hobby_label.py --list    # every file scanned
"""

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "web" / "templates"
JS_DIR = ROOT / "web" / "static" / "js"
PY_DIR = ROOT / "web"
SKIP_FILES = {"web/static/js/hobby-label.js"}

# (repo-relative file, exact offending text) -> reason. Keep it short.
ALLOW = {
    ("web/routes/pages.py", "/hobbies"): "the internal route path; web/hobby_paths.py maps the label's URL onto it",
    ("web/routes/pages.py", "/hobby/{slug}"): "the internal route path; web/hobby_paths.py maps the label's URL onto it",
    ("web/hobby_paths.py", "/hobbies"): "the internal route path the label's index URL is rewritten to",
    ("web/hobby_paths.py", "/hobby/"): "the internal route path the label's page URLs are rewritten to",
}

# The word on its own: not glued to an identifier or path character on either side.
WORD = re.compile(r"(?<![\w\-/:#.=])(hobby|hobbies)(?![\w\-/:=#])", re.IGNORECASE)
VISIBLE_ATTRS = ("title", "aria-label", "placeholder", "alt", "label")
# A page link written as the old fixed path ("/hobbies", "/hobby/<slug>", not /api/...): it still
# works (it redirects), but the label's own URL is hobby_label.index_url / .url(slug).
PAGE_PATH = re.compile(r"^/hobb(y|ies)(/|$|\?|#)", re.IGNORECASE)


def visible_word(text):
    """True when `text` (already known to be shown to a user) contains the word on its own."""
    return bool(WORD.search(text))


def literal_hit(text):
    """A string literal: a hit when it holds the word on its own AND reads like text (has a space,
    or the word is capitalised), or when it is a page path ("/hobby/..."). One lower-case token
    ('hobby', a kind key) is an identifier."""
    if PAGE_PATH.match(text):
        return True
    m = WORD.search(text)
    if not m:
        return False
    return any(ch.isspace() for ch in text) or any(w.group(0)[0].isupper() for w in WORD.finditer(text))


def _line_of(src, pos):
    return src.count("\n", 0, pos) + 1


# --- JS --------------------------------------------------------------------------------------

def js_strings(src):
    """(position, text) of every string literal in JS source; comments skipped. Template literals
    yield their text parts (the ${...} parts are code and are scanned as code). Regex literals are
    not recognised; a quote inside one could confuse this, which the repo's JS doesn't do."""
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c in "'\"":
            j, buf = i + 1, []
            while j < n and src[j] != c and src[j] != "\n":
                if src[j] == "\\" and j + 1 < n:
                    buf.append(src[j:j + 2])
                    j += 2
                    continue
                buf.append(src[j])
                j += 1
            out.append((i, "".join(buf)))
            i = j + 1
        elif c == "`":
            j, buf, depth = i + 1, [], 0
            while j < n:
                if src[j] == "\\" and j + 1 < n:
                    buf.append(src[j:j + 2])
                    j += 2
                    continue
                if src[j] == "`":
                    break
                if src.startswith("${", j):
                    # skip the expression (nested braces counted); its strings are rare and simple
                    out.append((i, "".join(buf)))
                    buf = []
                    k, depth = j + 2, 1
                    while k < n and depth:
                        depth += {"{": 1, "}": -1}.get(src[k], 0)
                        k += 1
                    out.extend((i, t) for _, t in js_strings(src[j + 2:k - 1]))
                    j = k
                    continue
                buf.append(src[j])
                j += 1
            out.append((i, "".join(buf)))
            i = j + 1
        else:
            i += 1
    return out


def check_js(rel, src, base_line=0):
    hits = []
    for pos, text in js_strings(src):
        if literal_hit(text):
            hits.append((rel, base_line + _line_of(src, pos), text.strip()))
    return hits


# --- templates -------------------------------------------------------------------------------

_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_SCRIPT = re.compile(r"(<script\b[^>]*>)(.*?)</script>", re.S | re.I)
_STYLE = re.compile(r"<style\b[^>]*>.*?</style>", re.S | re.I)
_JINJA = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.S)
_PY_STR = re.compile(r"'([^'\\]*(?:\\.[^'\\]*)*)'|\"([^\"\\]*(?:\\.[^\"\\]*)*)\"")
_TAG = re.compile(r"<[A-Za-z][^<>]*>", re.S)
_ATTR = re.compile(r"([\w:-]+)\s*=\s*(\"[^\"]*\"|'[^']*')")


def _blank(match):
    """Replaces a span with spaces/newlines of the same shape, so positions and lines still match."""
    return re.sub(r"[^\n]", " ", match.group(0))


def check_template(rel, src):
    hits = []
    src = _JINJA_COMMENT.sub(_blank, src)
    src = _HTML_COMMENT.sub(_blank, src)
    src = _STYLE.sub(_blank, src)
    # inline scripts: their strings, as JS (Jinja inside them counts as code)
    for m in _SCRIPT.finditer(src):
        if "src=" in m.group(1):
            continue
        body = _JINJA.sub(lambda j: "0" + _blank(j)[1:], m.group(2))
        hits += check_js(rel, body, _line_of(src, m.start(2)) - 1)
    src = _SCRIPT.sub(_blank, src)
    # Jinja expressions/statements: the string literals inside them
    for m in _JINJA.finditer(src):
        for s in _PY_STR.finditer(m.group(0)):
            text = s.group(1) if s.group(1) is not None else s.group(2)
            if literal_hit(text):
                hits.append((rel, _line_of(src, m.start()), text.strip()))
    src = _JINJA.sub(_blank, src)
    # tags: only the visible attributes
    for m in _TAG.finditer(src):
        for a in _ATTR.finditer(m.group(0)):
            name, value = a.group(1).lower(), a.group(2)[1:-1]
            if (name in VISIBLE_ATTRS and visible_word(value)) or (name in ("href", "action") and PAGE_PATH.match(value)):
                hits.append((rel, _line_of(src, m.start()), f'{a.group(1)}={a.group(2)}'))
    text_only = _TAG.sub(_blank, src)
    for m in WORD.finditer(text_only):
        line_start = text_only.rfind("\n", 0, m.start()) + 1
        line_end = text_only.find("\n", m.end())
        hits.append((rel, _line_of(text_only, m.start()),
                     " ".join(text_only[line_start:line_end if line_end >= 0 else None].split())))
    return hits


# --- Python ----------------------------------------------------------------------------------

def check_py(rel, src):
    hits = []
    tree = ast.parse(src)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            if literal_hit(node.value):
                hits.append((rel, node.lineno, node.value.strip()))
    return hits


# --- run -------------------------------------------------------------------------------------

def files():
    out = []
    out += sorted(TEMPLATES.rglob("*.html"))
    out += sorted(JS_DIR.rglob("*.js"))
    out += sorted(p for p in PY_DIR.rglob("*.py") if "__pycache__" not in p.parts)
    return [p for p in out if p.relative_to(ROOT).as_posix() not in SKIP_FILES]


def scan():
    hits = []
    for p in files():
        rel = p.relative_to(ROOT).as_posix()
        src = p.read_text(encoding="utf-8")
        if p.suffix == ".html":
            hits += check_template(rel, src)
        elif p.suffix == ".js":
            hits += check_js(rel, src)
        else:
            hits += check_py(rel, src)
    return [h for h in hits if (h[0], h[2]) not in ALLOW]


def main(argv):
    if "--list" in argv:
        for p in files():
            print(p.relative_to(ROOT).as_posix())
        return
    hits = scan()
    for rel, line, text in hits:
        print(f"FAIL {rel}:{line}: hard-coded hobby word: {text[:120]}")
    if hits:
        print(f"FAILED: {len(hits)} hard-coded hobby word(s); use the label (hobby_label / HobbyLabel / "
              f"install_config.hobby_label()), see scripts/check_hobby_label.py")
    else:
        print(f"OK: no hard-coded hobby words in {len(files())} files")


if __name__ == "__main__":
    main(sys.argv[1:])
