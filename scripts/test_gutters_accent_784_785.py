#!/usr/bin/env python3
"""#784 (even page gutters) and #785 (lavender accents become hazard yellow). Static, no server, no DB:

    python scripts/test_gutters_accent_784_785.py

  1. No lavender left: no --purple* variable anywhere in web/ (CSS, templates, JS), and no
     purple-hued colour literal (hex, rgb(), hsl()) in web/static or web/templates, except the
     KEPT list below (each with its reason).
  2. One accent: style.css :root builds --hazard-yellow from --accent-yellow-hue / -light, the
     --highlight set derives from it, the dial's needle (--tuner-yellow) shares it, and the places
     the lavender used to be (rail tabs, active chip, Grid / List, card edges, card-page timeline
     blocks) use it. A yellow fill always has --highlight-ink text.
  3. Contrast (WCAG 2.x): dark ink on the yellow, the yellow on the dark grounds, cream text on the
     muted blocks all >= 4.5:1 at the shipped values AND across the tuning panel's whole range.
  4. Gutters: .app-main and .app-header pad left and right from the one --rail-gutter (left via
     --rail-gutter-left, equal to it on wide screens); nothing else re-pads .app-main (the detail
     pages' 96px override is gone); under 760px the left gutter is small and the right one rail-wide.
  5. The dev tuning panel exposes the accent: accentHue / accentLight in DIAL_DEFAULTS and TUNABLES
     (so the panel's Reset and Copy values carry them), setTuning pushes the CSS variables, and the
     JS defaults equal the CSS ones.
Prints PASS/FAIL per check, then FAILED: <names> or ALL PASS.
"""

import colorsys
import os
import re

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
FAILS = []

# Purple-ish literals kept on purpose: (path relative to the repo, the literal, why).
KEPT = [
    ("web/static/css/cards.css", "#563A86", "V2 card face: the Project kind's frame colour (a card-kind code, not the UI accent)"),
    ("web/static/css/cards.css", "#7A3F70", "V2 card face: the Family kind's frame colour"),
    ("web/static/css/cards.css", "#E6DDF3", "V2 card face: the Project kind's name band"),
    ("web/static/css/cards.css", "#EBDDE8", "V2 card face: the Family kind's name band"),
]
SKIP_DIRS = ("web/static/vendor",)  # third-party themes (highlight.js), not ours to recolour


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def strip_comments(css):
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def rules(css):
    """(selector, body) for every innermost rule, comments removed (media blocks flattened)."""
    return [(s.strip(), b) for s, b in re.findall(r"([^{}]+)\{([^{}]*)\}", strip_comments(css))]


def rule(css_rules, sel):
    return next((b for s, b in css_rules if s == sel), "")


# --- 1. no lavender left ----------------------------------------------------------------------------
def hue_is_purple(r, g, b):
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    return 250 <= h * 360 <= 330 and s > 0.15 and 0.08 < l < 0.95


# 3-digit hex only as a CSS value or a quoted string (`: #abc;`, `'#abc'`), so issue numbers like
# (#515, #714) in comments aren't read as colours.
LITERAL = re.compile(
    r"#([0-9a-fA-F]{8}|[0-9a-fA-F]{6})\b|(?:(?<=:)|(?<=: )|(?<=['\"]))#([0-9a-fA-F]{3})(?=[;'\"\s!])"
    r"|rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)|hsla?\(\s*([0-9.]+)")
found_var, found_lit = [], []
for top in ("web/static", "web/templates"):
    for dirpath, _, files in os.walk(os.path.join(ROOT, top)):
        rel_dir = os.path.relpath(dirpath, ROOT).replace(os.sep, "/")
        if rel_dir.startswith(SKIP_DIRS):
            continue
        for fn in files:
            if not fn.endswith((".css", ".html", ".js")):
                continue
            rel = f"{rel_dir}/{fn}"
            text = read(rel)
            if re.search(r"--purple|--lavender", text):
                found_var.append(rel)
            for m in LITERAL.finditer(text):
                if m.group(1) or m.group(2):
                    h = (m.group(1) or m.group(2))[:6]
                    if len(h) == 3:
                        h = "".join(c * 2 for c in h)
                    rgb = tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
                    lit = "#" + (m.group(1) or m.group(2))
                elif m.group(3):
                    rgb = tuple(int(m.group(i)) for i in (3, 4, 5))
                    lit = m.group(0)
                else:
                    if 250 <= float(m.group(6)) <= 330:
                        found_lit.append((rel, m.group(0)))
                    continue
                if hue_is_purple(*rgb) and not any(k[0] == rel and k[1].lower() == lit.lower() for k in KEPT):
                    found_lit.append((rel, lit))
check("no --purple / --lavender variable left in web/", not found_var, found_var)
check("no purple colour literal left (outside the KEPT list)", not found_lit, found_lit)
for rel, lit, why in KEPT:
    check(f"KEPT literal still exists, so the list stays honest: {lit} ({why})", lit.lower() in read(rel).lower())

