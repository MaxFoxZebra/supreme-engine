"""Multi-language CV families, translation drift tracking, and design sync."""

from __future__ import annotations

import io
import re
import time
from pathlib import Path

from ruamel.yaml.comments import CommentedMap

from .context import (
    cjkfonts,
    ctx,
    languages,
    rel,
    safe_path,
    to_plain,
    yaml_rt,
)
from . import documents as _docs
from . import history_scans as _hist
from . import provenance as _prov
from . import workspace as _ws


# --------------------------------------------------------------------------
# Languages
#
# A CV's language is RenderCV's `locale.language`. A translation is a second
# document, saved beside its source as <stem>.<code>.yaml and linked to it in
# the sidecar with a copy of what the source said when it was translated. That
# copy is what lets the translation say what it is missing when the source
# moves on: the source now against the source then, never a guess about the
# translated text itself. CV Studio writes the scaffold -- the locale, the
# section titles, everything untouched that should stay untouched -- and the
# prose is translated by the user or by their AI client, through MCP.
# --------------------------------------------------------------------------


def translation_link(path: Path) -> dict | None:
    """What this document is a translation of, as recorded, or None."""
    t = ((_prov._edits_read()["docs"].get(rel(path)) or {}).get("translation")) or None
    return t if t and t.get("of") else None


def translations_of(path: Path) -> list[dict]:
    """Every translation of this document, as [{path, lang}]."""
    me = rel(path)
    out = []
    for p, doc in _prov._edits_read()["docs"].items():
        t = doc.get("translation") or {}
        if t.get("of") == me and safe_path(p).exists():
            out.append({"path": p, "lang": t.get("lang")})
    return sorted(out, key=lambda t: t["path"])


def language_family(path: Path) -> dict:
    """This document, its source if it is a translation, and the source's other
    translations: the set a language switch moves between."""
    link = translation_link(path)
    root = safe_path(link["of"]) if link else path
    members = [{"path": rel(root), "lang": _doc_lang(root), "source": True}]
    members += [{**t, "source": False} for t in translations_of(root)]
    return {"source": rel(root), "members": members if len(members) > 1 or link
            else members}


def _doc_lang(path: Path) -> str:
    try:
        return languages.file_language(path.read_text(encoding="utf-8"))
    except OSError:
        return "en"


def add_language(path: Path, code: str) -> dict:
    """Write the translation scaffold of `path` in language `code`.

    The copy is the source with its locale set, its section titles translated
    where CV Studio knows them, and nothing else changed: names, contact
    details, links, dates, company names and the design stay exactly as they
    are. The text is left in the source language for the user, or their AI
    client, to translate. Returns what was written and what is left to do.
    """
    if code not in languages.LANGS:
        raise ValueError(f"CV Studio cannot print a CV in {code!r}.")
    link = translation_link(path)
    if link:  # translate from the source, not from another translation
        path = safe_path(link["of"])
    src_lang = _doc_lang(path)
    if code == src_lang:
        raise ValueError(f"{rel(path)} is already in {languages.LANGS[code][2]}.")
    for t in translations_of(path):
        if t["lang"] == code:
            raise ValueError(f"{rel(path)} already has a {languages.LANGS[code][2]} "
                             f"version: {t['path']}.")
    stem = re.sub(r"\.[a-z]{2}$", "", path.stem)
    dest = path.with_name(f"{stem}.{code}{path.suffix}")
    if dest.exists():
        raise ValueError(f"{rel(dest)} already exists.")
    data = yaml_rt.load(path.read_text(encoding="utf-8"))
    cv = data.get("cv") or {}
    renamed, kept = {}, []
    secs = cv.get("sections")
    if isinstance(secs, dict):
        fresh = CommentedMap()
        for key, val in secs.items():
            title = languages.section_title(str(key), code)
            if title and title != key:
                renamed[str(key)] = title
                fresh[title] = val
            else:
                if not title:
                    kept.append(str(key))
                fresh[key] = val
        cv["sections"] = fresh
    loc = CommentedMap()
    loc["language"] = languages.LANGS[code][0]
    if code in languages.PRESENT:
        loc["present"] = languages.PRESENT[code]
    data["locale"] = loc
    import io
    buf = io.StringIO()
    yaml_rt.dump(data, buf)
    dest.write_text(buf.getvalue(), encoding="utf-8")
    source_cv = to_plain(yaml_rt.load(path.read_text(encoding="utf-8"))).get("cv")
    record = _prov._edits_read()
    record["docs"].setdefault(rel(dest), {})["translation"] = {
        "of": rel(path), "lang": code, "from_lang": src_lang, "at": time.time(),
        "keys": renamed, "synced": source_cv, "synced_at": time.time()}
    _prov._edits_write(record)
    out = {"path": rel(dest), "of": rel(path), "lang": code, "from_lang": src_lang,
           "sections_titled": renamed, "sections_to_title": kept}
    # Fetched now rather than at the first render, so it is usually there by
    # the time the copy is translated.
    if cjkfonts.fetch([code]):
        _, name, mb = cjkfonts.CUTS[code]
        out["font"] = {"language": name, "mb": mb}
    return out


