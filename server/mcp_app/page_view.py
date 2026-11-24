"""Visual page rendering, change marks, and interactive page-view MCP tools."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from pathlib import Path

from mcp.types import BlobResourceContents, CallToolResult, EmbeddedResource, ImageContent, TextContent

import cjkfonts
import greypng
import marks
import review
import studio
from cv_render import render_file
from . import applications as _apps
from .runtime import PAGE_VIEW, _VIEWS, _doc_path, _one_line, _shows_views, _views_note, _ws, tool


# The last render of each document in this connector's lifetime, which is
# the client's session: what the next render of it is compared with, so the
# view can mark what a change actually changed and show the page before it.
_LAST_RENDER: dict[str, dict] = {}


def _changes(old: dict, new: dict) -> dict | None:
    """Which blocks differ between two versions of a CV, as the view names
    them ({k, name, i}), how many entries went, and whether the design did.
    For a letter, which paragraphs (by where they are now) and whether the
    header did."""
    if "letter" in old or "letter" in new:
        ol, nl = old.get("letter") or {}, new.get("letter") or {}
        bk, ak = ol.get("chunks") or [], nl.get("chunks") or []
        blocks = [{"k": "header", "name": None, "i": None}] if ol.get("meta") != nl.get("meta") else []
        removed = 0
        for tag, bi, ai in review.align(bk, ak, lambda i, j: bk[i] == ak[j]):
            if tag in ("chg", "ins"):
                blocks.append({"k": "para", "name": None, "i": ai})
            elif tag == "del":
                removed += 1
        return {"blocks": blocks, "removed": removed, "design": False} if blocks or removed else None
    oc, nc = old.get("cv") or {}, new.get("cv") or {}
    blocks: list[dict] = []
    if {k: v for k, v in oc.items() if k != "sections"} != \
            {k: v for k, v in nc.items() if k != "sections"}:
        blocks.append({"k": "header", "name": None, "i": None})
    olds, news = oc.get("sections") or {}, nc.get("sections") or {}
    removed = 0
    for name, entries in news.items():
        entries = entries or []
        before = olds.get(name)
        if before is None:
            blocks.append({"k": "section", "name": name, "i": None})
            blocks += [{"k": "entry", "name": name, "i": i} for i in range(len(entries))]
            continue
        blocks += [{"k": "entry", "name": name, "i": i} for i, e in enumerate(entries)
                   if i >= len(before) or before[i] != e]
        removed += max(0, len(before) - len(entries))
    removed += sum(len(v or []) for k, v in olds.items() if k not in news)
    design = old.get("design") != new.get("design") or old.get("locale") != new.get("locale")
    if not (blocks or removed or design):
        return None
    return {"blocks": blocks, "removed": removed, "design": design}


def _flat(value, n: int = 700) -> str:
    """A CV value as the text it prints, for the page view to show what a
    block said before a change: the title line, then the bullets."""
    if value is None:
        return ""
    if isinstance(value, dict):
        head = [str(value[k]) for k in ("company", "institution", "name", "position", "title",
                                         "degree", "area", "label", "location", "date",
                                         "start_date", "end_date") if value.get(k)]
        lines = [" · ".join(head)] if head else []
        for k in ("summary", "details", "bullet"):
            if value.get(k):
                lines.append(str(value[k]))
        lines += ["• " + str(h) for h in value.get("highlights") or []]
        if not lines:
            lines = [f"{k}: {v}" for k, v in value.items() if isinstance(v, (str, int, float))]
        text = "\n".join(lines)
    elif isinstance(value, list):
        text = "\n".join(_flat(v, n) for v in value)
    else:
        text = str(value)
    return text if len(text) <= n else text[:n - 1] + "…"


def _review_units(p: Path) -> dict | None:
    """The changes an AI client made to this document and nobody has kept or
    undone yet, the way the app's review holds them, with the block of the
    page each one is (or None: a removed entry, the design, a letter's
    paragraph) and what it said before."""
    try:
        payload = studio.review_payload(p)
    except Exception:
        return None
    if not payload or not payload.get("units"):
        return None
    out = []
    for u in payload["units"]:
        uid, kind = u.get("id", ""), u.get("kind")
        block = None
        if uid.startswith("cv:") or uid.startswith("meta:"):
            block = {"k": "header", "name": None, "i": None}
        elif kind == "para" and u.get("tag") != "del" and u.get("at") is not None:
            block = {"k": "para", "name": None, "i": u.get("at")}
        elif kind == "section" and u.get("after") is not None:
            block = {"k": "section", "name": u.get("section"), "i": None}
        elif kind == "entry" and u.get("i") is not None:
            block = {"k": "entry", "name": u.get("section"), "i": u.get("i")}
        where = u.get("where")
        value = u.get("after") if u.get("after") is not None else u.get("before")
        if kind == "para":
            words = str(value or "").split()
            where = f"Paragraph {(u.get('at') or 0) + 1} · " + " ".join(words[:6]) + ("…" if len(words) > 6 else "")
        elif kind == "field" and uid.startswith("meta:"):
            where = "Header · " + str(u.get("label") or "")
        if kind == "entry" and isinstance(value, str):
            # A line of text has no title of its own: its first words are its name.
            where = str(u.get("section") or "").replace("_", " ").capitalize() + " · " + \
                " ".join(value.split()[:6]) + ("…" if len(value.split()) > 6 else "")
        out.append({"id": uid, "kind": kind, "tag": u.get("tag"), "label": _one_line(u.get("label"), 40),
                    "where": _one_line(where, 90), "block": block,
                    "before": _flat(u.get("before")), "after": _flat(u.get("after"))})
    return {"units": out, "sig": payload.get("sig"), "agent": _one_line(payload.get("agent"), 40)}


# --- What the page view fetches ----------------------------------------------
#
# A render's pages are kept by content, outside the workspace, so a view can
# fetch the one it is about to show (page_view_data) instead of every page
# arriving inline, and so a conversation opened again next week still has
# them. Beside them, one small record per render: which file and pages it
# was, and what the CV said, so an earlier view of a CV can tell it has been
# rendered again since and fold itself away.

MAX_VIEW_PAGES = 12
_PAGE_ID = re.compile(r"[0-9a-f]{32}")
_RID = re.compile(r"[0-9a-f]{8,24}")
_KEEP_DAYS = 60
_pruned = False


def _views_dir() -> Path:
    return cjkfonts.cache_dir().parent / "views"


def _write(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.part")
    tmp.write_bytes(data)
    os.replace(tmp, target)


def _keep_page(f: Path) -> str:
    """Store a page image by what it is; its id is what the view asks for."""
    return _keep_bytes(f.read_bytes())


def _keep_bytes(data: bytes) -> str:
    pid = hashlib.sha256(data).hexdigest()[:32]
    target = _views_dir() / "pages" / f"{pid}.png"
    try:
        if target.is_file():
            os.utime(target)   # in use again: not old enough to clear away
        else:
            _write(target, data)
    except OSError:
        pass
    return pid


def _prune_views() -> None:
    """Pages and records nothing has shown for two months, once per run."""
    global _pruned
    if _pruned:
        return
    _pruned = True
    cutoff = time.time() - _KEEP_DAYS * 86400
    for sub in ("pages", "renders"):
        try:
            for f in (_views_dir() / sub).iterdir():
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
        except OSError:
            pass


def _latest_file() -> Path:
    return _views_dir() / "latest.json"


def _latest() -> dict:
    try:
        return json.loads(_latest_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _record_render(p: Path, data: dict, shots: list[str]) -> str | None:
    """Note this render as the latest of its file; returns its id."""
    _prune_views()
    rid = f"{time.time_ns():x}"
    try:
        _write(_views_dir() / "renders" / f"{rid}.json",
               json.dumps({"path": str(p), "data": data, "shots": shots}).encode("utf-8"))
        latest = _latest()
        latest[str(p)] = rid
        # A file per CV ever rendered stays small; renamed and deleted ones
        # fall out after their records do.
        latest = {k: v for k, v in latest.items()
                  if (_views_dir() / "renders" / f"{v}.json").is_file()}
        _write(_latest_file(), json.dumps(latest).encode("utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    return rid


def _store_view(rid: str | None, view: dict) -> None:
    """Keep what the view showed with its render: a pinned or full-screen
    view follows the latest render of its file by asking for it."""
    rec = _render_record(rid or "")
    if rec is None:
        return
    try:
        _write(_views_dir() / "renders" / f"{rid}.json", json.dumps({**rec, "view": view}).encode("utf-8"))
    except (OSError, TypeError, ValueError):
        pass


def _render_record(rid: str) -> dict | None:
    if not _RID.fullmatch(rid or ""):
        return None
    try:
        return json.loads((_views_dir() / "renders" / f"{rid}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _view_status(path: str, rid: str) -> dict:
    """Whether a view's render is still the latest of its file, and if not,
    how much of the CV has changed since."""
    p = studio.safe_path(path)
    latest = _latest().get(str(p))
    if not latest or latest == rid or not _RID.fullmatch(rid or "") or int(latest, 16) < int(rid, 16):
        return {"current": True}
    old, new = _render_record(rid), _render_record(latest)
    changed = None
    if old and new and old.get("data") and new.get("data"):
        c = _changes(old["data"], new["data"])
        changed = (len({(b["k"], b["name"], b["i"]) for b in c["blocks"]}) + c["removed"]
                   + (1 if c["design"] else 0)) if c else 0
    return {"current": False, "changed": changed}


def _pdf_resource(path: str) -> EmbeddedResource:
    p = studio.safe_path(path)
    if p.suffix.lower() != ".pdf" or not p.is_file():
        raise ValueError("No PDF there. Render the CV again.")
    if p.stat().st_size > 25 * 1024 * 1024:
        raise ValueError("That PDF is too large to hand over here.")
    return EmbeddedResource(type="resource", resource=BlobResourceContents(
        uri="file:///" + p.name, mime_type="application/pdf",
        blob=base64.b64encode(p.read_bytes()).decode("ascii")))


# --- What changed, marked on the page (marks.py) ------------------------------

def _mark_rules(p: Path, letter: bool, data: dict, last: dict | None, changes: dict) -> str:
    """Show rules highlighting what is new: against what the review keeps
    when there is a review, else against the last render."""
    pairs: list[tuple[list[str], list[str]]] = []
    try:
        payload = studio.review_payload(p)
    except Exception:
        payload = None
    for u in (payload or {}).get("units") or []:
        if u.get("after") not in (None, "", []):
            pairs.append((marks.texts(u.get("before")), marks.texts(u.get("after"))))
    # A document made in this conversation has no text before it to review
    # against: what is new is what changed since its last render.
    if not pairs and last and last.get("data"):
        old = last["data"]
        if letter:
            ol, nl = old.get("letter") or {}, data.get("letter") or {}
            for b in changes.get("blocks") or []:
                if b["k"] == "para" and b["i"] is not None and b["i"] < len(nl.get("chunks") or []):
                    pairs.append((ol.get("chunks") or [], [nl["chunks"][b["i"]]]))
                elif b["k"] == "header":
                    pairs.append((marks.texts(ol.get("meta")), marks.texts(nl.get("meta"))))
        else:
            oc, nc = old.get("cv") or {}, data.get("cv") or {}
            os_, ns = oc.get("sections") or {}, nc.get("sections") or {}
            for b in changes.get("blocks") or []:
                if b["k"] == "header":
                    pairs.append((marks.texts({k: v for k, v in oc.items() if k != "sections"}),
                                  marks.texts({k: v for k, v in nc.items() if k != "sections"})))
                elif b["k"] == "entry" and b["i"] < len(ns.get(b["name"]) or []):
                    pairs.append((marks.texts(os_.get(b["name"])), marks.texts(ns[b["name"]][b["i"]])))
    if not pairs:
        return ""
    if letter:
        nl = data.get("letter") or {}
        document = list(nl.get("chunks") or []) + marks.texts(nl.get("meta"))
    else:
        document = marks.texts(data.get("cv"))
    return marks.rules(pairs, document)


def _typst_setup(p: Path, letter: bool) -> tuple[list, dict]:
    """The fonts and packages a document's Typst source compiles with."""
    if letter:
        meta, _ = studio.letters.parse(p.read_text(encoding="utf-8"))
        cvp = studio.letter_cv(studio.dated(meta, p))
        return studio.letters._font_paths(([cvp.parent / "fonts"] if cvp else []) + [p.parent / "fonts"]), {}
    import cv_map
    from rendercv.renderer.pdf_png import get_package_path
    return cv_map._fonts(p.parent), {"package_path": get_package_path()}


