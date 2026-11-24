"""Document, ATS, cover letter, language, design, and workspace MCP tools."""

from __future__ import annotations

import json
import re
from pathlib import Path

from mcp.types import CallToolResult, TextContent

import ats
import studio
from .runtime import PAGE_VIEW, _app_note, _doc_path, _one_line, _shows_views, _ws, tool
from . import page_view as _pv


@tool
def list_cvs() -> list[dict]:
    """List every CV in the workspace, with its path and where it lives."""
    return studio.list_documents()


@tool
def read_cv(path: str) -> str:
    """Read a CV's YAML source. `path` is relative to the workspace."""
    return studio.safe_path(path).read_text(encoding="utf-8")


def _written(path: str, said: str) -> CallToolResult:
    """What a writing tool answers: text. It shows no view of its own, since
    each view is a new page in the conversation; render_cv shows the change
    once every edit a request needs is made."""
    return CallToolResult(content=[TextContent(type="text", text=said)])


@tool
def write_cv(path: str, content: str) -> CallToolResult:
    """Overwrite a CV's YAML source with `content`.

    Prefer edit_cv_fields for small changes: this replaces the whole file and
    will drop any comments the user wrote that are not in `content`.
    """
    p = _doc_path(path)
    changed = studio.write_doc(p, content, "write_cv")["changed"]
    return _written(path, f"Wrote {len(content)} characters to {path}. "
                          f"{len(changed)} field(s) changed.")


@tool
def edit_cv_fields(path: str, edits: list[dict]) -> CallToolResult:
    """Change individual fields, preserving the rest of the file and its comments.

    Each edit is {"path": ["cv", "headline"], "value": "Solutions Engineer"}.
    List positions are integers: ["cv","sections","experience",0,"company"].
    """
    p = _doc_path(path)
    result = studio.apply_patches(p, edits, "edit_cv_fields")
    lines = [f"{len(result['applied'])} of {len(edits)} edit(s) applied to {path}."]
    # A patch whose path does not exist is skipped. Reporting that as a success
    # is how a mis-indexed entry gets believed, so it is named here instead.
    for miss in result["missed"]:
        lines.append(f"  NOT APPLIED: {'.'.join(map(str, miss['path']))} "
                     f"-- {miss['why']}")
    if result["missed"]:
        lines.append("Read the file before retrying: those paths do not exist.")
    if not result["applied"]:
        return CallToolResult(content=[TextContent(type="text", text="\n".join(lines))])
    return _written(path, "\n".join(lines))


def _versions_text(h: dict, limit: int) -> str:
    whos = {"you": "you", "start": "as it was when history began", "elsewhere": "outside CV Studio"}
    lines = [f"{h['path']}: {len(h['versions'])} version(s), newest first."
             + ("" if h["exists"] else " The file no longer exists.")]
    for v in h["versions"][:limit]:
        who = whos.get(v["who"]) or v.get("agent") or v["who"]
        ch = v.get("lines") or {}
        what = ("deleted" if v["deleted"] else "" if v["who"] == "start" else
                f"+{ch.get('added', 0)} -{ch.get('removed', 0)} lines" if ch else "")
        lines.append(f"- {v['id']}  {str(v['at'])[:16].replace('T', ' ')}  "
                     + (who if v["who"] == "start" else f"by {who}")
                     + (f" ({v['tool']})" if v.get("tool") else "")
                     + (f", {what}" if what else "") + (f". {v['note']}" if v.get("note") else ""))
    return "\n".join(lines)


