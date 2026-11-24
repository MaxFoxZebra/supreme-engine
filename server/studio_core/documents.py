"""CV YAML round-trip editing, rendering, block mapping, ATS checks, and design schema."""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path

from ruamel.yaml.comments import CommentedMap

from .context import (
    PAGE_SIZES,
    THEMES,
    ats,
    cjkfonts,
    ctx,
    cv_map,
    is_letter,
    jobstore,
    languages,
    letters,
    rel,
    render_file,
    review,
    safe_path,
    themes,
    to_plain,
    yaml_rt,
)
from . import history_scans as _hist
from . import letters_prep as _lp
from . import provenance as _prov
from . import translations as _trans
from . import workspace as _ws


_DEFAULTS: dict = {}


def design_defaults(theme: str) -> dict:
    """A theme's own value for every design setting, keyed "colors.name"."""
    if theme not in _DEFAULTS:
        out = {}
        for g in design_schema(theme).get("groups", []):
            for f in g["fields"]:
                out[".".join(map(str, f["path"]))] = f.get("default")
        _DEFAULTS[theme] = out
    return _DEFAULTS[theme]



# --------------------------------------------------------------------------
# documents
# --------------------------------------------------------------------------

def write_doc(path: Path, text: str, tool: str = "write") -> dict:
    """Replace a whole document, recording which fields that turned out to move.

    A whole-file write says nothing about what changed, so the difference has
    to be measured: parse what is there, write, parse what is now there. That
    keeps the marks the same whether a field was set through a patch or a file
    was replaced wholesale.
    """
    if is_letter(path):
        path.write_text(text, encoding="utf-8")
        return {"changed": []}
    before = None
    try:
        before = to_plain(yaml_rt.load(path.read_text(encoding="utf-8")))
    except Exception:
        pass
    path.write_text(text, encoding="utf-8")
    try:
        after = to_plain(yaml_rt.load(text))
    except Exception:
        return {"changed": []}
    if (before or {}).get("design") != (after or {}).get("design"):
        _trans.sync_design(path)
    return {"changed": _prov.record_edits(path, before, after, tool)}