# The page image the model gets with a render. Enough to see the layout (a
# heading left at the foot of a page, a near-empty last page, a line that
# wraps badly) and to read the text, at about half the tokens of the view's
# 144 ppi page: an image is paid for by its pixels, every turn it stays in
# the conversation. In sixteen greys, a quarter of the colour page's size: a
# result over about 150,000 characters reaches Claude as a file when code
# execution is on, and its view is then never shown (see greypng). A page
# still over MODEL_IMAGE_MAX in base64 is made again at MODEL_PPI_SMALL.
MODEL_PPI = 96
MODEL_PPI_SMALL = 72
MODEL_IMAGE_MAX = 100_000


def _model_image(p: Path, letter: bool, typ: Path | None, idx: int, full: Path) -> ImageContent:
    """Page `idx` for the model, at MODEL_PPI and in grey; the view's own
    page if the smaller one cannot be made."""
    data = None
    if typ is not None and typ.is_file():
        try:
            import typst
            fonts, extra = _typst_setup(p, letter)
            for ppi in (MODEL_PPI, MODEL_PPI_SMALL):
                out = typst.compile(str(typ), format="png", ppi=ppi, root=str(typ.parent),
                                    font_paths=fonts, **extra)
                out = out if isinstance(out, list) else [out]
                if idx >= len(out):
                    break
                data = greypng.grey16(out[idx]) or out[idx]
                if len(data) * 4 // 3 < MODEL_IMAGE_MAX:
                    break
        except Exception:
            pass
    if data is None:
        data = full.read_bytes()
        data = greypng.grey16(data) or data
    return ImageContent(type="image", data=base64.b64encode(data).decode("ascii"), mime_type="image/png")