@tool
def file_history(path: str | None = None, job_id: str | None = None, version_id: str | None = None,
                 limit: int = 30) -> str:
    """Every version CV Studio kept of a CV, a letter or an application: when,
    and who made it (the user, an AI client and which tool, or a change made
    outside CV Studio). Give `path` for a document or `job_id` for an
    application.

    With `version_id`, what that version changed, line by line, against the
    version before it. Use it when the user asks what changed, when, or by
    whom, or wants something back that a rewrite removed.
    """
    if job_id and not path:
        found = studio.jobstore._find(_ws(), job_id) if studio.jobstore else None
        if not found:
            raise ValueError("No application with that id.")
        path = f"{studio.jobstore.DIR}/{found[0]}"
    if not path:
        raise ValueError("Give a path or a job_id.")
    if version_id:
        d = studio.history_diff(path, version_id)
        out = [f"Version {version_id} of {d['version']['path']}, "
               f"{str(d['version']['at'])[:16].replace('T', ' ')}: "
               f"+{d['summary']['added']} -{d['summary']['removed']} lines against the version before."]
        for ln in d["lines"]:
            out.append({"add": "+ ", "del": "- ", "same": "  ", "skip": "  ... "}[ln["t"]] + ln["text"])
        return "\n".join(out)
    return _versions_text(studio.history_of(path), max(1, min(limit, 300)))


@tool
def restore_version(path: str, version_id: str) -> str:
    """Put a CV or a cover letter back as one of its earlier versions (ids
    from file_history). Only when the user asks for it. The restore is a
    version of its own, so nothing between is lost, and it is shown to the
    user as your change, to keep or undo.

    Applications are not restored here: a restore would also take back notes
    and statuses the user set since. They can restore one in the app.
    """
    p = _doc_path(path)
    r = studio.restore_version(studio.rel(p), version_id, who=studio.CLIENT_ID or "ai",
                               agent=studio.CLIENT_AGENT, tool="restore_version")
    v = r["restored"]
    return (f"Restored {r['path']} to its version of {str(v['at'])[:16].replace('T', ' ')}. "
            f"Call render_cv to show it.")




@tool
def set_base_cv(path: str) -> str:
    """Make a CV the user's base CV: the one "my CV" means, that every
    tailored copy is made from, and that the app compares copies with.

    Only when the user has said which CV it is, in their words. Ask them,
    naming the CVs as they would, and never choose one for them: the choice
    changes what every later tailoring starts from.
    """
    p = _doc_path(path)
    studio.set_base_cv(studio.rel(p))
    return (f"{studio.rel(p)} is the base CV now. Tailor by copying it: "
            f"create_cv(name=..., copy_from={studio.rel(p)!r}).")


@tool
def create_cv(name: str, copy_from: str | None = None, kind: str = "cv") -> str:
    """Create a CV or a cover letter, blank or duplicated from an existing one.

    Duplicating is the normal way to tailor: copy the base CV, then edit the
    copy for a specific job so the original stays intact.

    `kind` is "cv" or "letter". It decides the folder, and the folder is what
    the app reads to tell the two apart -- a letter written into profile/ would
    be listed as a CV.
    """
    safe = "".join(c for c in name if c.isalnum() or c in "-_ ").strip()
    if not safe:
        raise ValueError("Give it a name.")
    if kind not in ("cv", "letter"):
        raise ValueError('kind must be "cv" or "letter".')
    if kind == "letter":
        # Letters are Markdown now; create_letter is the way to start one for
        # an application, this the way to start one for nothing in particular.
        return f"Created {studio.new_letter(None, safe)['path']}. Write it with write_letter."
    folder = "letters" if kind == "letter" else "profile"
    dest = studio.safe_path(f"{folder}/{safe}.yaml")
    if dest.exists():
        raise ValueError(f"{folder}/{safe}.yaml already exists.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        studio.safe_path(copy_from).read_text(encoding="utf-8") if copy_from
        else studio.STARTER_CV,
        encoding="utf-8",
    )
    # Remember what this was copied from. It is what makes "how does this
    # differ from the base CV" answerable later, for the app and for you.
    if copy_from:
        studio.note_lineage(dest, studio.rel(studio.safe_path(copy_from)))
        return (f"Created {folder}/{safe}.yaml, tailored from {copy_from}. "
                f"The app will mark every field that differs from it.")
    return f"Created {folder}/{safe}.yaml"



