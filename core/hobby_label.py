"""The word for a hobby (#700, "A hobby by any other name"): pure rules, no DB.

A hobby is shown as "Hobby / Hobbies" by default. An install can call it something else
("Department / Departments") in Admin > Install; the setting is stored by core/install_config.py
(keys `hobby_label` and `hobby_label_plural`) and read through `install_config.hobby_label()`,
which returns a `Label` from this module.

What follows the label: every user-visible word on the web pages (templates, JS, card faces), the
page URLs (`/<plural slug>` for the index, `/<singular slug>/<slug>` for one; the old `/hobbies`,
`/hobby/<slug>` and any earlier label's paths redirect, web/hobby_paths.py) and the MCP tool names
and descriptions (read when the MCP process starts, mcp_server/server.py).

What stays "hobby" on purpose (stable plumbing): DB tables and columns, API routes
(`/api/hobby/...`, `/api/hobbies...`), JSON field names, MCP parameter names, `core/` identifiers.
"""

import re
import unicodedata
from dataclasses import dataclass

DEFAULT_ONE = "Hobby"
DEFAULT_MANY = "Hobbies"
MIN_LENGTH = 2
MAX_LENGTH = 32
# Letters (any alphabet), spaces and a little punctuation; must start with a letter.
_PUNCT = " -'&."

# First URL segments the app already uses or may well use. A label slug must not be one of these,
# or its pages would shadow (or be shadowed by) a real route. scripts/test_hobby_label_700.py
# checks every first segment of the live route table is in here.
RESERVED_SLUGS = frozenset({
    "", "account", "admin", "api", "brand", "captions", "curator", "f", "gallery", "guides",
    "healthz", "image", "login", "logout", "object", "project", "projects", "setup", "unfiled",
    "upload", "wallpaper", "static", "preview", "docs", "redoc", "openapi.json", "favicon.ico",
    "robots.txt", "tag", "tags", "card", "cards", "family", "families", "collection",
    "collections", "item", "items", "file", "files", "user", "users", "settings", "export",
    "exports", "search", "blog", "home", "index", "new", "edit", "changes", "trash", "decisions",
    "media", "thumb", "thumbs", "auth",
})


def slugify(text):
    """The URL form of a label word: accents dropped, lower case, runs of anything that isn't a
    letter or digit become one '-'. "Área de Estudo" -> "area-de-estudo"."""
    ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")


def _lower_first(word):
    return word[:1].lower() + word[1:] if word else word


def _title_first(word):
    return word[:1].upper() + word[1:] if word else word


@dataclass(frozen=True)
class Label:
    """One label, every casing a page needs. `one` / `many` are as the admin typed them, with the
    first letter upper-cased (headings, buttons, titles); `one_lc` / `many_lc` are lower case
    (inside a sentence: "Add a project to this department")."""
    one: str = DEFAULT_ONE
    many: str = DEFAULT_MANY

    @property
    def one_lc(self):
        return self.one.lower()

    @property
    def many_lc(self):
        return self.many.lower()

    @property
    def one_slug(self):
        return slugify(self.one)

    @property
    def many_slug(self):
        return slugify(self.many)

    @property
    def index_url(self):
        return "/" + self.many_slug

    def url(self, slug):
        """The page of one hobby: /<singular slug>/<hobby slug>."""
        return f"/{self.one_slug}/{slug}"

    @property
    def is_default(self):
        return (self.one, self.many) == (DEFAULT_ONE, DEFAULT_MANY)

    # MCP tool names (#700): the word inside a tool name, "constructicon_create_<word>".
    @property
    def one_ident(self):
        return self.one_slug.replace("-", "_")

    @property
    def many_ident(self):
        return self.many_slug.replace("-", "_")

    def as_dict(self):
        return {"one": self.one, "many": self.many, "one_lc": self.one_lc, "many_lc": self.many_lc,
                "one_slug": self.one_slug, "many_slug": self.many_slug, "index_url": self.index_url,
                "is_default": self.is_default}


DEFAULT = Label()


def make(one, many):
    """A Label from stored values; the default for missing ones (both must be set to rename)."""
    if not one or not many:
        return DEFAULT
    return Label(_title_first(one), _title_first(many))


def clean_word(value, what):
    """Validates one label word (singular or plural). Returns the cleaned word (spaces collapsed)
    or raises ValueError with a message an admin can act on."""
    if not isinstance(value, str):
        raise ValueError(f"The {what} must be text.")
    word = " ".join(value.split())
    if not (MIN_LENGTH <= len(word) <= MAX_LENGTH):
        raise ValueError(f"The {what} must be {MIN_LENGTH} to {MAX_LENGTH} characters long.")
    if not word[0].isalpha():
        raise ValueError(f"The {what} must start with a letter.")
    bad = sorted({ch for ch in word if not (ch.isalpha() or ch in _PUNCT)})
    if bad:
        raise ValueError(f"The {what} may hold only letters, spaces and - ' & . (not {''.join(bad)!r}).")
    slug = slugify(word)
    if len(slug) < MIN_LENGTH:
        raise ValueError(f"The {what} needs at least {MIN_LENGTH} plain letters for its web address.")
    if slug in RESERVED_SLUGS:
        raise ValueError(f"\"{word}\" can't be used: /{slug} is already a page of the app.")
    return _title_first(word)


def validate_pair(one, many):
    """Both words together: each valid on its own, and their web addresses distinct. The singular
    can't take "hobbies" and the plural can't take "hobby" (the old addresses redirect)."""
    one = clean_word(one, "singular label")
    many = clean_word(many, "plural label")
    if slugify(one) == slugify(many):
        raise ValueError(f"The singular and plural labels need different web addresses "
                         f"(both are /{slugify(one)}).")
    if slugify(one) == "hobbies" or slugify(many) == "hobby":
        raise ValueError("Use \"hobbies\" only as the plural and \"hobby\" only as the singular.")
    return one, many


_WORD_RE = re.compile(r"\b(hobby|hobbies)\b", re.IGNORECASE)


def relabel_text(text, label):
    """Replaces the words "hobby" / "hobbies" in free text with the label, keeping the case style
    of each occurrence (Hobby -> Department, hobby -> department, HOBBY -> DEPARTMENT). Parts that
    are identifiers stay: a word inside `backticks`, joined to `_` (`hobby_id`, a tool name; \\b
    already skips those), or followed by `=` / `:` (a parameter, `hobby=`, `hobby:<slug>`) or
    preceded by `/` (a path, `/api/hobby`). Used for the MCP tool descriptions."""
    if label.is_default or not text:
        return text

    def repl(m):
        start, end = m.start(), m.end()
        before = text[start - 1] if start else ""
        after = text[end] if end < len(text) else ""
        if before in "`/'\"-" or after in "`=:/'\"-":
            return m.group(0)
        word = m.group(0)
        new = label.one if word.lower() == "hobby" else label.many
        if word.isupper():
            return new.upper()
        if word[0].isupper():
            return _title_first(new)
        return new.lower()

    return _WORD_RE.sub(repl, text)