# The pages the view shows: sharper than the renderer's own PNGs, so the text
# stays crisp on a high-density screen with the page as wide as the chat.
VIEW_PPI = 200


def _view_pages(p: Path, letter: bool, typ: Path | None, count: int) -> list[bytes] | None:
    """Every page at VIEW_PPI, or None to use the renderer's."""
    if typ is None or not typ.is_file():
        return None
    try:
        import typst
        fonts, extra = _typst_setup(p, letter)
        out = typst.compile(str(typ), format="png", ppi=VIEW_PPI, root=str(typ.parent), font_paths=fonts, **extra)
    except Exception:
        return None
    out = out if isinstance(out, list) else [out]
    return out if len(out) == count else None


def _marked_pages(p: Path, letter: bool, typ: Path, count: int, rules: str) -> list[str] | None:
    """The pages again with `rules` applied, kept by id like the pages, or
    None unless every block is exactly where it is on the real page."""
    if not rules or not typ.is_file():
        return None
    import typst
    import cv_map
    src = typ.read_text(encoding="utf-8")
    if letter:
        plain, lit = studio.letter_map(p), studio.letter_map(p, prefix=rules)
        if not plain or not lit:
            return None
        where = lambda m: [(b["k"], b["i"], b["page"], round(b["y0"], 1)) for b in m["bands"]]
        if where(plain) != where(lit):
            return None
    else:
        probed, probes = cv_map._inject(src)
        if not probes:
            return None
        lit_src = probed.replace(cv_map.HELPER, cv_map.HELPER + rules, 1)
        a = cv_map._positions(typ, probed, p.parent)
        b = cv_map._positions(typ, lit_src, p.parent)
        if [(x["n"], x["p"], round(x["y"], 1)) for x in a] != [(x["n"], x["p"], round(x["y"], 1)) for x in b]:
            return None
    fonts, extra = _typst_setup(p, letter)
    scratch = typ.parent / ".cvstudio-marks.typ"
    try:
        scratch.write_text(rules + src, encoding="utf-8")
        out = typst.compile(str(scratch), format="png", ppi=VIEW_PPI,
                            root=str(typ.parent), font_paths=fonts, **extra)
    finally:
        try:
            scratch.unlink()
        except OSError:
            pass
    out = out if isinstance(out, list) else [out]
    if len(out) != count or len(out) > MAX_VIEW_PAGES:
        return None
    return [_keep_bytes(b) for b in out]


