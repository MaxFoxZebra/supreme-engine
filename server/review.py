"""What an AI client changed, kept until you have looked at it.

The provenance marks answer "who last wrote this field". They cannot answer
the question that matters after a model has been at a document: what exactly
is different now, and do I want it. For that the document has to be compared
with what it was before the model started, so the first time an AI client
writes a document, the text it had is kept here. Every later write by any
client leaves that text alone, so the comparison covers all of them at once,
until you have kept or undone every change and there is nothing left to show.

A change is shown and resolved in units a person would decide about: a
paragraph of a letter, a line of its header, one entry of a CV, one field of
its header, a section added or removed. Keeping a unit folds it into the
kept text, so it stops being shown; undoing one puts the document back as it
was for that unit only, and leaves the rest of what the model wrote.

Called from the MCP process to record and from the app to show and resolve.
Standard library and ruamel only, so both can import it.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import io
import json
import re
import time
from pathlib import Path

FILE = ".cvstudio-review.json"


# --------------------------------------------------------------------------
# The record
# --------------------------------------------------------------------------

def _read(ws: Path) -> dict:
    try:
        data = json.loads((ws / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"docs": {}}
    if not isinstance(data, dict) or not isinstance(data.get("docs"), dict):
        return {"docs": {}}
    return data


def _write(ws: Path, data: dict) -> None:
    # Bookkeeping never breaks a save: the documents are the record.
    try:
        (ws / FILE).write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    except OSError:
        pass


def stamp(ws: Path) -> float | None:
    try:
        return (ws / FILE).stat().st_mtime
    except OSError:
        return None


def checkpoint(ws: Path, rel: str, before: str | None, *, by: str, agent: str | None,
               tool: str) -> None:
    """Note that an AI client changed `rel`, which held `before` until now.

    `before` is None for a document the client created. Only the first write
    since the last review keeps its text; later ones only add to the count.
    """
    data = _read(ws)
    now = time.time()
    entry = data["docs"].get(rel)
    if entry is None:
        entry = {"before": before, "created": before is None, "since": now,
                 "writes": 0, "tools": []}
        data["docs"][rel] = entry
    entry["at"] = now
    entry["by"] = by
    if agent:
        entry["agent"] = agent
    entry["writes"] = int(entry.get("writes") or 0) + 1
    if tool not in entry["tools"]:
        entry["tools"] = (entry["tools"] + [tool])[-8:]
    _write(ws, data)


def pending(ws: Path) -> dict:
    """{path: summary} for every document with changes waiting, for badges."""
    out = {}
    for rel, e in _read(ws)["docs"].items():
        if (ws / rel).is_file():
            out[rel] = {k: e.get(k) for k in ("by", "agent", "since", "at", "writes", "created")}
    return out


def entry(ws: Path, rel: str) -> dict | None:
    e = _read(ws)["docs"].get(rel)
    if e is not None and not (ws / rel).is_file():
        return None
    return e


def clear(ws: Path, rel: str) -> None:
    data = _read(ws)
    if data["docs"].pop(rel, None) is not None:
        _write(ws, data)


def set_before(ws: Path, rel: str, text: str) -> None:
    data = _read(ws)
    if rel in data["docs"]:
        data["docs"][rel]["before"] = text
        data["docs"][rel]["created"] = False
        _write(ws, data)


def moved(ws: Path, old: str, new: str | None) -> None:
    """A document renamed or deleted takes its pending review with it."""
    data = _read(ws)
    e = data["docs"].pop(old, None)
    if e is None:
        return
    if new:
        data["docs"][new] = e
    _write(ws, data)


def signature(before: str | None, now: str) -> str:
    """What a resolve request was looking at, so a stale one is refused
    rather than applied to a document that moved on under it."""
    raw = (before if before is not None else "\0created") + "\0\0" + now
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------
# Aligning two sequences
# --------------------------------------------------------------------------

def _align(bkeys: list, akeys: list, same) -> list[tuple[str, int | None, int | None]]:
    """(tag, before index, after index) for every item of either side, in
    order. tag is eq, chg, ins or del. Items are matched on their key; a
    matched pair whose content differs is chg, and a replaced run is paired
    off item by item, so an edited paragraph reads as edited rather than as
    one deleted and another added."""
    ops: list = []
    sm = difflib.SequenceMatcher(None, bkeys, akeys, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                ops.append(("eq" if same(i1 + k, j1 + k) else "chg", i1 + k, j1 + k))
        elif tag == "replace":
            n = min(i2 - i1, j2 - j1)
            for k in range(n):
                ops.append(("chg", i1 + k, j1 + k))
            for k in range(i1 + n, i2):
                ops.append(("del", k, None))
            for k in range(j1 + n, j2):
                ops.append(("ins", None, k))
        elif tag == "delete":
            for k in range(i1, i2):
                ops.append(("del", k, None))
        else:
            for k in range(j1, j2):
                ops.append(("ins", None, k))
    return ops


def _rebuild(ops, before: list, after: list, chosen: set, side: str) -> list:
    """One side of an aligned list with the chosen units moved across.

    side "doc": the document, with the chosen units undone.
    side "base": the kept text, with the chosen units kept.
    """
    out = []
    for n, (tag, bi, ai) in enumerate(ops):
        pick = n in chosen
        if side == "doc":
            if tag == "eq" or (tag == "chg" and not pick) or (tag == "ins" and not pick):
                out.append(after[ai])
            elif tag in ("chg", "del") and pick:
                out.append(before[bi])
        else:
            if tag == "eq" or (tag == "chg" and not pick) or (tag == "del" and not pick):
                out.append(before[bi])
            elif tag in ("chg", "ins") and pick:
                out.append(after[ai])
    return out


# --------------------------------------------------------------------------
# Letters
# --------------------------------------------------------------------------

LETTER_FIELDS = {"subject": "Subject", "to": "Addressed to", "place": "Written from",
                 "date": "Date", "language": "Language", "looks_like": "Letterhead from",
                 "letterhead": "Letterhead"}


def _chunks(body: str) -> list[str]:
    return [c.strip("\n") for c in re.split(r"\n\s*\n", (body or "").strip()) if c.strip()]


def _chunk_key(chunk: str) -> str:
    """A paragraph as the page prints it: its line breaks are not a change."""
    lines = [l.strip() for l in chunk.split("\n") if l.strip()]
    if all(re.match(r"^[-*•]\s+", l) for l in lines):
        return "\n".join("- " + re.sub(r"^[-*•]\s+", "", l) for l in lines)
    return " ".join(lines)


def letter_units(letters, before: str | None, now: str) -> list[dict]:
    if before is None:
        return [{"id": "created", "kind": "created", "label": "New letter"}]
    bm, bb = letters.parse(before)
    am, ab = letters.parse(now)
    units = []
    for k in list(dict.fromkeys(list(LETTER_FIELDS) + [k for k in {**bm, **am}
                                                         if k not in ("application", "company")])):
        if bm.get(k) != am.get(k) and not (bm.get(k) in (None, "", []) and am.get(k) in (None, "", [])):
            units.append({"id": "meta:" + k, "kind": "field",
                          "label": LETTER_FIELDS.get(k, k.replace("_", " ").capitalize()),
                          "before": bm.get(k), "after": am.get(k)})
    bc, ac = _chunks(bb), _chunks(ab)
    bk, ak = [_chunk_key(c) for c in bc], [_chunk_key(c) for c in ac]
    ops = _align(bk, ak, lambda i, j: bk[i] == ak[j])
    seen = 0  # paragraphs of the letter as it is now, before this one
    for n, (tag, bi, ai) in enumerate(ops):
        at = seen
        if ai is not None:
            seen += 1
        if tag == "eq":
            continue
        # `at` is where it sits on the page now; for a removed paragraph,
        # the place it was taken from.
        units.append({"id": f"body:{n}", "kind": "para", "tag": tag,
                      "label": {"chg": "Paragraph changed", "ins": "Paragraph added",
                                "del": "Paragraph removed"}[tag],
                      "at": at,
                      "before": bk[bi] if bi is not None else None,
                      "after": ak[ai] if ai is not None else None})
    return units


def letter_resolve(letters, before: str, now: str, ids: set, action: str) -> tuple[str, str]:
    """(new document text, new kept text) with `ids` kept or undone."""
    bm, bb = letters.parse(before)
    am, ab = letters.parse(now)
    bm, am = dict(bm), dict(am)
    for uid in ids:
        if uid.startswith("meta:"):
            k = uid[5:]
            if action == "undo":
                am[k] = copy.deepcopy(bm.get(k))
            else:
                bm[k] = copy.deepcopy(am.get(k))
    bc, ac = _chunks(bb), _chunks(ab)
    bk, ak = [_chunk_key(c) for c in bc], [_chunk_key(c) for c in ac]
    ops = _align(bk, ak, lambda i, j: bk[i] == ak[j])
    chosen = {int(u[5:]) for u in ids if u.startswith("body:")}
    if action == "undo":
        ac = _rebuild(ops, bc, ac, chosen, "doc")
    else:
        bc = _rebuild(ops, bc, ac, chosen, "base")
    return (letters.dump(am, "\n\n".join(ac)), letters.dump(bm, "\n\n".join(bc)))


# --------------------------------------------------------------------------
# CVs
# --------------------------------------------------------------------------

HEADER_LABELS = {"name": "Name", "headline": "Headline", "location": "Location",
                 "email": "Email", "phone": "Phone", "website": "Website",
                 "social_networks": "Social networks", "photo": "Photo"}
TOP_LABELS = {"design": "Design", "locale": "Language and dates", "settings": "Settings",
              "rendercv_settings": "Settings"}


def _section_label(name: str) -> str:
    return str(name).replace("_", " ").capitalize()


def _entry_key(entry, i: int, entry_title) -> str:
    if isinstance(entry, dict):
        return "t:" + entry_title(entry, i)
    return "s:" + json.dumps(entry, sort_keys=True, default=str)


def _sections(data) -> dict:
    cv = (data or {}).get("cv") or {}
    s = cv.get("sections") if isinstance(cv, dict) else None
    return s if isinstance(s, dict) else {}


def _entry_ops(bl: list, al: list, entry_title):
    bk = [_entry_key(e, i, entry_title) for i, e in enumerate(bl)]
    ak = [_entry_key(e, i, entry_title) for i, e in enumerate(al)]
    return _align(bk, ak, lambda i, j: bl[i] == al[j])


def cv_units(before: dict | None, now: dict | None, entry_title) -> list[dict]:
    before, now = before or {}, now or {}
    units = []
    bcv, acv = before.get("cv") or {}, now.get("cv") or {}
    for k in dict.fromkeys(list(bcv) + list(acv)):
        if k == "sections" or bcv.get(k) == acv.get(k):
            continue
        units.append({"id": "cv:" + k, "kind": "field",
                      "label": HEADER_LABELS.get(k, k.replace("_", " ").capitalize()),
                      "where": "Header", "before": bcv.get(k), "after": acv.get(k)})
    bs, as_ = _sections(before), _sections(now)
    for name in dict.fromkeys(list(as_) + list(bs)):
        bl, al = bs.get(name), as_.get(name)
        if bl == al:
            continue
        if not isinstance(bl, list) or not isinstance(al, list):
            units.append({"id": "sec:" + name, "kind": "section",
                          "label": ("Section added" if bl is None else
                                    "Section removed" if al is None else "Section changed"),
                          "where": _section_label(name), "section": name,
                          "before": bl, "after": al})
            continue
        for n, (tag, bi, ai) in enumerate(_entry_ops(bl, al, entry_title)):
            if tag == "eq":
                continue
            e = al[ai] if ai is not None else bl[bi]
            i = ai if ai is not None else bi
            units.append({"id": f"ent:{name}:{n}", "kind": "entry", "tag": tag,
                          "label": {"chg": "Changed", "ins": "Added", "del": "Removed"}[tag],
                          "where": _section_label(name) + " › " + (
                              entry_title(e, i) if isinstance(e, dict) else _section_label(name)),
                          "section": name, "i": ai,
                          "before": bl[bi] if bi is not None else None,
                          "after": al[ai] if ai is not None else None})
    for k in dict.fromkeys(list(before) + list(now)):
        if k == "cv" or before.get(k) == now.get(k):
            continue
        units.append({"id": "top:" + k, "kind": "block",
                      "label": TOP_LABELS.get(k, k.replace("_", " ").capitalize()),
                      "where": "Document", "before": before.get(k), "after": now.get(k)})
    return units


def _set(mapping, key, value) -> None:
    if value is None:
        if key in mapping:
            del mapping[key]
    else:
        mapping[key] = copy.deepcopy(value)


def cv_resolve(yaml_rt, to_plain, entry_title, before: str, now: str, ids: set,
               action: str) -> tuple[str, str]:
    """(new document text, new kept text), through ruamel so the comments in
    both survive."""
    doc = yaml_rt.load(now)
    base = yaml_rt.load(before)
    src, dst = (base, doc) if action == "undo" else (doc, base)
    pdoc, pbase = to_plain(doc), to_plain(base)
    by_section: dict[str, set] = {}
    for uid in ids:
        kind, _, rest = uid.partition(":")
        if kind == "top":
            _set(dst, rest, src.get(rest))
        elif kind == "cv":
            _set(dst["cv"], rest, (src.get("cv") or {}).get(rest))
        elif kind == "sec":
            if dst["cv"].get("sections") is None:
                dst["cv"]["sections"] = {}
            _set(dst["cv"]["sections"], rest, _sections(src).get(rest))
        elif kind == "ent":
            name, _, n = rest.rpartition(":")
            by_section.setdefault(name, set()).add(int(n))
    for name, chosen in by_section.items():
        bl, al = _sections(pbase).get(name) or [], _sections(pdoc).get(name) or []
        ops = _entry_ops(bl, al, entry_title)
        rb, ra = list(_sections(base).get(name) or []), list(_sections(doc).get(name) or [])
        side = "doc" if action == "undo" else "base"
        items = [copy.deepcopy(x) for x in _rebuild(ops, rb, ra, chosen, side)]
        target = doc if action == "undo" else base
        seq = _sections(target).get(name)
        if seq is None:
            continue
        del seq[:]
        seq.extend(items)
        # A section with nothing in it cannot be typed, so it does not render.
        if not len(seq):
            del target["cv"]["sections"][name]

    def dump(d) -> str:
        buf = io.StringIO()
        yaml_rt.dump(d, buf)
        return buf.getvalue()
    return dump(doc), dump(base)