# --- 2. one accent ----------------------------------------------------------------------------------
css = read("web/static/style.css")
R = rules(css)
roots = [b for s, b in R if s == ":root"]
root = "\n".join(roots)


def var(name, body=root):
    m = re.search(re.escape(name) + r":\s*([^;]+);", body)
    return m.group(1).strip() if m else ""


check("accent: --hazard-yellow is built from the accent hue and lightness",
      re.fullmatch(r"hsl\(var\(--accent-yellow-hue\) \d+% var\(--accent-yellow-light\)\)", var("--hazard-yellow")) is not None,
      var("--hazard-yellow"))
check("accent: --highlight is the hazard yellow", var("--highlight") == "var(--hazard-yellow)", var("--highlight"))
check("accent: the needle shares it (--tuner-yellow: var(--hazard-yellow))", var("--tuner-yellow") == "var(--hazard-yellow)")
for v in ("--highlight-hover", "--highlight-tint", "--highlight-glow", "--highlight-muted", "--highlight-muted-edge",
          "--tuner-yellow-glow", "--tuner-tick-glow"):
    check(f"accent: {v} follows the accent hue", "var(--accent-yellow-hue)" in var(v), var(v))
check("accent: --highlight-ink is the dark hazard black", var("--highlight-ink") == "var(--hazard-black)")
check("accent: no other :root colour is purple-named", not re.search(r"--(purple|lavender|violet)", root))
USES = {
    ".nav-tab": "border: 3px solid var(--highlight)",
    ".nav-rail-left .nav-tab": "border: 3px solid var(--highlight)",
    ".tag-chip.active": "background: var(--highlight)",
    ".layout-toggle-btn.active": "background: var(--highlight)",
    ".tag-suggestion-item:hover, .tag-suggestion-item.active": "background: var(--highlight)",
    ".project-card": "var(--highlight-muted)",
    ".project-subproject-card": "var(--highlight-muted)",
    ".project-subproject-label": "color: var(--highlight)",
    ".project-video-timeline-block": "background: var(--highlight-muted)",
    ".project-cover-selector:checked, .project-writeup-selector:checked": "background: var(--highlight)",
}
for sel, want in USES.items():
    check(f"accent used: {sel}", want in rule(R, sel), rule(R, sel).strip()[:160])
# Every rule with a yellow FILL must set dark ink text (a light label on yellow fails contrast).
for sel, body in R:
    if re.search(r"background:\s*var\(--highlight(-hover)?\)", body) and re.search(r"(^|[\s;])color:", body):
        check(f"contrast: {sel} puts dark ink on its yellow fill", "color: var(--highlight-ink)" in body, body.strip()[:160])
check("focus: the active Grid / List button's ring is dark on its yellow",
      "outline-color: var(--highlight-ink)" in rule(R, ".layout-toggle-btn.active:focus-visible"))
check("focus: the chips have a visible ring", "outline: 2px solid var(--highlight)" in rule(R, ".tag-chip:focus-visible"))