def _for_job(p: Path) -> dict | None:
    try:
        job = studio.job_for(p)
    except Exception:
        return None
    if not job:
        return None
    return {"id": job["id"], "company": _one_line(job.get("company"), 60) or None,
            "title": _one_line(job.get("title"), 80) or None, "logo": _apps._logo_uri(job)}


def _page_view(path: str, p: Path, pngs: list[Path], page: int, pages: int,
               words, pdf, bands=None, box=None, typ: Path | None = None) -> dict:
    """What the page view shows: every page (up to twelve) by id, where
    each block of the CV landed on them, named the way the outline names it
    so a click can say "Experience · Acme" rather than a position, and what
    changed since the last render of it."""
    labels = {}
    data: dict = {}
    letter = studio.is_letter(p)
    if letter:
        meta, body = studio.letters.parse(p.read_text(encoding="utf-8"))
        data = {"letter": {"meta": {k: meta.get(k) for k in ("to", "place", "date", "subject")},
                           "chunks": [review.chunk_key(c) for c in studio.letters.chunks(body)]}}
    else:
        try:
            data = studio.to_plain(studio.yaml_rt.load(p.read_text(encoding="utf-8"))) or {}
        except Exception:
            data = {}
    cv = data.get("cv") or {}
    sections = cv.get("sections") or {}
    chunks = (data.get("letter") or {}).get("chunks") or []
    for b in bands or []:
        if b["k"] == "para":
            first = (chunks[b["i"]] if b["i"] < len(chunks) else "").split()
            label = f"Paragraph {b['i'] + 1} · " + " ".join(first[:6]) + ("…" if len(first) > 6 else "")
        elif b["k"] == "header" and letter:
            label = "Header · to, date and subject"
        elif b["k"] == "header":
            label = "Header"
        else:
            label = str(b.get("name") or "").replace("_", " ").strip().capitalize()
            if b["k"] == "entry":
                entries = sections.get(b.get("name")) or []
                i = b.get("i") or 0
                label += " · " + (studio.entry_title(entries[i], i) if i < len(entries)
                                  else f"entry {i + 1}")
        labels[f"{b['k']}|{b.get('name')}|{b.get('i')}"] = _one_line(label)
    sharp = _view_pages(p, letter, typ, len(pngs))
    shots = [_keep_bytes(b) for b in sharp[:MAX_VIEW_PAGES]] if sharp else [_keep_page(f) for f in pngs[:MAX_VIEW_PAGES]]
    images = shots
    last = _LAST_RENDER.get(str(p))
    changes = _changes(last["data"], data) if last and data and last.get("data") else None
    # What is waiting for the user to keep or undo outranks what differs
    # from the last render: it is what they can act on, and it survives a
    # restart of the client.
    pending = _review_units(p)
    if pending and all(u["kind"] == "created" for u in pending["units"]):
        # Made in this conversation: all of it is new against the file, so the
        # page marks what changed since its last render, still to keep or undo.
        changes = {**(changes or {"blocks": [], "removed": 0, "design": False}), "review": pending}
    elif pending:
        units = pending["units"]
        changes = {"blocks": [u["block"] for u in units if u["block"]],
                   "removed": sum(1 for u in units if u.get("tag") == "del" or
                                  (u["kind"] == "section" and not u["after"])),
                   "design": any(u["id"].startswith("top:") for u in units),
                   "review": pending}
    if not changes and last and letter and last.get("data") and last.get("images") != images:
        changes = _changes(last["data"], data) or {"blocks": [], "removed": 0, "design": False, "page": True}
    marked = None
    if changes and typ:
        try:
            marked = _marked_pages(p, letter, typ, len(shots), _mark_rules(p, letter, data, last, changes))
        except Exception:
            marked = None
    _LAST_RENDER[str(p)] = {"data": data, "images": images}
    rid = _record_render(p, data, shots)
    first = min(max(page, 1), len(shots)) - 1 if shots else 0
    view = {
        "view": "cv-page", "rid": rid, "path": path, "page": page, "pages": pages,
        "words": words, "pdf": pdf, "letter": letter,
        # Whose CV and for what, which says more than a file name.
        "title": _one_line(cv.get("name"), 60) or None,
        "subtitle": _one_line(cv.get("headline") or cv.get("label"), 90) or None,
        # Each page by its id, fetched by the view as an image of its own.
        # None inline: clients cap the structured part of a result well below
        # what one dense page weighs, and drop all of it when it is over.
        "shots": shots,
        "first": {"id": shots[first]} if shots else None,
        # The same pages with what changed highlighted, laid out identically.
        "marked": marked,
        # The application this document is for, when it is attached to one.
        "job": _for_job(p),
        "map": [{**b, "label": labels.get(f"{b['k']}|{b.get('name')}|{b.get('i')}")}
                for b in bands or []],
        "box": box,
        "changes": changes,
        "before": last["images"] if changes and last and last.get("images") != images else None,
    }
    _store_view(rid, view)
    return view


