"""Field-level edit provenance, base CV lineage, diffing, and AI change review."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .context import (
    EDITS_FILE,
    EDITS_KEEP,
    EDITS_MAX_AGE,
    EDITS_VALUE_CHARS,
    ctx,
    is_cv_yaml,
    is_letter,
    letters,
    rel,
    review,
    safe_path,
    to_plain,
    yaml_rt,
)
from . import documents as _docs
from . import history_scans as _hist
from . import letters_prep as _lp
from . import workspace as _ws


# --------------------------------------------------------------------------
# Provenance
#
# Two different questions, which look the same on screen and are not:
#
#   "is this line different from the base CV?"  -- computed live from the two
#       documents, correct no matter who changed it or when, and needs nothing
#       stored but a pointer to the base.
#   "who last wrote this line?"                 -- history, and nothing but a
#       record of the writes can answer it.
#
# The second is the one that needs care. A field address like
# ["cv","sections","experience",2,"highlights",0] is positional, so it is wrong
# the moment an entry is inserted above it. Every edit therefore also stores an
# anchor (section, entry title, key) and a hash of the value it wrote. On the
# way back out, an edit is kept only if it can be re-found *and* the value is
# still the one it wrote. Anything else is dropped, because a mark on the wrong
# line is worse than no mark at all.
# --------------------------------------------------------------------------


def _edits_read() -> dict:
    try:
        data = json.loads((ctx.workspace / EDITS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "docs": {}}
    if not isinstance(data, dict) or not isinstance(data.get("docs"), dict):
        return {"version": 1, "docs": {}}
    return data


def _edits_write(data: dict) -> None:
    """Bookkeeping must never be what breaks a save, so failures are swallowed.

    Both halves write this file, so two saves landing together can lose one
    side's entry to the read-modify-write. The cost of that is a missing mark,
    not missing work, which is why it is not worth a lock file: the documents
    themselves are the record, and this only annotates them.
    """
    try:
        review.write_whole(ctx.workspace / EDITS_FILE, json.dumps(data, separators=(",", ":")))
    except OSError:
        pass


def _clip(value) -> object:
    """A value small enough to keep a few hundred of without thinking about it."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        text = value if isinstance(value, str) else json.dumps(value)
    else:
        text = json.dumps(value, separators=(",", ":"), default=str)
    return text if len(text) <= EDITS_VALUE_CHARS else text[:EDITS_VALUE_CHARS] + "\u2026"


def _hash(value) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def field_key(path: list) -> str:
    return ".".join(str(k) for k in path)


def get_at(data, path: list):
    node = data
    for k in path:
        try:
            node = node[int(k)] if isinstance(node, list) else node[k]
        except (KeyError, IndexError, ValueError, TypeError):
            return None
    return node


def entry_title(entry, i: int) -> str:
    """What the outline calls an entry. Kept in step with entryTitle() in the UI.

    Every RenderCV entry type keeps its headline under a different key, so this
    is a preference order rather than one lookup.
    """
    if not isinstance(entry, dict):
        return " ".join(str(entry or "").split()[:4]) or f"entry {i + 1}"
    for k in ("company", "institution", "name", "title", "label", "position",
              "bullet"):
        if entry.get(k):
            return str(entry[k])
    return f"entry {i + 1}"


def _anchor(path: list, data: dict) -> dict | None:
    """Enough to re-find a field after the entries around it have moved."""
    if len(path) < 4 or path[0] != "cv" or path[1] != "sections":
        return None
    name = path[2]
    try:
        i = int(path[3])
    except (TypeError, ValueError):
        return None
    entry = get_at(data, ["cv", "sections", name, i])
    out = {"section": name, "entry": entry_title(entry, i), "i": i}
    if len(path) > 4:
        out["key"] = path[4]
        if len(path) > 5:
            out["at"] = path[5]
    return out


def _refind(edit: dict, data: dict) -> list | None:
    """Where this edit's field lives now, or None if it cannot be trusted.

    The recorded path first, since nothing has usually moved. Failing that the
    anchor, which survives a reorder. Either way the value has to still hash to
    what was written, otherwise somebody has edited it since and the edit is
    no longer the last word on that field.
    """
    want = edit.get("to_hash")
    path = edit.get("field") or []
    if want and _hash(get_at(data, path)) == want:
        return path
    anchor = edit.get("anchor")
    if not anchor or not want:
        return None
    section = get_at(data, ["cv", "sections", anchor.get("section")])
    if not isinstance(section, list):
        return None
    for i, entry in enumerate(section):
        if entry_title(entry, i) != anchor.get("entry"):
            continue
        found = ["cv", "sections", anchor["section"], i]
        if anchor.get("key") is not None:
            found.append(anchor["key"])
        if anchor.get("at") is not None:
            found.append(anchor["at"])
        if _hash(get_at(data, found)) == want:
            return found
    return None