@tool(view=PAGE_VIEW)
def ats_check(path: str, job_id: str | None = None) -> CallToolResult:
    """Read a CV's rendered PDF the way an applicant tracking system does.

    Renders first if the PDF is older than the YAML. Returns the parsing
    problems found (icons that extract as junk characters, a profile shown as
    a bare username, non-standard headings, entries missing or out of order in
    the text layer) and, against the posting of `job_id` or of the application
    this CV is attached to, which of the posting's keywords the CV uses.

    Use it after tailoring. A missing keyword is worth working in only where
    it is true of the user: never add a skill they have not claimed, and put
    it in the bullet that proves it, not only in the skills list, since some
    screening models (Workday HiredScore) look for evidence in the
    experience. The
    keyword list is picked out of the posting by a heuristic, so read it as a
    prompt, not a checklist. `design.header.connections.show_icons: false` and
    `display_urls_instead_of_usernames: true` fix the two commonest parsing
    problems.

    In a client that shows views, the user sees the page with the posting's
    requirements and keywords beside it, each found (and where) or missing;
    picking a missing one asks you, in their words, to add evidence for it.
    """
    p = studio.safe_path(path)
    views = _shows_views()
    # The page first: a render brings the PDF up to date for the check too.
    page = _pv._render(path, 1, True) if views and studio.is_cv_yaml(p) else None
    r = studio.ats_report(p, job_id)
    if not r.get("ok"):
        raise ValueError(r.get("error") or "The check could not run.")
    kw = r.get("keywords")
    report = {
        "pages": r["pages"], "words": r["words"],
        "problems": [{"title": c["title"], "detail": c["detail"]}
                     for c in r["checks"] if c["level"] != "ok"],
        "against": r.get("against"),
        "keywords": None if not kw else {
            "used": f"{len(kw['found'])} of {kw['total']}",
            "found": [t["term"] for t in kw["found"]],
            "missing": [t["term"] for t in kw["missing"]]},
    }
    text = TextContent(type="text", text=json.dumps(report, indent=2, ensure_ascii=False))
    if page is None or page.is_error or not page.structured_content:
        return CallToolResult(content=[text])
    view = dict(page.structured_content)
    view["view"] = "cv-match"
    view["match"] = _match_view(p, r, job_id)
    return CallToolResult(content=[text], structured_content=view)