# --- 3. contrast -----------------------------------------------------------------------------------
def hx(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def hsl(h, s, l):
    return colorsys.hls_to_rgb((h % 360) / 360, l / 100, s / 100)


def lum(c):
    f = lambda v: v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4  # noqa: E731
    r, g, b = map(f, c)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(a, b):
    la, lb = lum(a), lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def over(fg, alpha, bg):
    return tuple(fg[i] * alpha + bg[i] * (1 - alpha) for i in range(3))


hue = float(var("--accent-yellow-hue") or "nan")
light = float((var("--accent-yellow-light") or "nan%").rstrip("%"))
sat = float(re.search(r"\) (\d+)% var", var("--hazard-yellow")).group(1))
muted = re.search(r"\) (\d+)% (\d+)%\)", var("--highlight-muted"))
INK, TEXT = hx(var("--hazard-black")), hx(var("--text"))
GROUNDS = {n: hx(var(n)) for n in ("--bg", "--card", "--surface", "--chip")}
yellow = hsl(hue, sat, light)
check("contrast: the shipped yellow is the needle's #E8B93A (within 1 step)",
      all(abs(a - b) <= 1.5 / 255 for a, b in zip(yellow, hx("#E8B93A"))), [round(v * 255) for v in yellow])
check("contrast: dark ink on the yellow >= 4.5", ratio(INK, yellow) >= 4.5, round(ratio(INK, yellow), 2))
for n, g in GROUNDS.items():
    check(f"contrast: yellow on {n} >= 4.5", ratio(yellow, g) >= 4.5, round(ratio(yellow, g), 2))
check("contrast: yellow label text on its tint >= 4.5",
      ratio(yellow, over(yellow, 0.16, GROUNDS["--surface"])) >= 4.5)
mutedc = hsl(hue, float(muted.group(1)), float(muted.group(2)))
check("contrast: cream text on the muted timeline blocks >= 4.5", ratio(TEXT, mutedc) >= 4.5, round(ratio(TEXT, mutedc), 2))
print(f"  (shipped: ink on yellow {ratio(INK, yellow):.2f}:1, yellow on --card {ratio(yellow, GROUNDS['--card']):.2f}:1, "
      f"cream on muted {ratio(TEXT, mutedc):.2f}:1)")

js = read("web/static/js/radio-dial.js")


def tunable(key):
    m = re.search(r"\{ key: '" + key + r"'[^}]*min: ([0-9.]+), max: ([0-9.]+)", js)
    return (float(m.group(1)), float(m.group(2))) if m else None


hr, lr = tunable("accentHue"), tunable("accentLight")
if hr and lr:
    worst_ink = min(ratio(INK, hsl(h, sat, l)) for h in range(int(hr[0]), int(hr[1]) + 1) for l in range(int(lr[0]), int(lr[1]) + 1))
    worst_card = min(ratio(hsl(h, sat, l), GROUNDS["--card"]) for h in range(int(hr[0]), int(hr[1]) + 1)
                     for l in range(int(lr[0]), int(lr[1]) + 1))
    check("contrast: across the tuning panel's whole accent range, ink on yellow >= 4.5", worst_ink >= 4.5, round(worst_ink, 2))
    check("contrast: across the tuning panel's whole accent range, yellow on --card >= 4.5", worst_card >= 4.5, round(worst_card, 2))

# --- 4. gutters -------------------------------------------------------------------------------------
wide_root = roots[0] if roots else ""
check("gutter: one --rail-gutter sized from the rail width", "var(--rail-width)" in var("--rail-gutter", wide_root))
check("gutter: wide screens: left gutter = right gutter", var("--rail-gutter-left", wide_root) == "var(--rail-gutter)")
main_rules = [b for s, b in R if re.search(r"(^|,|\s)\.app-main(\.dp-page)?(\s*$|\s*,)", s)]
check("gutter: one .app-main rule", len(main_rules) == 1, len(main_rules))
pad = re.search(r"padding:\s*([^;]+);", main_rules[0] if main_rules else "")
check("gutter: .app-main pads right with --rail-gutter and left with --rail-gutter-left",
      bool(pad) and pad.group(1).split() == ["32px", "var(--rail-gutter)", "32px", "var(--rail-gutter-left)"], pad and pad.group(1))
check("gutter: .app-header uses the same gutters",
      "padding: 0 var(--rail-gutter) 0 var(--rail-gutter-left)" in rule(R, ".app-header"))
all_css = css + read("web/static/css/details.css") + read("web/static/css/cards.css") + read("web/static/css/curator-queue.css")
check("gutter: nothing else re-pads .app-main (the detail pages' 96px override is gone)",
      not re.search(r"\.app-main[^{]*\{[^}]*padding-(left|right)", strip_comments(all_css)))
narrow = re.search(r"@media \(max-width: 760px\) \{\s*:root \{([^}]*)\}", strip_comments(css))
check("gutter: under 760px the right gutter stays rail-wide", bool(narrow) and "var(--rail-width)" in var("--rail-gutter", narrow.group(1)))
check("gutter: under 760px the left gutter is small (a fixed px, under the rail)",
      bool(narrow) and re.fullmatch(r"\d+px", var("--rail-gutter-left", narrow.group(1)) or "") is not None
      and int(var("--rail-gutter-left", narrow.group(1))[:-2]) < int(re.search(r"(\d+)px", var("--rail-width", wide_root)).group(1)))

# --- 5. the dev tuning panel's accent control --------------------------------------------------------
defaults = re.search(r"const DIAL_DEFAULTS = \{(.*?)\n\};", js, re.S)
d = defaults.group(1) if defaults else ""
check("tuning: DIAL_DEFAULTS has accentHue and accentLight", "accentHue:" in d and "accentLight:" in d)
check("tuning: TUNABLES has an accent hue slider and a tint slider", bool(hr and lr), (hr, lr))
js_hue = re.search(r"accentHue:\s*([0-9.]+)", d)
js_light = re.search(r"accentLight:\s*([0-9.]+)", d)
check("tuning: JS accent defaults equal style.css :root",
      bool(js_hue and js_light) and float(js_hue.group(1)) == hue and float(js_light.group(1)) == light,
      (js_hue and js_hue.group(1), js_light and js_light.group(1), hue, light))
check("tuning: setTuning pushes --accent-yellow-hue and --accent-yellow-light",
      "setProperty('--accent-yellow-hue'" in js and "setProperty('--accent-yellow-light', `${this.tuning.accentLight}%`)" in js)
panel = read("web/static/js/radio-dial-tuning.js")
check("tuning: the panel builds one slider per TUNABLES entry (so the accent gets one)", "RadioDial.TUNABLES.forEach" in panel)
check("tuning: Reset goes back to DEFAULTS and Copy copies dial.tuning (accent included)",
      "dial.setTuning(defaults)" in panel and "JSON.stringify(dial.tuning" in panel)
check("tuning: the panel's own slider styling is untouched (no accent in its rules)",
      not any("highlight" in b or "hazard-yellow" in b for s, b in R if s.startswith(".radio-dial-tune")))

print("FAILED: " + ", ".join(FAILS) if FAILS else "ALL PASS")