def changed_fields(before, after, prefix: list | None = None) -> list[list]:
    """The leaf paths at which two parsed documents differ.

    A list is compared whole rather than walked: a bullet list that gained an
    item has shifted every index after it, and reporting that as five changed
    bullets would be true and useless.
    """
    out: list[list] = []
    prefix = prefix or []
    keys = list(dict.fromkeys(list((before or {}).keys()) + list((after or {}).keys())))
    for k in keys:
        a = (before or {}).get(k)
        b = (after or {}).get(k)
        here = prefix + [k]
        if isinstance(a, dict) and isinstance(b, dict):
            out += changed_fields(a, b, here)
        elif isinstance(a, list) and isinstance(b, list):
            for i in range(max(len(a), len(b))):
                av = a[i] if i < len(a) else None
                bv = b[i] if i < len(b) else None
                if isinstance(av, dict) and isinstance(bv, dict):
                    out += changed_fields(av, bv, here + [i])
                elif av != bv:
                    out.append(here + [i])
        elif a != b:
            out.append(here)
    return out


def record_edits(path: Path, before: dict | None, after: dict | None,
                 tool: str, by: str | None = None,
                 agent: str | None = None) -> list[list]:
    """Note who changed which fields. Returns the paths, for the caller to report.

    `by` defaults to whoever this process is: an AI client when the MCP server
    is running, and the user otherwise. That is the whole distinction the marks
    are drawing.
    """
    fields = changed_fields((before or {}).get("cv"), (after or {}).get("cv"),
                            ["cv"])
    if not fields:
        return []
    who = by or ctx.client_id or ("ai" if ctx.is_mcp else "you")
    now = time.time()
    data = _edits_read()
    doc = data["docs"].setdefault(rel(path), {})
    log = doc.get("edits")
    if not isinstance(log, list):
        log = []
    for field in fields:
        value = get_at(after, field)
        log.append({
            "at": now, "by": who, "tool": tool,
            **({"agent": agent or ctx.client_agent} if (agent or ctx.client_agent) else {}),
            "field": field,
            **({"anchor": _anchor(field, after)} if _anchor(field, after) else {}),
            "from": _clip(get_at(before, field)),
            "to": _clip(value),
            "to_hash": _hash(value),
        })
    # Only the newest edit per field is ever shown, so only the newest is kept.
    # Without this a file saved fifty times carries fifty records of the same
    # headline, and the sidecar grows with how often you work rather than with
    # how much there is to say. What it costs is the value a field held two
    # edits ago, which nothing asks for.
    cutoff = now - EDITS_MAX_AGE
    newest: dict[str, dict] = {}
    for edit in log:
        if (edit.get("at") or 0) < cutoff:
            continue
        key = field_key(edit.get("field") or [])
        held = newest.get(key)
        if not held or (edit.get("at") or 0) >= (held.get("at") or 0):
            # Keep the value this field held before anyone started editing it,
            # rather than before the most recent keystroke: "was: Your Role" is
            # the useful answer, "was: Solutions Enginee" is not.
            if held:
                edit = {**edit, "from": held.get("from")}
            newest[key] = edit
    doc["edits"] = sorted(newest.values(),
                          key=lambda e: e.get("at") or 0)[-EDITS_KEEP:]
    _edits_write(data)
    return fields


def _forget_deleted(data: dict) -> bool:
    """Drop documents that are no longer in the workspace. Returns True if any went.

    Otherwise a workspace churned through fifty tailored copies keeps the marks
    for all fifty, and the file grows with everything you have ever deleted.
    """
    gone = [p for p in data["docs"] if not (ctx.workspace / p).is_file()]
    for p in gone:
        del data["docs"][p]
    return bool(gone)




def note_lineage(path: Path, base: str | None) -> None:
    """Remember which document this one was copied from.

    Duplicating is how a CV gets tailored, and until this was stored the copy
    had no idea what it was a copy *of* -- so "what did this change from the
    base" was unanswerable for both halves of the app.
    """
    if not base:
        return
    data = _edits_read()
    _forget_deleted(data)
    doc = data["docs"].setdefault(rel(path), {})
    doc["base"] = {"path": base, "at": time.time()}
    _edits_write(data)