def load_doc(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    try:
        data, err = yaml_rt.load(text), None
    except Exception as exc:
        data, err = None, str(exc)
    # The mtime is what lets the editor tell its own writes apart from someone
    # else's -- Claude's, usually -- and reload rather than overwrite.
    plain = to_plain(data) if data else None
    return {"yaml": text, "data": plain,
            "parse_error": err, "mtime": path.stat().st_mtime,
            # Where each block lives in the source, so the YAML tab can show
            # the same selection the page and the form do.
            "lines": line_map(data, text) if data else {},
            # Who last wrote each field, and how it differs from the CV it was
            # tailored from. Never written back into the file.
            "prov": _prov.provenance(path, plain),
            # Which language this is in, the other languages of the same CV,
            # and what its source changed since it was translated.
            "family": _trans.language_family(path),
            "drift": _trans.translation_drift(path),
            # How this document would point at the workspace photo, when
            # there is one to point at.
            "photo_ref": _ws.photo_ref(path) if _ws.photo_info() else None,
            # What an AI client changed that you have not yet kept or undone.
            "review": _prov.review_payload(path)}


def line_map(doc, text: str) -> dict:
    """Where each block of the document sits in the YAML source.

    The selection is the thread through every view, and the YAML tab was the one
    place it could not follow because nothing knew which lines an entry occupied.
    ruamel keeps the position of every node it parsed, so this is exact rather
    than a search for a matching string.

    Returns {"header": [start, end], "<section>": [...], "<section>/<i>": [...]}
    with 0-based, end-exclusive line numbers.
    """
    try:
        cv = doc["cv"]
    except Exception:
        return {}
    total = len(text.splitlines())
    out: dict[str, list[int]] = {}

    sections = cv.get("sections") if hasattr(cv, "get") else None
    # The header is everything in `cv` before the sections block begins.
    try:
        head_start = cv.lc.line
        head_end = sections.lc.line - 1 if sections is not None else total
        out["header"] = [head_start, max(head_start + 1, head_end)]
    except Exception:
        pass
    if sections is None:
        return out

    names = list(sections)
    for n, name in enumerate(names):
        try:
            key_line = sections.lc.key(name)[0]
        except Exception:
            continue
        nxt = sections.lc.key(names[n + 1])[0] if n + 1 < len(names) else total
        out[name] = [key_line, nxt]
        entries = sections[name] or []
        # An entry runs to the start of the next one, or to the end of the section.
        starts = []
        for i in range(len(entries)):
            try:
                starts.append(entries.lc.item(i)[0])
            except Exception:
                starts.append(None)
        for i, s in enumerate(starts):
            if s is None:
                continue
            follow = next((x for x in starts[i + 1:] if x is not None), None)
            out[f"{name}/{i}"] = [s, follow if follow is not None else nxt]
    return out


def apply_ops(path: Path, ops: list[dict], tool: str = "edit") -> dict:
    """Add and remove whole entries and sections.

    apply_patches deliberately refuses a path that is not already in the
    document -- that refusal is what tells a model it mis-indexed an entry
    instead of quietly writing a new one somewhere. Structure is the other
    half of that bargain: adding an entry IS creating a path that did not
    exist, so it has to be asked for in as many words rather than inferred
    from a patch that happened to land past the end of a list.

    RenderCV types a section by the entries in it -- every section is a
    list[OneLineEntry] or a list[ExperienceEntry], never a mixture -- so a
    section with nothing in it cannot be typed and fails validation. Removing
    the last entry therefore removes the section, which is what "remove" meant
    anyway; leaving an empty one behind would break the render of a document
    that was fine a moment ago.

    Returns the same {"applied", "missed", "changed"} shape as apply_patches.
    """
    data = yaml_rt.load(path.read_text(encoding="utf-8"))
    before = to_plain(data)
    applied: list[dict] = []
    missed: list[dict] = []
    cv = data.get("cv") if hasattr(data, "get") else None
    if cv is None:
        return {"applied": [], "missed": [{"op": o, "why": "no cv in this file"}
                                          for o in ops], "changed": []}
    for op in ops:
        kind = op.get("op")
        if cv.get("sections") is None:
            cv["sections"] = {}
        sections = cv["sections"]
        try:
            if kind == "add_section":
                name = op["name"]
                if name in sections:
                    missed.append({"op": op, "why": "that section already exists"})
                    continue
                sections[name] = op["value"]
            elif kind == "remove_section":
                del sections[op["name"]]
            elif kind == "add_entry":
                seq = sections[op["section"]]
                at = op.get("at")
                at = len(seq) if at is None else max(0, min(int(at), len(seq)))
                seq.insert(at, op["value"])
            elif kind == "remove_entry":
                name = op["section"]
                seq = sections[name]
                seq.pop(int(op["at"]))
                if not len(seq):
                    del sections[name]
            else:
                missed.append({"op": op, "why": "unknown operation"})
                continue
            applied.append(op)
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            missed.append({"op": op, "why": f"{type(exc).__name__}: {exc}"})
    import io
    buf = io.StringIO()
    yaml_rt.dump(data, buf)
    path.write_text(buf.getvalue(), encoding="utf-8")
    return {"applied": applied, "missed": missed,
            "changed": _prov.record_edits(path, before, to_plain(data), tool)}


def _patch_data(data, patches: list[dict]) -> tuple[list, list]:
    """Write `patches` into loaded YAML: (applied paths, missed)."""
    applied: list[list] = []
    missed: list[dict] = []
    for patch in patches:
        keys, value = patch.get("path") or [], patch.get("value")
        if not keys:
            missed.append({"path": keys, "why": "no path given"})
            continue
        node, ok = data, True
        for k in keys[:-1]:
            try:
                node = node[int(k)] if isinstance(node, list) else node[k]
            except (KeyError, IndexError, ValueError, TypeError):
                # A design setting belongs to a group the document may never
                # have written -- a starter CV has no `design.header` at all --
                # and the Design screen offers every one. Those groups are made
                # on the way down. Everywhere else a missing parent still means
                # a wrong path, and is reported rather than invented.
                if keys[0] == "design" and isinstance(node, dict) and isinstance(k, str):
                    node[k] = CommentedMap()
                    node = node[k]
                    continue
                ok = False
                break
        if not ok or node is None:
            missed.append({"path": keys, "why": "no such field"})
            continue
        last = keys[-1]
        try:
            if isinstance(node, list):
                node[int(last)] = value
            else:
                node[last] = value
        except (KeyError, IndexError, ValueError, TypeError):
            missed.append({"path": keys, "why": "no such field"})
            continue
        applied.append(keys)
    return applied, missed


def save_patches(path: Path, patches: list[dict]) -> dict:
    """The user's own edits, from the app's form: written to the file, and
    into the copy an AI client's changes are reviewed against, so they are
    neither shown as the client's change nor taken back by its Undo.

    Into that copy only where the entry, or the field outside any entry,
    still reads there as it did in the file before this save. Anywhere the
    client changed, the two no longer line up by position, and the edit is
    left in the review as it was before this (edit_on_page does the same).
    """
    pre = path.read_text(encoding="utf-8")
    wrote = apply_patches(path, patches, "save")
    done = [p for p in patches if (p.get("path") or []) in wrote["applied"]]
    rp = rel(path)
    kept = (review.entry(ctx.workspace, rp) or {}).get("before")
    if kept is None or not done:
        return wrote
    try:
        data = yaml_rt.load(kept)
        was = to_plain(yaml_rt.load(pre))
    except Exception:
        return wrote
    if not isinstance(data, dict) or not isinstance(was, dict):
        return wrote
    then = to_plain(data)
    missing = object()

    def at(d, keys):
        for k in keys:
            try:
                d = d[int(k)] if isinstance(d, list) else d[k]
            except (KeyError, IndexError, ValueError, TypeError):
                return missing
        return d

    def anchor(keys):
        return keys[:4] if len(keys) > 4 and list(keys[:2]) == ["cv", "sections"] else keys

    same = [p for p in done if at(was, anchor(p["path"])) == at(then, anchor(p["path"]))]
    if not same:
        return wrote
    _patch_data(data, same)
    buf = io.StringIO()
    yaml_rt.dump(data, buf)
    if buf.getvalue() != kept:
        review.set_before(ctx.workspace, rp, buf.getvalue())
    return wrote


def apply_patches(path: Path, patches: list[dict], tool: str = "edit", record: bool = True) -> dict:
    """Set individual fields, keeping the rest of the file and its comments.

    Returns {"applied": [...], "missed": [...], "changed": [...]}. `missed`
    matters: a patch whose path does not exist in the document is skipped, and
    reporting that as a success is how a model mis-indexes an entry, is told it
    worked, and moves on. The paths that actually changed value are recorded as
    provenance on the way past; not for a preview's scratch copy (`record`
    False), which is no one's document.
    """
    data = yaml_rt.load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        # Empty, comments only, or caught mid-write by another program:
        # writing it back would replace the file with `null`.
        return {"applied": [], "missed": [{"path": p.get("path") or [], "why": "no cv in this file"}
                                          for p in patches], "changed": []}
    before = to_plain(data)
    applied, missed = _patch_data(data, patches)
    import io
    buf = io.StringIO()
    yaml_rt.dump(data, buf)
    path.write_text(buf.getvalue(), encoding="utf-8")
    if not record:
        return {"applied": applied, "missed": missed, "changed": []}
    after = to_plain(data)
    if (before or {}).get("design") != (after or {}).get("design"):
        _trans.sync_design(path)
    return {"applied": applied, "missed": missed,
            "changed": _prov.record_edits(path, before, after, tool)}


# Error text from RenderCV is precise but not friendly. These are the failures
# real users actually hit, with an explanation of what to do about it.
HINTS = (
    # Typst fetches a theme's icon and font packages the first time it lays
    # one out, and caches them. Offline, or behind a proxy, that first time
    # fails with a trace that says nothing a person can act on.
    ("failed to download package",
     "The page needs a font or icon package it downloads once, the first time a "
     "theme is used, and it could not be downloaded. Check your internet "
     "connection, then try again. Your CV is saved; you can keep editing it in Form "
     "or YAML meanwhile."),
    ("mapping values are not allowed",
     "A line of text contains a colon followed by a space, which YAML reads as a "
     "new field. Wrap that text in a >- block, or put it in quotes."),
    ("not a valid phone number",
     "Phone numbers are checked against real numbering plans, not just their shape. "
     "Use the full international form (e.g. +33-6-12-34-56-78) and make sure it is "
     "a number that could actually be dialled in that country."),
    # The same colon mistake inside a list of strings produces a *valid* YAML
    # dict rather than a syntax error, so it surfaces as a type complaint
    # instead. This is the form users hit most, because bullets get written
    # naturally as "Did the thing: with this result".
    ("input should be a valid string",
     "A bullet probably contains a colon followed by a space, so YAML turned it into "
     "a field instead of text. Put that bullet in quotes, or reword it to avoid the "
     "colon (an en dash reads well)."),
    ("entry type of this section",
     "The entries in this section are not all the same shape. Every entry in one "
     "section must be the same type. Look for a bullet that accidentally became "
     "a field."),
    ("not a valid email", "That email address is malformed. Check the @ and the domain."),
    ("could not find", "A referenced file is missing. Check the paths in your YAML."),
)


def friendly(error: str) -> str | None:
    """Match a hint against RenderCV's error output.

    The output is a box-drawn table, so a message is wrapped across lines and
    padded with spaces and border glyphs. Matching the raw text fails for any
    phrase long enough to wrap -- which is all the useful ones -- so flatten it
    to a single spaced line first.
    """
    flat = re.sub(r"[─-╿|]", " ", error)
    flat = re.sub(r"\s+", " ", flat).strip().lower()
    for needle, hint in HINTS:
        if needle in flat:
            return hint
    return None


def outline_of(data: dict | None) -> list[tuple[str, int]]:
    """A document's sections as (key, entry count), in the order they render."""
    cv = (data or {}).get("cv") or {}
    sections = cv.get("sections") or {}
    return [(k, len(v or [])) for k, v in sections.items()]


def block_map(result: dict, source: Path) -> dict | None:
    """Where each section and entry of a render landed on the page.

    Best effort by design: this only makes the preview clickable, so anything
    that goes wrong -- no Typst, a source shaped unexpectedly by some theme --
    costs the click targets and nothing else. The render itself has already
    succeeded by the time this runs.
    """
    if cv_map is None:
        return None, "Typst mapping is unavailable in this build"
    if not result.get("typ"):
        return None, "the render produced no Typst source to read"
    reasons: list = []
    try:
        # Parsed here rather than through load_doc: this wants the section
        # names and nothing else, and load_doc also computes provenance and a
        # line map, which is work nobody asked for on every render -- and one
        # more thing that can throw where the only consequence is the click
        # targets silently vanishing.
        outline = outline_of(to_plain(yaml_rt.load(
            source.read_text(encoding="utf-8"))))
        if not outline:
            return None, "the document has no sections to map"
        built = cv_map.build_map(Path(result["typ"]), outline, source.parent,
                                 reasons)
        if built:
            return built, None
        return None, reasons[0] if reasons else "the render could not be mapped"
    except Exception as exc:
        # Still best effort -- this costs the click targets and nothing else --
        # but it says so now rather than leaving a dead page and no trace.
        return None, f"{type(exc).__name__}: {exc}"


def _shape(result: dict, source: Path) -> dict:
    if not result.get("ok"):
        log = (result.get("log") or "render failed")[-3000:]
        return {"ok": False, "error": log, "hint": friendly(log)}
    stamp = int(time.time() * 1000)
    shaped = {
        "ok": True,
        "pages": result.get("pages"),
        "ats_words": result.get("ats_word_count"),
        "pdf": rel(Path(result["pdf"])) if result.get("pdf") else None,
        "pngs": [f"/api/asset?path={rel(Path(p))}&v={stamp}"
                 for p in result.get("png_pages", [])],
    }
    blocks, why = block_map(result, source)
    if why:
        # The page is still shown, it just cannot be clicked. Saying so beats
        # a preview that quietly stops responding.
        shaped["map_why"] = why
    if blocks and blocks.get("bands"):
        shaped["map"] = blocks["bands"]
        # The column the text sits in, so a click target can hug the writing
        # rather than stretch across the sheet.
        if blocks.get("box"):
            shaped["map_box"] = blocks["box"]
    return shaped


def output_dir(path: Path) -> Path:
    """Where a document's render lands.

    Shared with the MCP server so that a CV rendered from Claude Desktop and
    one rendered by clicking Render end up as the same file, rather than two
    PDFs in different folders that quietly drift apart.
    """
    if path.parent.name in ("profile", "letters"):
        return ctx.workspace / "assets" / path.stem
    return path.parent / "output"


def render(path: Path) -> dict:
    return _shape(render_file(path, output_dir(path)), path)


def last_pdf(path: Path) -> Path | None:
    """The newest PDF in this document's output folder, whatever its age.

    A CV and a letter of the same name share the folder (profile/x.yaml and
    letters/x.md both render to assets/x), so each takes only its own kind.
    """
    out = output_dir(path)
    letter = is_letter(path)
    pdfs = sorted((f for f in out.glob("*.pdf") if letters.is_output(f) == letter),
                  key=lambda f: f.stat().st_mtime, reverse=True) if out.is_dir() else []
    return pdfs[0] if pdfs else None


def current_pdf(path: Path) -> tuple[Path | None, dict | None]:
    """The PDF for this document as it now stands, rendering it if the one on
    disk is older than the YAML. Returns (pdf, None) or (None, failed render)."""
    pdf = last_pdf(path)
    if pdf and pdf.stat().st_mtime >= path.stat().st_mtime:
        return pdf, None
    r = render(path)
    if not r.get("ok"):
        return None, r
    return ctx.workspace / r["pdf"], None


def job_for(path: Path, job_id: str | None = None) -> dict | None:
    """The application to check a CV against: the one named, else the one this
    CV is attached to."""
    if jobstore is None:
        return None
    jobs = jobstore.list_jobs(ctx.workspace)
    if job_id:
        return next((j for j in jobs if j["id"] == job_id), None)
    return next((j for j in jobs if j.get("cv_path") == rel(path)), None)


def ats_report(path: Path, job_id: str | None = None,
               posting: str | None = None) -> dict:
    """What an ATS reads from this CV's PDF, and how it meets a posting.

    The posting is the one pasted, else the named application's, else the
    application this CV is attached to. No posting is not an error: the
    parsing checks stand on their own.
    """
    pdf, failed = current_pdf(path)
    if pdf is None:
        return {"ok": False, "error": (failed or {}).get("hint") or
                "The CV did not render, so there is no PDF to read."}
    try:
        pages = ats.pdf_text(pdf)
    except Exception as exc:
        return {"ok": False, "error": f"Could not read the PDF: {exc}"}
    data = to_plain(yaml_rt.load(path.read_text(encoding="utf-8"))) or {}
    text = "\n".join(pages)
    out = {"ok": True, "pdf": rel(pdf), "pages": len(pages),
           "words": len(text.split()), "text": text[:8000],
           "checks": ats.parse_checks(pages, data), "against": None,
           "keywords": None}
    # An empty posting is a choice ("check it against nothing"), not an
    # absence, so only None falls back to the attached application.
    job = None if posting is not None else job_for(path, job_id)
    source = posting.strip() if posting is not None else (job or {}).get("description")
    if job:
        out["against"] = {"job_id": job["id"], "company": job.get("company"),
                          "title": job.get("title"),
                          "has_posting": bool(job.get("description"))}
    elif source:
        out["against"] = {"pasted": True, "has_posting": True}
    if source:
        terms = ats.keywords(source, (job or {}).get("company"))
        out["keywords"] = ats.match(terms, text)
    # Where the job is does not depend on which posting it is checked against.
    where = job or job_for(path, job_id)
    if where and (data.get("cv") or {}).get("photo") and photo_unusual(where):
        out["checks"].append({
            "id": "photo", "level": "warn",
            "title": "A photo, for a posting where CVs usually have none",
            "detail": "This application is in the UK, the US or Ireland, where "
                      "recruiters usually ask for CVs without a photo, and some "
                      "systems set aside CVs that have one. Turn it off for this "
                      "CV with the Photo chip in the editor."})
    return out


# Where recruiters usually ask for CVs without a photo, read from where the
# job is. Only the application's own location fields: a posting that merely
# mentions London is not evidence.
PHOTO_UNUSUAL = re.compile(
    r"(?i)\b(united kingdom|uk|u\.k\.|england|scotland|wales|northern ireland|"
    r"london|manchester|edinburgh|glasgow|bristol|cambridge, uk|united states|usa|"
    r"u\.s\.a?\.?|us|new york|san francisco|seattle|boston|austin|chicago|"
    r"los angeles|ireland|dublin|cork)\b")


def photo_unusual(job: dict) -> bool:
    where = " ".join(str(job.get(k) or "") for k in ("country", "location"))
    return bool(PHOTO_UNUSUAL.search(where))


# The parsing problems RenderCV's own design options solve, keyed by the
# id parse_checks gives them.
ATS_FIXES = {
    "icons": (["design", "header", "connections", "show_icons"], False),
    "urls": (["design", "header", "connections",
              "display_urls_instead_of_usernames"], True),
    "hyphens": (["design", "typography", "alignment"], "justified-with-no-hyphenation"),
}


def ats_fix(path: Path, fix: str) -> dict:
    if fix not in ATS_FIXES:
        raise ValueError(f"Unknown fix: {fix}")
    keys, value = ATS_FIXES[fix]
    from ruamel.yaml.comments import CommentedMap
    doc = yaml_rt.load(path.read_text(encoding="utf-8"))
    node = doc
    for k in keys[:-1]:
        # apply_patches will not create a missing branch, and the starter CV
        # has no design.header at all, so this builds it.
        if not isinstance(node.get(k), dict):
            node[k] = CommentedMap()
        node = node[k]
    node[keys[-1]] = value
    import io
    buf = io.StringIO()
    yaml_rt.dump(doc, buf)
    return write_doc(path, buf.getvalue(), "save")


def thumb(path: Path) -> dict:
    """The first page as it was last rendered, and whether that is still true.

    Read off disk rather than rendered, because the base card asks on every
    boot and a render is seconds of work. A page older than the YAML is still
    returned -- it is the right shape while the new one is made -- but marked,
    so the caller knows to render rather than show last week's CV as today's.
    """
    pdf = last_pdf(path)
    first = pdf.with_name(f"{pdf.stem}_1.png") if pdf else None
    if not first or not first.is_file():
        return {"png": None, "fresh": False}
    made = first.stat().st_mtime
    return {"png": f"/api/asset?path={rel(first)}&v={int(made * 1000)}",
            "fresh": made >= path.stat().st_mtime}


def preview(path: Path, text: str | None = None,
            patches: list[dict] | None = None) -> dict:
    """Render unsaved editor content without writing to the user's file.

    The scratch file goes beside the original rather than into a temp directory
    because RenderCV resolves a `fonts/` folder relative to the input file;
    rendering elsewhere would silently drop any custom font.
    """
    with _PREVIEW_LOCK:
        return _preview(path, text, patches)


# One preview at a time, and one theme tile: each run writes the same scratch
# file and clears the same folder, so one landing while another waited for
# the renderer took the other's file, or its pages, from under it.
_PREVIEW_LOCK = threading.Lock()
_THEME_LOCK = threading.Lock()


def _preview(path: Path, text: str | None, patches: list[dict] | None) -> dict:
    tmp = path.parent / (".cvstudio-preview" + path.suffix)
    try:
        # Form edits arrive as patches, so start from the saved file and apply
        # them to the scratch copy; the YAML tab sends its text directly.
        tmp.write_text(text if text is not None
                       else path.read_text(encoding="utf-8"), encoding="utf-8")
        if patches:
            apply_patches(tmp, patches, record=False)
        # Shaped against the scratch copy, not the saved file: the block map has
        # to describe the document as it was just rendered, unsaved edits and
        # all. `tmp` is still on disk here -- the unlink below runs after.
        # Every document previews into this one folder, so last time's output
        # is still sitting in it. Clear it: otherwise the folder grows without
        # bound and stale pages linger for anything that looks at it.
        scratch = ctx.workspace / "assets" / ".preview"
        if scratch.is_dir():
            for stale in scratch.iterdir():
                if stale.is_file():
                    try:
                        stale.unlink()
                    except OSError:
                        pass
        return _shape(render_file(tmp, scratch), tmp)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def theme_preview(path: Path, theme: str, patches: list[dict] | None = None) -> dict:
    """Page one of this document in another theme, for the Design screen.

    The theme picker shows the document you are designing rather than a sample,
    so each tile is a real render. It gets its own folder per theme: the live
    preview clears its scratch folder on every run, and a tile that shared it
    would take the page you are looking at down with it.
    """
    if theme not in available_themes():
        raise ValueError(f"Unknown theme: {theme}")
    with _THEME_LOCK:
        return _theme_preview(path, theme, patches)


def _theme_preview(path: Path, theme: str, patches: list[dict] | None) -> dict:
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", path.stem) or "doc"
    out = ctx.workspace / "assets" / ".themes" / stem / theme
    if out.is_dir():
        for stale in out.iterdir():
            if stale.is_file():
                try:
                    stale.unlink()
                except OSError:
                    pass
    tmp = path.parent / (".cvstudio-theme" + path.suffix)
    try:
        tmp.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        apply_patches(tmp, list(patches or []) +
                      [{"path": ["design", "theme"], "value": theme}], record=False)
        result = render_file(tmp, out)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    pngs = result.get("png_pages") or []
    if not result.get("ok") or not pngs:
        return {"ok": False, "theme": theme}
    stamp = int(time.time() * 1000)
    return {"ok": True, "theme": theme, "pages": result.get("pages"),
            "png": f"/api/asset?path={rel(Path(pngs[0]))}&v={stamp}"}


def available_themes() -> list[str]:
    """Themes RenderCV actually ships, asked at runtime rather than hardcoded.

    The docs list five; v2.8 ships nine. Reading the real list means the app
    does not go stale when RenderCV adds one.
    """
    try:
        from rendercv.schema.models.design.built_in_design import available_themes as at
        builtin = list(at)
    except Exception:
        builtin = list(THEMES)
    # CV Studio's own themes first: they are the ones made to look finished.
    return [t for t in themes.DEFAULTS if t not in builtin and t not in themes.HIDDEN] + builtin


def _unwrap(spec: dict) -> dict:
    """Collapse Optional[...] / anyOf into the one meaningful variant."""
    for v in spec.get("anyOf", [spec]):
        if v.get("type") != "null":
            return v
    return spec


def _describe(spec: dict, defs: dict, path: list, group: str, depth: int = 0) -> list[dict]:
    """Turn one schema property into UI field descriptors.

    Nested models (font_family, font_size) are flattened one level so every
    setting reachable in the YAML is reachable in the interface.
    """
    v = _unwrap(spec)
    default = spec.get("default", v.get("default"))
    ref = v.get("$ref")
    target = defs.get(ref.split("/")[-1], {}) if ref else {}
    name = ref.split("/")[-1] if ref else ""

    if target.get("enum") or v.get("enum"):
        return [{"path": path, "kind": "enum",
                 "options": target.get("enum") or v.get("enum"), "default": default}]
    if "TypstDimension" in name:
        return [{"path": path, "kind": "dimension", "default": default}]
    if target.get("properties") and depth < 1:
        out = []
        for k, sub in target["properties"].items():
            out += _describe(sub, defs, path + [k], group, depth + 1)
        return out
    t = v.get("type") or target.get("type")
    if t == "boolean":
        return [{"path": path, "kind": "bool", "default": bool(default)}]
    if t == "array":
        return [{"path": path, "kind": "list", "default": default or []}]
    if group == "colors" or (isinstance(default, str) and default.startswith("rgb(")):
        return [{"path": path, "kind": "color", "default": default}]
    if t in ("integer", "number"):
        return [{"path": path, "kind": "number", "default": default}]
    return [{"path": path, "kind": "text", "default": default}]


def design_schema(theme: str) -> dict:
    """Every design option for a theme, described well enough to build a UI from."""
    try:
        if theme in themes.DEFAULTS:
            # One of CV Studio's: classic's options, with the theme's own
            # defaults, so they do not read as changes you made.
            sch = themes.theme_classes()[theme].model_json_schema()
        else:
            from rendercv.schema.models.design.built_in_design import built_in_design_adapter
            sch = built_in_design_adapter.json_schema()
    except Exception as exc:
        return {"groups": [], "themes": available_themes(), "error": str(exc)}

    defs = sch.get("$defs", {})
    branch = sch if theme in themes.DEFAULTS else None
    for b in sch.get("oneOf", []) if branch is None else []:
        model = defs.get(b.get("$ref", "").split("/")[-1], {})
        if (model.get("properties", {}).get("theme", {}) or {}).get("const") == theme:
            branch = model
            break
    if branch is None:
        return {"groups": [], "themes": available_themes()}

    groups = []
    for gname, gspec in branch.get("properties", {}).items():
        if gname == "theme":
            continue
        v = _unwrap(gspec)
        ref = v.get("$ref")
        model = defs.get(ref.split("/")[-1], {}) if ref else {}
        fields = []
        for fname, fspec in (model.get("properties") or {}).items():
            fields += _describe(fspec, defs, [gname, fname], gname)
        # Forced off at render time (cv_render.FORCED), so a switch for it
        # would be a control that does nothing.
        fields = [f for f in fields if f["path"] != ["page", "show_top_note"]]
        own = themes.DEFAULTS.get(theme)
        for f in fields if own else []:
            node = own
            for k in f["path"]:
                node = node.get(k) if isinstance(node, dict) else None
            if node is not None and not isinstance(node, dict):
                f["default"] = node
        if fields:
            groups.append({"name": gname, "fields": fields})
    return {"groups": groups, "themes": available_themes()}