@tool(view=PAGE_VIEW)
def render_cv(path: str, page: int = 1) -> CallToolResult:
    """Render a CV to PDF and return the page as an image to look at.

    Returns the page count, the word count an ATS would extract, the PDF's
    location, and an image of the requested page. Check the image before
    reporting success: page-break damage does not show up in the YAML. The
    image is in grey to keep it small; the PDF and the view keep the colours.

    Make every edit a request needs first, then render once: in a client
    that shows views each render adds the page to the conversation.

    In a client that shows views, the user also sees every page in the
    conversation and can click a block to tell you what to change there:
    their message names the block and its place in the YAML. They can also
    keep or undo each change you made, from the page; you are told when
    they undo one, and should not make that change again unless asked.

    To show the user a CV or a letter, whatever they call it (the page, the
    render, the full view, the PDF), call this: the page it shows is the one
    they can click to edit, and its own button opens it full screen. Never
    show it through another tool (a PDF viewer, the file system, a shell):
    that is a picture they cannot edit.
    """
    r = _render(path, page, _shows_views())
    sent = r.structured_content is not None
    _views_note("render", {"path": path, "data": sent},
                f"render_cv of {path} " + ("failed" if r.is_error else
                                           "sent the page view its data" if sent else
                                           "sent no data for a view, as the client did not say it shows views"))
    return r


