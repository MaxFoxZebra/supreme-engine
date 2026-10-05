"""Claude's changes, marked on the page like a highlighter.

The page view shows a CV or a letter as Typst laid it out. To show what an AI
client changed in place, the same Typst source is compiled again with a show
rule per new phrase that wraps it in `highlight`. Highlighting draws behind
the text and takes no room, so the marked page is the real page with the new
words lit; the caller still checks that no block moved before using it.

A string show rule matches every occurrence in the document, so a phrase that
also appears elsewhere is anchored by the words around it: an outer rule on
the longer, unique stretch applies the inner rule to that stretch only.
"""

from __future__ import annotations

import difflib
import re

TOKEN = re.compile(r"\s+|\w+(?:['’.,\-]\w+)*%?|[^\s]")
FILL = 'rgb("#c4efd1")'
CUT = 'rgb("#e0684f")'
MAX_RULES = 60


def texts(value) -> list[str]:
    """The printed strings of a CV value: a line, an entry's fields, its bullets."""
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, dict):
        out: list[str] = []
        for v in value.values():
            out += texts(v)
        return out
    if isinstance(value, list):
        out = []
        for v in value:
            out += texts(v)
        return out
    return []


def clean(s: str) -> str:
    """As it prints: Markdown links, bold, italic and code marks taken off,
    a list bullet's marker too, spacing folded."""
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", str(s))
    s = re.sub(r"(\*\*|__|`)", "", s)
    s = re.sub(r"(?<![\w*])[*_]([^*_\n]+)[*_](?![\w*])", r"\1", s)
    s = re.sub(r"(?m)^\s*(?:[-*+•]|\d+[.)])\s+", "", s)
    return " ".join(s.split())


def _tokens(s: str) -> list[str]:
    return TOKEN.findall(s)


def changed(pool: list[str], after: str) -> tuple[list[str], list[tuple[int, int]], list[tuple[int, bool]]]:
    """What is new in `after` against the closest text in `pool`: its tokens,
    the runs that are new as (start, end), and where words were cut as
    (index of the word beside the cut, whether the cut is after it). All of
    it is new when nothing in `pool` is close."""
    b = _tokens(after)
    if not b:
        return b, [], []
    best, score = None, 0.0
    for cand in pool:
        r = difflib.SequenceMatcher(None, cand, after, autojunk=False).ratio()
        if r > score:
            best, score = cand, r
    if best is None or score < 0.35:
        return b, [(0, len(b))], []
    a = _tokens(best)
    runs: list[list[int]] = []
    cuts: list[tuple[int, bool]] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        # Words replaced by punctuation alone were cut, not rewritten.
        if tag == "replace" and not any(re.search(r"\w", t) for t in b[j1:j2]):
            tag = "delete"
        if tag == "delete":
            if not any(re.search(r"\w", t) for t in a[i1:i2]):
                continue
            before = [k for k in range(j1) if re.search(r"\w", b[k])]
            after_ = [k for k in range(j1, len(b)) if re.search(r"\w", b[k])]
            if before:
                cuts.append((before[-1], True))
            elif after_:
                cuts.append((after_[0], False))
            continue
        if tag not in ("insert", "replace"):
            continue
        # A space alone between two new runs joins them into one.
        if runs and all(not t.strip() for t in b[runs[-1][1]:j1]):
            runs[-1][1] = j2
        else:
            runs.append([j1, j2])
    out = []
    for j1, j2 in runs:
        while j1 < j2 and not b[j1].strip():
            j1 += 1
        while j2 > j1 and not b[j2 - 1].strip():
            j2 -= 1
        if j2 > j1:
            out.append((j1, j2))
    return b, out, cuts


def _typ_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def rules(changes: list[tuple[list[str], list[str]]], document: list[str]) -> str:
    """Typst show rules marking what is new, and where words were cut.
    `changes` is, per changed block, (what it said before, what it says
    now); `document` every printed string of the document as it is now.
    Empty when there is nothing to mark."""
    whole = "\n".join(clean(t) for t in document)
    out: list[str] = []
    seen: set[tuple[str, str]] = set()

    def emit(toks: list[str], j1: int, j2: int, show: str) -> None:
        phrase = "".join(toks[j1:j2]).strip()
        if not re.search(r"\w", phrase) or (phrase, show) in seen:
            return
        seen.add((phrase, show))
        if whole.count(phrase) == 1:
            out.append(f"#show {_typ_str(phrase)}: {show}")
            return
        # Anchored by the words around it until it is the only one.
        lo, hi = j1, j2
        for step in range(16):
            if step % 2 == 0 and lo > 0:
                lo -= 1
            elif hi < len(toks):
                hi += 1
            elif lo > 0:
                lo -= 1
            else:
                return
            stretch = "".join(toks[lo:hi]).strip()
            if whole.count(stretch) == 1:
                out.append(f"#show {_typ_str(stretch)}: it => {{ show {_typ_str(phrase)}: {show}; it }}")
                return

    for before, after in changes:
        pool = [clean(t) for t in before]
        for text in after:
            toks, runs, cuts = changed(pool, clean(text))
            for j1, j2 in runs:
                emit(toks, j1, j2, "cvs-mark")
            for k, behind in cuts:
                emit(toks, k, k + 1, "w => [#w#cvs-cut]" if behind else "w => [#cvs-cut#w]")
            if len(out) >= MAX_RULES:
                break
    if not out:
        return ""
    return (f"#let cvs-mark(it) = highlight(fill: {FILL}, radius: 1.5pt, extent: 0.5pt, it)\n"
            # A cut is a thin red stroke at the place, taking no room.
            f"#let cvs-cut = box(width: 0pt, place(dx: 0.4pt, dy: -0.78em, box(width: 1.6pt, "
            f"height: 0.95em, fill: {CUT}, radius: 0.8pt)))\n"
            + "\n".join(out[:MAX_RULES]) + "\n")
