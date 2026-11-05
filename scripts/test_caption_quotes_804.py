#!/usr/bin/env python3
"""Self-contained check for "Quote, Unquote" (#804): moondream reads a PDF page's title text back as
`!"FT Versa Wing"`, and title_from_caption cut the closing quote, leaving `!"FT Versa Wing` (unbalanced).

This is the safe string-cleanup half only (strip stray leading punctuation and surrounding quotes, never
leave an unbalanced quote or bracket in a title). Whether to RE-ASK the model instead is a separate owner
decision on #804 and is NOT built or tested here.

Throwaway DB + storage (scripts/_testenv.py), no server, no model (the Ollama call is faked).

    python scripts/test_caption_quotes_804.py

Prints a verdict line; exits 1 if any check fails.
"""

import io
import os
import sys

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("caption-quotes-804-")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

try:
    import cairosvg  # noqa: F401
except Exception:  # silent-ok: no libcairo here (e.g. Windows): object types import it
    import types
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from PIL import Image  # noqa: E402

from core import actor, captions, db, item_title, paths  # noqa: E402
_testenv.assert_isolated()

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def balanced(s):
    """An independent (deliberately dumb) balance check: no counting of apostrophes, which are not quotes."""
    if s.count('"') % 2:
        return False
    if s.count("“") != s.count("”"):
        return False
    for a, b in ("()", "[]", "{}"):
        if s.count(a) != s.count(b):
            return False
    return True


SC = captions.strip_caption_quotes
TT = captions.title_from_caption

# ---- strip_caption_quotes: a caption that is only punctuation plus a quoted phrase ---------------------
BUG = '!"FT Versa Wing"'
check("the issue's caption: leading ! and the quotes go", SC(BUG) == "FT Versa Wing", SC(BUG))
for raw, want in (('"FT Versa Wing"', "FT Versa Wing"),                  # a fully-quoted phrase
                  ('!"FT Versa Wing".', "FT Versa Wing"),                # a full stop after the closing quote
                  ('?! "FT Versa Wing"', "FT Versa Wing"),               # more junk, a space
                  ("“FT Versa Wing”", "FT Versa Wing"),        # smart double quotes
                  ("!“FT Versa Wing”", "FT Versa Wing"),
                  ("‘FT Versa Wing’", "FT Versa Wing"),        # smart single quotes
                  ("'FT Versa Wing'", "FT Versa Wing"),
                  ('!"FT Versa Wing', "FT Versa Wing"),                  # the closing quote is missing
                  ("!“FT Versa Wing", "FT Versa Wing"),
                  ('"FT Versa Wing\'s manual"', "FT Versa Wing's manual"),  # an apostrophe inside the phrase survives
                  ):
    check(f"strip_caption_quotes({raw!r}) = {want!r}", SC(raw) == want, SC(raw))
check("...and a quote mark followed by more text keeps its quotes but loses the junk before it",
      SC('!"FT Versa Wing" is the title') == '"FT Versa Wing" is the title', SC('!"FT Versa Wing" is the title'))

# legit text must pass through untouched
for ok in ("a dog's toy", 'the "fast" car', '"fast" car with a red wing', "A red car on a table.",
           "The image shows a desk, with a lamp.", "(video) The blue chassis on carpet", "'Tis the season",
           "rock 'n' roll poster", "The dogs' toys on a rug", "Wow! A big red truck", ".NET book cover",
           "!!!!!!", "", "\"\"", 'Say "hello" and "goodbye"'):
    check(f"unchanged: {ok!r}", SC(ok) == ok, SC(ok))
check("None passes through", SC(None) is None)

# ---- title_from_caption never leaves unbalanced quotes or brackets -------------------------------------
check("the issue's caption gives a clean title", TT(BUG) == "FT Versa Wing", TT(BUG))
# negative control: the unchanged final step of the OLD pipeline leaves exactly the reported dangling quote
old = item_title.clean_short_title(BUG)
check("negative control: the old path (clean_short_title alone) gives '!\"FT Versa Wing' (unbalanced)",
      old == '!"FT Versa Wing' and not balanced(old), old)
check("...and the new title for the same caption IS balanced", balanced(TT(BUG)))