def set_cv_language(path: Path, code: str) -> dict:
    """Print a CV in another language: its locale, and nothing else.

    What Settings writes when the app is used in one language. Dates, month
    names and "present" follow; the text stays as written.
    """
    if code not in languages.LANGS:
        raise ValueError(f"CV Studio cannot print a CV in {code!r}.")
    import io
    data = yaml_rt.load(path.read_text(encoding="utf-8"))
    loc = data.get("locale")
    if not isinstance(loc, dict):
        loc = CommentedMap()
        data["locale"] = loc
    loc["language"] = languages.LANGS[code][0]
    if code in languages.PRESENT:
        loc["present"] = languages.PRESENT[code]
    else:
        # Left behind from another language it would print in that one, and
        # empty RenderCV refuses the file.
        loc.pop("present", None)
    buf = io.StringIO()
    yaml_rt.dump(data, buf)
    path.write_text(buf.getvalue(), encoding="utf-8")
    return {"path": rel(path), "lang": code}


def _map_to_translation(path_: list, keys: dict) -> list:
    """A field's path in the source, as the same field's path in a
    translation whose section keys were renamed."""
    if len(path_) > 2 and path_[:2] == ["cv", "sections"]:
        return path_[:2] + [keys.get(str(path_[2]), path_[2])] + path_[3:]
    return path_


def translation_drift(path: Path) -> dict | None:
    """What the source changed since this translation was last brought up to
    date, field by field, with what the translation says there now."""
    link = translation_link(path)
    if not link:
        return None
    out = {"of": link["of"], "lang": link.get("lang"), "from_lang": link.get("from_lang"),
           "synced_at": link.get("synced_at"), "missing": False, "changes": []}
    try:
        src = to_plain(yaml_rt.load(safe_path(link["of"]).read_text(encoding="utf-8"))) or {}
        mine = to_plain(yaml_rt.load(path.read_text(encoding="utf-8"))) or {}
    except (OSError, ValueError):
        out["missing"] = True
        return out
    except Exception:
        return out
    then = {"cv": link.get("synced")}
    keys = link.get("keys") or {}
    for f in _prov.changed_fields(then.get("cv"), src.get("cv"), ["cv"]):
        before, after = _prov.get_at(then, f), _prov.get_at(src, f)
        here = _map_to_translation(f, keys)
        out["changes"].append({
            "key": _prov.field_key(here), "source_key": _prov.field_key(f),
            "where": _prov._where_changed(f, src, then),
            "kind": "added" if before is None else "removed" if after is None
                    else "changed",
            "before": _prov._brief(before), "after": _prov._brief(after),
            "translation": _prov._brief(_prov.get_at(mine, here))})
    return out


def mark_translation_current(path: Path) -> dict:
    """Say this translation now covers everything its source says."""
    link = translation_link(path)
    if not link:
        raise ValueError(f"{rel(path)} is not a translation.")
    src = to_plain(yaml_rt.load(safe_path(link["of"]).read_text(encoding="utf-8"))) or {}
    record = _prov._edits_read()
    t = record["docs"].setdefault(rel(path), {}).setdefault("translation", link)
    t["synced"] = src.get("cv")
    t["synced_at"] = time.time()
    _prov._edits_write(record)
    return {"ok": True, "path": rel(path), "of": link["of"]}


def sync_design(path: Path) -> list[str]:
    """Give every translation of `path` the same design block, so the versions
    of one CV print alike. The locale is not design and is left alone."""
    touched = []
    trans = translations_of(path)
    if not trans:
        return touched
    import copy
    import io
    src = yaml_rt.load(path.read_text(encoding="utf-8"))
    design = src.get("design")
    for t in trans:
        tp = safe_path(t["path"])
        try:
            data = yaml_rt.load(tp.read_text(encoding="utf-8"))
            if to_plain(data.get("design")) == to_plain(design):
                continue
            data["design"] = copy.deepcopy(design)
            buf = io.StringIO()
            yaml_rt.dump(data, buf)
            tp.write_text(buf.getvalue(), encoding="utf-8")
            touched.append(t["path"])
        except Exception:
            continue
    return touched