def _render(path: str, page: int, views: bool) -> CallToolResult:
    p = studio.safe_path(path)

    def failed(text: str) -> CallToolResult:
        return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)

    if studio.is_letter(p):
        r = studio.render_letter(p)
        if not r.get("ok"):
            return failed(f"RENDER FAILED\n\n{r.get('error')}")
        pngs = [studio.WORKSPACE / u.split("path=")[1].split("&")[0] for u in r["pngs"]]
        idx = max(1, min(page, r["pages"])) - 1
        summary = f"Rendered {path}\nPages: {r['pages']}\nWords: {r['words']}\nPDF: {r['pdf']}"
        view = None
        typs = [f for f in studio.output_dir(p).glob("*.typ")
                if not f.name.startswith(".") and studio.letters.is_output(f)]
        typ = max(typs, key=lambda f: f.stat().st_mtime) if typs else None
        if views:
            try:
                mapped = studio.letter_map(p)
            except Exception:
                mapped = None
            view = _page_view(path, p, pngs, idx + 1, r["pages"], r["words"], r["pdf"],
                              (mapped or {}).get("bands"), (mapped or {}).get("box"), typ)
        return CallToolResult(
            content=[TextContent(type="text", text=summary), _model_image(p, True, typ, idx, pngs[idx])],
            structured_content=view)

    result = render_file(p, studio.output_dir(p))
    if not result.get("ok"):
        log = (result.get("log") or "render failed")[-2500:]
        hint = studio.friendly(log)
        return failed(f"RENDER FAILED\n\n{('Likely cause: ' + hint) if hint else ''}\n\n{log}")

    pages = result["pages"]
    summary = (
        f"Rendered {path}\n"
        f"Pages: {pages}\n"
        f"Words an ATS reads: {result['ats_word_count']}\n"
        f"PDF: {result['pdf']}"
    )
    content: list = [TextContent(type="text", text=summary)]
    pngs = [Path(f) for f in result.get("png_pages") or []]
    idx = max(1, min(page, pages)) - 1
    if pngs:
        content.append(_model_image(p, False, Path(result["typ"]) if result.get("typ") else None, idx, pngs[idx]))
    view = None
    if views and pngs:
        mapped, _ = studio.block_map(result, p)
        view = _page_view(path, p, pngs, idx + 1, pages, result["ats_word_count"],
                          studio.rel(Path(result["pdf"])) if result.get("pdf") else None,
                          (mapped or {}).get("bands"), (mapped or {}).get("box"),
                          Path(result["typ"]) if result.get("typ") else None)
    return CallToolResult(content=content, structured_content=view)




@tool(view=PAGE_VIEW, app_only=True)
def review_change(path: str, ids: list[str], action: str, sig: str | None = None,
                  page: int = 1) -> CallToolResult:
    """Keep or undo changes an AI client made to a document, from the page
    view: `ids` are the review's units ("*" for all of them), `action` is
    "keep" or "undo", `sig` the review's signature when the page was drawn,
    so a document that changed since is refused rather than half-undone.
    Returns the page as it is afterwards. For the user's hand only: hidden
    from the model."""
    p = _doc_path(path)
    studio.review_resolve(p, [str(i) for i in ids][:200], action, sig)
    # Compared with the render before, an undo would show as a change of its
    # own; what is left to review is the only comparison that means anything.
    _LAST_RENDER.pop(str(p), None)
    return _render(path, page, True)


@tool(view=PAGE_VIEW, app_only=True)
def page_view_data(what: str, path: str = "", page: str = "", rid: str = "",
                   block: dict | None = None) -> CallToolResult:
    """For the page view only, hidden from the model. what="page": a page's
    image by its id. what="pdf": the PDF at `path`, to download. what="status":
    whether render `rid` of `path` is still its latest. what="latest": the
    view of the latest render of `path`. what="fields": the text of `block`
    of `path`, to edit on the page."""
    if _VIEWS.get("asked", {}).get("at", 0) <= _VIEWS.get("render", {}).get("at", 0):
        _views_note("asked", {"what": what}, f"the page view is running in the chat: it asked for {what}")
    if what == "fields":
        f = _block_fields(_doc_path(path), block or {})
        return CallToolResult(content=[TextContent(type="text", text="fields")], structured_content=f)
    if what == "page":
        if not _PAGE_ID.fullmatch(page):
            raise ValueError("not a page id")
        f = _views_dir() / "pages" / f"{page}.png"
        if not f.is_file():
            # Said, not raised: the view tells "gone" from "not answering",
            # and a raised error reaches it only as "Error executing tool".
            return CallToolResult(content=[TextContent(type="text", text="That page is gone. Render the CV again.")],
                                  is_error=True)
        return CallToolResult(content=[ImageContent(
            type="image", data=base64.b64encode(f.read_bytes()).decode("ascii"), mime_type="image/png")])
    if what == "pdf":
        return CallToolResult(content=[_pdf_resource(path)])
    if what == "latest":
        rec = _render_record(_latest().get(str(studio.safe_path(path))) or "")
        if not rec or not rec.get("view"):
            return CallToolResult(content=[TextContent(type="text", text="No later render to show.")], is_error=True)
        return CallToolResult(content=[TextContent(type="text", text="latest")], structured_content=rec["view"])
    if what == "status":
        st = _view_status(path, rid)
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(st))], structured_content=st)
    raise ValueError("what is page, pdf, status, latest or fields")