def _match_view(p: Path, r: dict, job_id: str | None) -> dict:
    """What the match view lists: each keyword with the blocks it is in, or
    the job that should carry it; the posting's requirements; the parsing
    problems. Posting text is shown to the user, so it is cut to one short
    line each: it reaches the model only if they send it."""
    data = studio.to_plain(studio.yaml_rt.load(p.read_text(encoding="utf-8"))) or {}
    cv = data.get("cv") or {}
    blocks: list[tuple[dict, str]] = []
    head = " ".join(str(cv.get(k) or "") for k in ("name", "headline", "location", "label"))
    blocks.append(({"k": "header", "name": None, "i": None}, ats._norm(head)))
    for name, entries in (cv.get("sections") or {}).items():
        for i, e in enumerate(entries or []):
            blocks.append(({"k": "entry", "name": name, "i": i}, ats._norm(_pv._flat(e, 20000))))
    kw = r.get("keywords") or {}
    found, missing = [], []
    for t in kw.get("found") or []:
        where = [b for b, txt in blocks if ats._has(t["key"], txt)]
        found.append({"term": _one_line(t["term"], 40), "key": t["key"], "where": where})
    hits: dict[tuple, int] = {}
    for f in found:
        for b in f["where"]:
            hits[(b["k"], b["name"], b["i"])] = hits.get((b["k"], b["name"], b["i"]), 0) + 1
    # Where a missing keyword would go: the job that already says most of
    # what the posting asks, else the most recent one, else the summary.
    jobs = [b for b, _ in blocks if b["k"] == "entry" and
            re.search(r"experi|work|employ|career|project|emploi|trabajo|trabalho|erfahrung|beruf",
                      str(b["name"]), re.I)]
    best = max(jobs, key=lambda b: (hits.get((b["k"], b["name"], b["i"]), 0), -b["i"]), default=None) \
        or next((b for b, _ in blocks if b["k"] == "entry" and
                 re.search(r"summary|profil|about|resum|perfil|sobre|zusammen", str(b["name"]), re.I)), None) \
        or {"k": "header", "name": None, "i": None}
    for t in kw.get("missing") or []:
        missing.append({"term": _one_line(t["term"], 40), "key": t["key"], "best": best})
    posting = None
    job = studio.job_for(p, job_id)
    if job and job.get("description"):
        posting = job["description"]
    terms = (kw.get("found") or []) + (kw.get("missing") or [])
    found_keys = {t["key"] for t in kw.get("found") or []}
    reqs = []
    for q in ats.requirements(posting or "", terms):
        reqs.append({"text": _one_line(q["text"], 160), "terms": q["terms"],
                     "found": [k for k in q["terms"] if k in found_keys]})
    against = r.get("against") or {}
    return {"company": _one_line(against.get("company"), 60) or None,
            "title": _one_line(against.get("title"), 80) or None,
            "has_posting": bool(kw), "total": kw.get("total") or 0, "rate": kw.get("rate"),
            "found": found, "missing": missing, "requirements": reqs, "best": best,
            "problems": [_one_line(c["title"], 90) for c in r["checks"] if c["level"] != "ok"]}


@tool
def create_letter(job_id: str) -> dict:
    """Start a cover letter for an application, and attach it to it.

    Writes letters/cover-<company>.md with everything but the words filled in:
    the subject, greeting and closing in the posting's language, today's date,
    and the look of the application's CV. The body holds three short prompts
    for what each paragraph is for: replace them with write_letter.
    """
    r = studio.new_letter(job_id)
    meta, body = studio.letters.parse(studio.safe_path(r["path"]).read_text(encoding="utf-8"))
    return {"path": r["path"], "header": meta, "body": body,
            "next": "Write the letter with write_letter, then render_cv to look at it. "
                    "Aim for about 250 words, never more than one page. Every achievement, "
                    "title, date, number and level in it is already on the CV it goes "
                    "with; the context around them comes from the user."}


@tool
def write_letter(path: str, body: str, subject: str | None = None) -> CallToolResult:
    """Replace a cover letter's body, and its subject line if given.

    `body` is the whole letter from greeting to closing, as Markdown, all of
    which prints: paragraphs separated by blank lines, # to ### headings,
    '- ' and '1. ' lists (nested by indenting), '> ' quotes, '---' rules,
    fenced code, pipe tables, **bold**, *italic*, ~~struck~~, `code` and
    [text](url). Most cover letters need only paragraphs; use the rest where
    it helps the reader. The name, contact details and signature are printed
    from the CV the letter looks like, so leave them out. The header is kept.
    Every achievement, title, date, number, tool and level must already be on
    that CV. What a letter adds is motivation and the context around those
    facts (what was hard, what the constraint was), and that context comes
    from the user, never from you.
    """
    p = studio.safe_path(path)
    if not studio.is_letter(p):
        raise ValueError(f"{path} is not a cover letter. Letters are letters/*.md.")
    payload = {"body": body}
    if subject is not None:
        payload["meta"] = {"subject": subject}
    r = studio.save_letter(p, payload)
    return _written(path, f"Wrote {path}: {r['words']} words."
                    + " render_cv it to see the page.")