def base_cv() -> dict | None:
    """The CV every tailored copy starts from, as {path, at, missing}.

    Kept in the workspace beside the edits rather than in the browser's
    preferences, because it is a fact about this set of documents and not about
    this machine -- and because a model working in here has to be able to read
    it. Which document you tailor from is the first thing it needs to know.

    Distinct from the per-document `base` that note_lineage records. That one
    says what a particular copy came from and stays true even when this moves;
    this one only says where the next copy should come from.

    Pure: a missing file is reported, never quietly repointed or cleared.
    """
    chosen = _edits_read().get("base") or {}
    if chosen.get("path"):
        return {"path": chosen["path"], "at": chosen.get("at"),
                "missing": not safe_path(chosen["path"]).exists()}
    # Nothing chosen. One CV is not a choice, it is the answer -- but two are,
    # and picking for you would put a document you never nominated at the top
    # of the screen and copy every tailored CV from it.
    mine = [d for d in _ws.list_documents() if d["group"] == "My CVs"]
    if len(mine) == 1:
        return {"path": mine[0]["path"], "at": None, "missing": False}
    return None


def set_base_cv(path: str | None) -> None:
    """Nominate a document as the base, or clear the nomination with None."""
    data = _edits_read()
    if path is None:
        data.pop("base", None)
    else:
        target = safe_path(path)
        if not target.exists():
            raise FileNotFoundError(path)
        if not is_cv_yaml(target):
            raise ValueError(f"{path} is not a CV.")
        if rel(target).startswith("letters/"):
            raise ValueError("A cover letter cannot be the base CV.")
        data["base"] = {"path": rel(target), "at": time.time()}
    _edits_write(data)
    # _edits_write swallows write failures on purpose -- a lost mark is not
    # worth failing a save over. A choice the user just made is different: it
    # would come back as the old base with nothing said, which is the kind of
    # bug nobody ever diagnoses. So read it back and complain.
    if (_edits_read().get("base") or {}).get("path") != (data.get("base") or {}).get("path"):
        raise OSError(f"Could not record the base CV in {EDITS_FILE}.")


def provenance(path: Path, data: dict | None) -> dict:
    """Who last wrote each field of this document, and how it differs from its base.

    `fields` is keyed by a dotted path so the UI can look one up without
    walking. Only the newest surviving edit per field is returned: the rest are
    history nobody is asking to see.
    """
    # `last` is the newest edit by anyone; `last_ai` the newest by anyone who
    # is not the user. They differ the moment you touch a document a model
    # worked on, and it is the second that the summary wants: "Claude, an hour
    # ago" stays true and stays interesting after you have typed since.
    out = {"fields": {}, "base": None, "from_base": [],
           "last": None, "last_ai": None}
    if not data:
        return out
    doc = (_edits_read()["docs"].get(rel(path)) or {})

    for edit in reversed(doc.get("edits") or []):
        found = _refind(edit, data)
        if found is None:
            continue
        key = field_key(found)
        if key in out["fields"]:
            continue
        out["fields"][key] = {"by": edit.get("by"), "at": edit.get("at"),
                              "agent": edit.get("agent"), "tool": edit.get("tool"),
                              "from": edit.get("from")}
        who = {"by": edit.get("by"), "at": edit.get("at"),
               "agent": edit.get("agent")}
        for slot in ("last", "last_ai"):
            if slot == "last_ai" and edit.get("by") == "you":
                continue
            held = out[slot]
            if not held or (edit.get("at") or 0) > (held.get("at") or 0):
                out[slot] = who

    base = doc.get("base") or {}
    base_path = base.get("path")
    if base_path:
        out["base"] = {"path": base_path, "at": base.get("at"), "missing": True}
        try:
            other = yaml_rt.load(safe_path(base_path).read_text(encoding="utf-8"))
            out["base"]["missing"] = False
            out["from_base"] = [field_key(f) for f in changed_fields(
                to_plain(other).get("cv"), data.get("cv"), ["cv"])]
        except Exception:
            # A base that has been renamed or deleted is a missing comparison,
            # not a broken document. Say so and show the rest.
            pass
    return out