# --- Small edits on the page -------------------------------------------------
#
# A typo, a date, a word: the user fixes it in the page view, in a form over
# the block, and it is saved as their edit, not an AI client's. Only text
# fields; anything structural is still asked of the model.

HEADER_KEYS = ("name", "headline", "location", "email", "phone", "website")
LETTER_HEAD = ("to", "place", "date", "subject")
_LONG = {"summary", "details", "description", "text"}


def _src_sig(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _label(key: str) -> str:
    return {"highlights": "Bullets, one per line", "to": "Addressed to"}.get(
        key, key.replace("_", " ").capitalize())


def _block_fields(p: Path, block: dict) -> dict:
    text = p.read_text(encoding="utf-8")
    k, i = block.get("k"), block.get("i")
    fields: list[dict] = []
    if studio.is_letter(p):
        meta, body = studio.letters.parse(text)
        if k == "para":
            ch = studio.letters.chunks(body)
            if not isinstance(i, int) or not 0 <= i < len(ch):
                raise ValueError("That paragraph is no longer there. Render the letter again.")
            fields = [{"key": "text", "label": "Paragraph", "value": ch[i], "multi": True}]
        elif k == "header":
            for key in LETTER_HEAD:
                v = meta.get(key)
                v = "\n".join(map(str, v)) if isinstance(v, list) else ("" if v is None else str(v))
                fields.append({"key": key, "label": _label(key), "value": v, "multi": key == "to"})
    else:
        cv = (studio.to_plain(studio.yaml_rt.load(text)) or {}).get("cv") or {}
        if k == "header":
            for key in HEADER_KEYS:
                if key in cv or key in ("name", "headline"):
                    if not isinstance(cv.get(key), (str, int, float, type(None))):
                        continue
                    fields.append({"key": key, "label": _label(key),
                                   "value": "" if cv.get(key) is None else str(cv[key])})
        elif k == "entry":
            entries = (cv.get("sections") or {}).get(block.get("name")) or []
            if not isinstance(i, int) or not 0 <= i < len(entries):
                raise ValueError("That entry is no longer there. Render the CV again.")
            e = entries[i]
            if isinstance(e, str):
                fields = [{"key": "text", "label": "Text", "value": e, "multi": True}]
            elif isinstance(e, dict):
                for key, v in e.items():
                    if isinstance(v, bool):
                        continue
                    if isinstance(v, (str, int, float)):
                        fields.append({"key": key, "label": _label(key), "value": str(v),
                                       "multi": key in _LONG or len(str(v)) > 70})
                    elif key == "highlights" and isinstance(v, list) and all(isinstance(x, str) for x in v):
                        fields.append({"key": key, "label": _label(key), "value": "\n".join(v),
                                       "multi": True, "list": True})
    if not fields:
        raise ValueError("That part of the page has nothing to edit here: ask Claude instead.")
    return {"fields": fields, "src": _src_sig(text)}


def _typed(old, new: str):
    """A date written as a number stays a number."""
    if isinstance(old, int) and not isinstance(old, bool) and new.strip().lstrip("-").isdigit():
        return int(new)
    return new


def _apply_block(text: str, letter: bool, block: dict, values: dict, like: str | None = None) -> str | None:
    """`text` with `values` written into `block`. With `like` (the document
    the block was read from), the block is found in `text` by what it said in
    `like` rather than by position, and only fields that read the same in
    both are written: this is how an edit reaches the review's kept copy
    without touching what an AI client changed. None if it is not there."""
    k, i = block.get("k"), block.get("i")
    if letter:
        meta, body = studio.letters.parse(text)
        meta = dict(meta)
        if k == "para":
            ch = studio.letters.chunks(body)
            j = i
            if like is not None:
                _, lb = studio.letters.parse(like)
                lch = studio.letters.chunks(lb)
                key = review.chunk_key(lch[i]) if 0 <= i < len(lch) else None
                j = next((n for n, c in enumerate(ch) if review.chunk_key(c) == key), None)
            if j is None or not 0 <= j < len(ch):
                return None
            new = str(values.get("text") or "").strip("\n")
            ch = ch[:j] + ([new] if new.strip() else []) + ch[j + 1:]
            return studio.letters.dump(meta, "\n\n".join(ch))
        lmeta = studio.letters.parse(like)[0] if like is not None else meta
        for key in LETTER_HEAD:
            if key not in values or lmeta.get(key) != meta.get(key):
                continue
            v = str(values[key])
            if key == "to" and ("\n" in v or isinstance(meta.get("to"), list)):
                meta[key] = [l.strip() for l in v.split("\n") if l.strip()]
            else:
                meta[key] = v.strip() or None
        return studio.letters.dump(meta, body)

    import io
    data = studio.yaml_rt.load(text)
    cv = data.get("cv") if data else None
    if cv is None:
        return None
    lcv = ((studio.to_plain(studio.yaml_rt.load(like)) or {}).get("cv") or {}) if like is not None else None
    if k == "header":
        for key in HEADER_KEYS:
            if key not in values:
                continue
            if lcv is not None and lcv.get(key) != studio.to_plain(cv.get(key)):
                continue
            v = str(values[key]).strip()
            if v:
                cv[key] = _typed(cv.get(key), v)
            elif key in cv and key != "name":
                del cv[key]
    elif k == "entry":
        entries = (cv.get("sections") or {}).get(block.get("name"))
        if entries is None:
            return None
        j = i
        if lcv is not None:
            was = ((lcv.get("sections") or {}).get(block.get("name")) or [])
            if not 0 <= i < len(was):
                return None
            j = next((n for n, e in enumerate(entries) if studio.to_plain(e) == was[i]), None)
        if j is None or not 0 <= j < len(entries):
            return None
        e = entries[j]
        if isinstance(e, str):
            new = str(values.get("text") or "").strip()
            if not new:
                raise ValueError("To remove it, ask Claude: an empty line would not print.")
            entries[j] = new
        else:
            for key, v in values.items():
                if key not in e:
                    continue
                if key == "highlights":
                    lines = [l.strip() for l in str(v).split("\n") if l.strip()]
                    lines = [l[2:].strip() if l[:2] in ("- ", "• ", "* ") else l for l in lines]
                    e[key] = lines
                elif str(v).strip():
                    e[key] = _typed(e.get(key), str(v).strip())
                else:
                    del e[key]
    else:
        return None
    buf = io.StringIO()
    studio.yaml_rt.dump(data, buf)
    return buf.getvalue()


@tool(view=PAGE_VIEW, app_only=True)
def edit_on_page(path: str, block: dict, values: dict, src: str, page: int = 1) -> CallToolResult:
    """Save the user's own small edit of one block of a document, from the
    page view: `values` by field, as page_view_data(what="fields") gave them,
    `src` its signature then, so a document that changed since is refused.
    Saved as the user's edit, not an AI client's. Returns the page as it is
    afterwards. For the user's hand only: hidden from the model."""
    p = _doc_path(path)
    letter = studio.is_letter(p)
    text = p.read_text(encoding="utf-8")
    if _src_sig(text) != src:
        raise ValueError("The document changed since this page was drawn. Edit it on the latest page.")
    new = _apply_block(text, letter, block, values or {})
    if new is None:
        raise ValueError("That part is no longer there. Render it again.")
    if new == text:
        return _render(path, page, True)
    rp = studio.rel(p)
    kept = review.entry(studio.WORKSPACE, rp)
    kept_before = (kept or {}).get("before")
    if kept_before is not None:
        # What the AI client changed stays reviewable; the user's own words
        # never become part of it.
        try:
            mirrored = _apply_block(kept_before, letter, block, values or {}, like=text)
        except ValueError:
            mirrored = None
        if mirrored is not None and mirrored != kept_before:
            review.set_before(studio.WORKSPACE, rp, mirrored)
    p.write_text(new, encoding="utf-8")
    if not letter:
        try:
            studio.record_edits(p, studio.to_plain(studio.yaml_rt.load(text)),
                                studio.to_plain(studio.yaml_rt.load(new)), "page", by="you")
        except Exception:
            pass
    result = _render(path, page, True)
    if result.is_error:
        # Nothing half-saved: the file goes back, and the page with it.
        p.write_text(text, encoding="utf-8")
        if kept_before is not None:
            review.set_before(studio.WORKSPACE, rp, kept_before)
        _render(path, page, True)
        why = next((c.text for c in result.content if c.type == "text"), "it did not render")
        why = why.replace("RENDER FAILED", "").strip()
        raise ValueError("Not saved: with that change the page does not render. " + why[:600])
    return result


