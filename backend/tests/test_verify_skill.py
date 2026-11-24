"""The verify skill's handles still exist in the frontend.

Every handle in SKILL.md and features/*.md is checked against frontend/src:
- a CSS selector in a code span or quoted in a Playwright call: each `.class` must appear in a .js/.jsx file
  (styles.css alone is dead), each `[attr]` name must appear, each `[attr="value"]` value must appear or be a DESK card id.
- `⌘K Title` must be a palette title (DESK in widgets/cards.js, LOOSE_PANELS in lib/palette.js) or action title.
- `src/...` paths must exist under frontend/.
To name a removed selector on purpose, keep it out of a code span.
"""

import re
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / ".claude" / "skills" / "verify"
FRONTEND = ROOT / "frontend"
SRC = FRONTEND / "src"

TAGS = {"a", "article", "aside", "button", "canvas", "details", "div", "form", "h1", "h2",
        "h3", "header", "img", "input", "label", "li", "nav", "ol", "section", "select",
        "span", "summary", "svg", "table", "td", "textarea", "th", "tr", "ul"}
SELECTOR = re.compile(r"^(?:[.\[]|(?:%s)[.\[])" % "|".join(sorted(TAGS)))
PLAYWRIGHT_CALL = re.compile(
    r"""(?:locator|click|dblclick|hover|fill|press|waitForSelector|\$\$?|\$eval|\$\$eval|"""
    r"""querySelector(?:All)?|isVisible|textContent|innerText|getAttribute)\(\s*(["'`])(.+?)\1""")
CLASS = re.compile(r"\.(-?[A-Za-z_][\w-]*)")
ATTR = re.compile(r"""\[([\w-]+)(?:[~^$*|]?=\s*["']?([^"'\]]+)["']?)?\]""")
PALETTE = re.compile(r"⌘K\s+([^`]+)$")


def skill_files():
    return [SKILL / "SKILL.md", *sorted((SKILL / "features").glob("*.md"))]


@cache
def source():
    return "\n".join(p.read_text() for p in SRC.rglob("*")
                     if p.suffix in {".js", ".jsx"})


@cache
def desk_ids():
    cards = (SRC / "widgets" / "cards.js").read_text()
    return set(re.findall(r'^\s*\["([\w-]+)",\s*"', cards, re.M))


@cache
def palette_titles():
    cards = (SRC / "widgets" / "cards.js").read_text()
    palette = (SRC / "lib" / "palette.js").read_text()
    titles = set(re.findall(r'^\s*\["[\w-]+",\s*"([^"]+)"', cards + palette, re.M))
    titles |= set(re.findall(r'title:\s*(?:dark \? )?"([^"]+)"', palette))
    titles |= set(re.findall(r'title:\s*dark \? "[^"]+" : "([^"]+)"', palette))
    return titles


def handles(text):
    """(kind, value) pairs named in one skill file."""
    out = []
    fenced = re.findall(r"```[^\n]*\n(.*?)```", text, re.S)
    prose = re.sub(r"```.*?```", "", text, flags=re.S)
    for span in re.findall(r"`([^`\n]+)`", prose):
        span = span.strip()
        if SELECTOR.match(span):
            out.append(("selector", span))
        elif m := PALETTE.match(span):
            out.append(("palette", m.group(1).strip()))
        elif re.fullmatch(r"src/[\w./-]+\.(?:jsx?|css)", span):
            out.append(("path", span))
    for block in fenced:
        out += [("selector", m.group(2)) for m in PLAYWRIGHT_CALL.finditer(block)
                if SELECTOR.match(m.group(2))]
    return out


def problems(kind, value):
    src = source()
    if kind == "path":
        return [] if (FRONTEND / value).exists() else [f"no file frontend/{value}"]
    if kind == "palette":
        return [] if value in palette_titles() else [f"no palette title {value!r}"]
    bad = []
    no_attrs = ATTR.sub("", value)
    for cls in CLASS.findall(no_attrs):
        if not re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(cls), src):
            bad.append(f"class .{cls} isn't in any .js/.jsx")
    for name, val in ATTR.findall(value):
        if not re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(name), src) and \
                not (name.startswith("data-") and re.search(
                    r"dataset\.%s\b" % re.escape(_camel(name[5:])), src)):
            bad.append(f"attribute [{name}] isn't in any .js/.jsx")
        elif val and val not in desk_ids() and f'"{val}"' not in src and f"'{val}'" not in src:
            bad.append(f'[{name}="{val}"]: no string "{val}" in src and not a DESK card id')
    return bad


def _camel(s):
    head, *rest = s.split("-")
    return head + "".join(w.title() for w in rest)


def test_skill_has_feature_files():
    feats = sorted((SKILL / "features").glob("*.md"))
    assert len(feats) >= 5, "the verify skill's feature map is missing"
    sections = ["## Sub-features", "## How to get to it (user POV)", "## Driving it", "## Gotchas"]
    for f in feats:
        if f.name == "README.md":
            continue
        text = f.read_text()
        missing = [s for s in sections if s not in text]
        assert not missing, f"{f.name} lacks {missing}"
    readme = (SKILL / "features" / "README.md").read_text()
    unlisted = [f.name for f in feats if f.name != "README.md" and f.name not in readme]
    assert not unlisted, f"features/README.md doesn't list {unlisted}"


def test_every_named_handle_exists():
    found, bad = 0, []
    for f in skill_files():
        for kind, value in handles(f.read_text()):
            found += 1
            bad += [f"{f.relative_to(SKILL)}: `{value}`: {p}" for p in problems(kind, value)]
    assert found > 50, f"only {found} handles extracted; is the parser still matching the skill?"
    assert not bad, "the verify skill names handles the frontend no longer has:\n" + "\n".join(bad)