def _where_changed(path: list, mine: dict, theirs: dict) -> str:
    """Where a changed field is, in the words the outline uses."""
    if path[:2] != ["cv", "sections"]:
        return "Header \u00b7 " + " \u203a ".join(str(k) for k in path[1:])
    bits = [str(path[2]).replace("_", " ").capitalize()] if len(path) > 2 else []
    if len(path) > 3:
        i = int(path[3])
        entry = get_at(mine, path[:4])
        if entry is None:
            entry = get_at(theirs, path[:4])
        if isinstance(entry, dict):
            bits.append(entry_title(entry, i))
    rest = path[4:]
    if rest:
        key = str(rest[0])
        if len(rest) > 1 and key == "highlights":
            bits.append(f"bullet {int(rest[1]) + 1}")
        else:
            bits.append(key.replace("_", " "))
    return " \u203a ".join(bits)


def _brief(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, dict):
        return entry_title(v, 0)
    if isinstance(v, list):
        return f"{len(v)} item{'' if len(v) == 1 else 's'}"
    text = " ".join(str(v).split())
    return text if len(text) <= 160 else text[:157] + "\u2026"


def base_diff(path: Path) -> dict:
    """What a tailored CV changed from the one it was copied from, line by line.

    Against the copy's own recorded base rather than today's base CV: a CV
    written for one job should keep saying what it changed from what it was
    made from, even after another document becomes the base.
    """
    base_path = doc_base(path)
    out = {"base": base_path, "missing": False, "changes": [], "design": [],
           "units": [], "sig": None}
    if not base_path:
        return out
    try:
        now = path.read_text(encoding="utf-8")
        then = safe_path(base_path).read_text(encoding="utf-8")
        mine = to_plain(yaml_rt.load(now)) or {}
        theirs = to_plain(yaml_rt.load(then)) or {}
    except (OSError, ValueError):
        out["missing"] = True
        return out
    except Exception:
        # Unparseable mid-edit: say nothing rather than something wrong.
        return out
    # The same units a review shows, so the Changes tab can draw each one
    # whole -- an entry field by field, its bullets reworded, added, removed --
    # and put the base's version of any one of them back.
    out["units"] = review.cv_units(theirs, mine, entry_title)
    out["sig"] = review.signature(then, now)
    for f in changed_fields(theirs.get("cv"), mine.get("cv"), ["cv"]):
        before, after = get_at(theirs, f), get_at(mine, f)
        out["changes"].append({
            "key": field_key(f), "where": _where_changed(f, mine, theirs),
            "kind": "added" if before is None else "removed" if after is None
                    else "changed",
            "before": _brief(before), "after": _brief(after)})
    out["design"] = sorted({str(f[1]) for f in changed_fields(
        theirs.get("design"), mine.get("design"), ["design"]) if len(f) > 1})
    return out


def base_revert(path: Path, ids: list, sig: str | None) -> dict:
    """Put back what the base says for some of the ways a CV differs from it.

    `ids` are units of base_diff. Everything else in the CV stays as it is,
    comments included. Refused when the CV or its base changed since the
    units were read, so an id never lands on a different entry.
    """
    base_path = doc_base(path)
    if not base_path:
        raise ValueError("This CV was not copied from another one.")
    now = path.read_text(encoding="utf-8")
    then = safe_path(base_path).read_text(encoding="utf-8")
    if sig and sig != review.signature(then, now):
        raise ValueError("The CV or the one it was copied from changed. "
                         "The changes are shown again as they are now.")
    units = review.cv_units(to_plain(yaml_rt.load(then)), to_plain(yaml_rt.load(now)),
                            entry_title)
    chosen = set(ids) & {u["id"] for u in units}
    if chosen:
        text, _ = review.cv_resolve(yaml_rt, to_plain, entry_title, then, now,
                                    chosen, "undo")
        _docs.write_doc(path, text, "revert")
    return base_diff(path)




# --------------------------------------------------------------------------
# Reviewing what an AI client changed (see review.py)
# --------------------------------------------------------------------------

def review_units(path: Path, before: str | None, now: str) -> list[dict]:
    if is_letter(path):
        return review.letter_units(letters, before, now)
    if before is None:
        return [{"id": "created", "kind": "created", "label": "New CV"}]
    return review.cv_units(to_plain(yaml_rt.load(before)), to_plain(yaml_rt.load(now)),
                           entry_title)


def doc_base(path: Path) -> str | None:
    """The document this one was copied from, as note_lineage recorded it."""
    return ((_edits_read()["docs"].get(rel(path)) or {}).get("base") or {}).get("path")