@tool
def add_language(path: str, language: str) -> dict:
    """Start a translation of a CV into another language.

    Writes <name>.<code>.yaml beside `path` and links it to it. The copy
    already prints dates, month names and "present" in `language` (a code such
    as "fr", or a RenderCV name such as "french"), and its common section
    titles are translated. Everything else is still in the source language:
    translate it with edit_cv_fields, field by field. Leave names, email,
    phone, links, company names, dates and the design exactly as they are;
    the design follows the source's automatically.

    Section titles listed in `sections_to_title` are ones CV Studio could not
    translate. RenderCV prints a section's key as its title, so rename those
    keys by rewriting the file with write_cv, keeping the entries as they are.

    Then render_cv the result and look at it.
    """
    code = studio.languages.code_of(language)
    if code == "en" and str(language).strip().lower() not in ("en", "english"):
        raise ValueError(f"CV Studio cannot print a CV in {language!r}. It can in: "
                         + ", ".join(v[2] for v in studio.languages.LANGS.values()))
    return studio.add_language(studio.safe_path(path), code)


@tool
def translation_status(path: str) -> dict:
    """What a translated CV is missing from the CV it was translated from.

    Lists every field the source changed since the translation was last
    brought up to date: where it is, what the source said then and says now,
    `key` (the same field's path in the translation, for edit_cv_fields) and
    what the translation says there now. Empty `changes` means it is current.
    """
    drift = studio.translation_drift(studio.safe_path(path))
    if drift is None:
        raise ValueError(f"{path} is not a translation. list_cvs shows each CV's "
                         "`translation_of`.")
    return drift


@tool
def mark_translation_current(path: str) -> dict:
    """Record that a translation now says everything its source says.

    Call it after carrying over every change translation_status listed, and
    only then: from here on, only later changes to the source are listed.
    """
    return studio.mark_translation_current(studio.safe_path(path))


@tool
def design_options() -> dict:
    """The themes, fonts and page sizes available for the design block."""
    # available_themes() asks RenderCV rather than trusting the fallback list,
    # which is what the Design screen shows -- the two should not disagree.
    return {
        "themes": studio.available_themes(),
        "fonts": studio.font_families(),
        "page_sizes": studio.PAGE_SIZES,
        "note": "Set these under design.theme, design.typography.font_family.body "
                "and design.page.size.",
    }


@tool
def workspace_info() -> dict:
    """Where the workspace is and what is in it."""
    ws = _ws()
    base = studio.base_cv()
    return {
        "workspace": str(ws),
        "cv_count": len(studio.list_documents()),
        "job_count": len(studio.jobstore.list_jobs(ws)) if studio.jobstore else 0,
        # The document every tailored CV starts from. Named here because it is
        # the first thing worth knowing before writing a CV in this workspace:
        # tailoring means create_cv(copy_from=<this>), not starting over.
        "base_cv": (base or {}).get("path"),
        "base_cv_note": (
            "Tailor by copying it: create_cv(name=..., copy_from=<base_cv>). "
            "The app then marks every field that differs from it."
            if base and not base.get("missing") else
            "The user has not chosen one, so there is nothing to tailor from "
            "yet. Ask which CV it is rather than picking one for them, then "
            "record their answer with set_base_cv."),
        # The one photo the user chose for their CVs, if any. A CV shows it
        # by pointing cv.photo at it; nothing else about it is yours to change.
        "photo": (
            f"{studio.PHOTO_FILE} at the workspace root. A CV shows it when "
            "cv.photo is its path relative to that CV (../photo.jpg for one in "
            "profile/), and hides it when cv.photo is null. Only turn it on or "
            "off when the user asks; the photo itself is theirs to change."
            if studio.photo_info() else
            "None. The user adds one in the app, under Design, Photo."),
        "storage": "CVs are plain YAML files the user owns. Applications are "
                   "too, one file each in tracker/; change them through the "
                   "tools, which keep the status history and the app in step. "
                   "They also export to JSON and CSV.",
        # Some users have only this connector (the Claude Desktop extension),
        # and a tool's "in the app" is a dead end for them.
        **_app_note(),
    }