for cap, want in (('"FT Versa Wing"', "FT Versa Wing"),
                  ("“FT Versa Wing”", "FT Versa Wing"),
                  ('!"FT Versa Wing', "FT Versa Wing"),
                  ('The title reads "FT Versa Wing', "Title reads FT Versa Wing"),      # a quote nothing closes
                  ('A red car with a "fast stripe', "Red car with a fast stripe"),     # a lone opening quote goes
                  ("Bolt (M3 thread and a nut on a tray", "Bolt M3 thread and a nut"),  # 8-word cut, then "(" has no ")"
                  ("Washers) in a jar", "Washers in a jar"),                            # a closer nothing opened
                  ("?!Wing spar on a table", "Wing spar on a table"),
                  ("a dog's toy on a rug", "Dog's toy on a rug"),                       # apostrophe is not a quote
                  ("The dogs' toys on a rug", "Dogs' toys on a rug"),
                  ("Old A-frame (Mk2) in a field", "Old A-frame (Mk2) in a field"),     # balanced brackets stay
                  ("A red car on a table.", "Red car on a table"),
                  ("The image shows a green monster truck with six wheels, parked on a bench.",
                   "Green monster truck with six wheels"),
                  ("A green \"monster\" truck on a bench", 'Green "monster" truck on a bench'),   # inner pair stays
                  ):
    got = TT(cap)
    check(f"title_from_caption({cap[:34]!r}) = {want!r}", got == want, got)

# whatever goes in, a title that comes out is balanced and does not start with stray punctuation
fuzz = [BUG, '"', '""', '"a"', "((", ")(", "[x", "x]", "{x", "“x", "x”", "‘x", "x’", "'x", "x'",
        'He said "hi', 'He said hi"', "!(Wing) \"spar", ";:Wing, spar", "...", "...Wing", "a \"b\" \"c", "(a) (b",
        "The \"Quick\" brown fox jumps over the lazy dog and keeps going far away",
        '"Alpha beta gamma delta epsilon zeta eta theta iota kappa"', "rock 'n' roll club night",
        '“First” and “second']
bad = []
for cap in fuzz:
    t = TT(cap)
    if t is None:
        continue
    if not balanced(t) or t[0] in "!?,;:" or t[-1] in ",;:":
        bad.append((cap, t))
check("fuzz: no title has an unbalanced quote/bracket or stray edge punctuation", not bad, bad)
check("tidy_title is idempotent on the fuzz set", all(captions.tidy_title(captions.tidy_title(c)) == captions.tidy_title(c)
                                                      for c in fuzz))
check("no letters -> no title (unchanged)", TT("!!!!") is None and TT('"') is None)
check("existing behaviours kept: a fragment is still not a title", TT("urn with wires") is None)

# ---- the pipeline stores the clean caption and title -----------------------------------------------------
db.init_db()
ctx = actor.acting_as(actor.ACTOR_UI)
ctx.__enter__()
captions.DISABLED = False


def mk(slug):
    sf = f"{slug}.png"
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (20, 120, 20)).save(buf, "PNG")
    (paths.storage_dir() / sf).write_bytes(buf.getvalue())
    db.insert_upload(slug, f"{slug}.png", sf, "tester", media_type="image")
    return slug


def fake(answer):
    def caption_once(image_path, prompt=None, temperature=None, num_predict=None, label=None, keep_alive=0):
        return {"caption": answer, "elapsed_seconds": 0.0, "restarted": False, "restart_seconds": 0.0,
                "restart_reason": None, "error": None}
    captions.caption_once = caption_once


P = mk("quote0001")
fake(BUG)
captions._run_caption_now(P)
m = db.get_by_slug(P)["type_metadata"]
check("pipeline: auto_caption is stored without the stray ! and quotes, status done, step 0",
      m.get("auto_caption") == "FT Versa Wing" and m.get("auto_caption_status") == "done" and m.get("auto_caption_step") == 0, m)
check("pipeline: auto_caption_title is 'FT Versa Wing' (balanced)", m.get("auto_caption_title") == "FT Versa Wing", m)
N = mk("quote0002")
fake("A red car on a table with a \"fast\" stripe.")
captions._run_caption_now(N)
m = db.get_by_slug(N)["type_metadata"]
check("pipeline: an ordinary caption is stored exactly as the model said it",
      m.get("auto_caption") == 'A red car on a table with a "fast" stripe.' and m.get("auto_caption_title") == "Red car on a table", m)
ctx.__exit__(None, None, None)

print()
print(f"FAILED: {', '.join(FAILS)}" if FAILS else "ALL PASS")
sys.exit(1 if FAILS else 0)