def review_before(path: Path, e: dict) -> tuple[str | None, str | None]:
    """What a review compares the document with, and the base when it is one.

    A CV an AI client made by copying the base had no text of its own before,
    so compared with nothing, all a review could say was "Claude wrote this
    CV": not one word of what it changed. It is a copy of the base, so the
    base is what it is compared with -- every reworded bullet, every entry
    added or taken out, each kept or undone on its own, undo putting back what
    the base says.

    The base as it was when the copy was made, which the MCP wrapper keeps:
    an edit you make to the base afterwards is a difference the Changes tab
    shows, but not a change of the model's. A copy recorded before that was
    kept is compared with the base as it is now.
    """
    before = e.get("before")
    if not e.get("created") or is_letter(path):
        return before, None
    if before is not None:
        return before, e.get("base")
    base = doc_base(path)
    if not base:
        return None, None
    try:
        return safe_path(base).read_text(encoding="utf-8"), base
    except (OSError, ValueError):
        return None, None


def _review_state(path: Path, e: dict, now: str) -> tuple[str | None, str | None, list]:
    """(before, base, units) for a document with changes waiting."""
    before, base = review_before(path, e)
    try:
        units = review_units(path, before, now)
    except Exception:
        # A file the model left unparseable is still reviewable, whole.
        units = [{"id": "all", "kind": "text", "label": "The whole file",
                  "before": before, "after": now}]
    if base and not units:
        # Copied and not changed yet: nothing differs from the base, but it is
        # still a new document somebody else made.
        before, base = None, None
        units = review_units(path, None, now)
    return before, base, units


def review_payload(path: Path) -> dict | None:
    """The changes waiting on this document, as units to keep or undo."""
    e = review.entry(ctx.workspace, rel(path))
    if not e:
        return None
    now = path.read_text(encoding="utf-8")
    before, base, units = _review_state(path, e, now)
    if not units:
        # Everything it changed has been changed back, by hand or otherwise.
        review.clear(ctx.workspace, rel(path))
        return None
    return {"by": e.get("by"), "agent": e.get("agent"), "since": e.get("since"),
            "at": e.get("at"), "writes": e.get("writes"), "tools": e.get("tools") or [],
            "created": bool(e.get("created")), "base": base,
            "sig": review.signature(before, now), "units": units}


def review_resolve(path: Path, ids: list, action: str, sig: str | None) -> dict:
    """Keep or undo some of an AI client's changes, or all of them ("*")."""
    if action not in ("keep", "undo"):
        raise ValueError("Keep or undo, nothing else.")
    rp = rel(path)
    e = review.entry(ctx.workspace, rp)
    if not e:
        return {"review": None}
    now = path.read_text(encoding="utf-8")
    before, base, units = _review_state(path, e, now)
    if sig and sig != review.signature(before, now):
        raise ValueError("The document changed while you were reviewing it. "
                         "The changes are shown again as they are now.")
    if action == "keep" and "*" in ids:
        review.clear(ctx.workspace, rp)
        return {"review": None}
    if before is None:
        # A document it made from nothing: taking it back is deleting it. One
        # it copied from the base is compared with the base instead, so undoing
        # all of it puts the base's text back and leaves the copy to keep or
        # delete; "Delete it" in the app deletes the document itself.
        if action == "keep":
            review.clear(ctx.workspace, rp)
            return {"review": None}
        return {"review": None, "deleted": _hist.delete_document(rp)}
    known = {u["id"] for u in units}
    chosen = known if "*" in ids or "all" in known else set(ids) & known
    if not chosen:
        return {"review": review_payload(path)}
    if action == "keep" and chosen == known:
        # Keeping all of it is having nothing left to look at, and needs no
        # parse: "The whole file" is the one card when the YAML has none.
        review.clear(ctx.workspace, rp)
        return {"review": None}
    if action == "undo" and chosen == known:
        # All of it: the text it had, exactly, comments and all.
        text, kept = before, before
    elif is_letter(path):
        text, kept = review.letter_resolve(letters, before, now, chosen, action)
    else:
        text, kept = review.cv_resolve(yaml_rt, to_plain, entry_title, before, now,
                                       chosen, action)
    if action == "undo":
        if is_letter(path):
            path.write_text(text, encoding="utf-8")
        else:
            _docs.write_doc(path, text, "review")
    else:
        review.set_before(ctx.workspace, rp, kept)
    return {"review": review_payload(path)}




def edits_stamp() -> float | None:
    """When the provenance file last changed, so the poll can spot a new mark."""
    try:
        return (ctx.workspace / EDITS_FILE).stat().st_mtime
    except OSError:
        return None

