"""MCP server for CV Studio.

Lets an AI client (Claude Desktop, or anything else speaking MCP) read, edit and
render the CVs in a CV Studio workspace. It shares the same code as the desktop
app, so a CV rendered here is byte-identical to one rendered by clicking Save.

The important design choice is that `render_cv` returns the rendered page as an
*image*, not just a file path. That lets the model actually look at the result
and catch what only shows up visually: a bullet stranded alone on page two, a
heading orphaned at a page break, a lopsided final page. Those are invisible in
the YAML and obvious in the picture.

Run it with:  cv-studio-server --mcp
"""

from __future__ import annotations

import base64
import contextvars
import functools
import hashlib
import inspect
import json
import os
import re
import time
from pathlib import Path
from typing import Annotated

from pydantic import Field

from mcp.server.apps import Apps, client_supports_apps
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.prompts.base import Prompt
from mcp.types import (BlobResourceContents, CallToolResult, EmbeddedResource, Icon, ImageContent,
                       TextContent, ToolAnnotations)

import ats
import cjkfonts
import review
import studio
from cv_render import render_file

def _icons() -> list[Icon]:
    """The app's mark, inline: a client shows it beside the connector and its
    tools, and an inline image needs no network to show."""
    try:
        mark = (Path(__file__).resolve().parent / "static" / "brand-mark.png").read_bytes()
    except OSError:
        return []
    return [Icon(src="data:image/png;base64," + base64.b64encode(mark).decode(),
                 mime_type="image/png", sizes=["64x64"])]


# Views a client can show inside the conversation (MCP Apps). A tool bound to
# one still answers in text and an image, which is all a client without MCP
# Apps sees and all the model ever reads; the view gets the rest in the
# result's structured content, which is sent only to a client that said it
# can show views, so it never lands in a model's context by mistake.
PAGE_VIEW = "ui://cv-studio/page.html"
apps = Apps()
apps.add_html_resource(
    PAGE_VIEW,
    (Path(__file__).resolve().parent / "static" / "mcp-page.html").read_text(encoding="utf-8"),
    name="cv-page", title="CV page",
    # No frame around it: the view sits on the conversation like the rest
    # of it. Said explicitly, since hosts' defaults differ.
    prefers_border=False,
    description="The rendered page, every page of it; click a block to tell "
                "Claude what to change there.")

mcp = MCPServer(
    name="cv-studio",
    extensions=[apps],
    title="CV Studio",
    version=studio.VERSION,
    website_url="https://github.com/MaxFoxZebra/supreme-engine",
    icons=_icons(),
    instructions=(
        "Read, edit and render CVs in the user's CV Studio workspace.\n\n"
        "CVs are RenderCV YAML files. Two rules save most failures:\n"
        "1. A colon followed by a space inside a bullet turns it into a YAML "
        "field. Quote such text or use a >- block.\n"
        "2. Phone numbers are validated against real numbering plans, not just "
        "their format.\n\n"
        "After editing, always call render_cv and look at the returned page "
        "image before telling the user it is done. Page-break problems are "
        "invisible in the source.\n\n"
        "You can also keep the user's job applications up to date. If you have "
        "access to their mail or calendar, those are read-only sources: read "
        "them, and write what you learn in here. Never write anything back to "
        "them, no events, no replies, no labels.\n\n"
        "Three rules for the tracker:\n"
        "1. Call find_job before writing anything. The company name is the "
        "only reliable key and it is often ambiguous. If it returns nothing, "
        "or more than one candidate, ask rather than guessing.\n"
        "2. Show the user every change you intend to make and wait for them "
        "to agree. A status change is appended to a permanent history that "
        "the funnel is drawn from, and this app has no undo.\n"
        "3. An automated acknowledgement is not a status change. Record it "
        "with last_contact_at and leave the status alone. Never set a ghosted "
        "status from silence: absence of a message is not a message.\n\n"
        "When the user asks to prepare an interview, read the prep with "
        "get_interview_prep, the posting with read_job and the CV that was "
        "sent with read_cv, then write better questions, stories and asks "
        "with save_interview_prep. Never invent experience: a story is a "
        "line of the CV, or a gap.\n\n"
        "When a thread or an invitation names the recruiter, the hiring "
        "manager or an interviewer, record them with save_person: the app "
        "drafts follow-ups and thank-you notes to them.\n\n"
        "Never type a job's title or posting yourself when you have its link: "
        "a summary of a page is not the page. Pass the link to add_job, which "
        "reads the title and the full text from the job board itself, or call "
        "read_posting first. If it cannot read it, ask the user to paste the "
        "posting rather than writing one from memory.\n\n"
        "Adding a job is the start of applying to it. Unless the user said to "
        "only track it, follow each add_job with a CV tailored to the posting "
        "(create_cv copying the base CV in the posting's language, "
        "edit_cv_fields, render_cv, ats_check, then update_job_tracking with "
        "cv_path) and a cover letter (create_letter, write_letter, render_cv). "
        "add_job's `next` says the same. Never tailor against a summary of "
        "the posting, and never add experience the base CV does not have.\n\n"
        "When you add an application, pass company_website: the company's own "
        "domain, found from the posting or its careers page, not the job "
        "board's. Its logo is fetched from there and shown on the row.\n\n"
        "CVs can exist in several languages. A translation is its own file, "
        "linked to the CV it was translated from. To translate, call "
        "add_language: it writes the copy with dates, month names and section "
        "titles already in the new language. Then translate the remaining "
        "text with edit_cv_fields, and leave names, contact details, links, "
        "company names and dates as they are. When the source CV changes, "
        "translation_status lists what the translation is missing; carry each "
        "change over, then call mark_translation_current. To tailor for a "
        "posting, copy the base CV in the posting's language (list_cvs says "
        "each CV's `lang`; read_job says the application's `language`).\n\n"
        "Cover letters are Markdown files in letters/, not RenderCV: a short "
        "header (application, company, looks_like, place, date, subject, "
        "language) and the letter below it, as it would be typed in an email. "
        "It prints bold, italic, [links](url) and '- ' bullet lists, nothing "
        "else. The letterhead, font and colours come from the CV named in "
        "looks_like, so never write a name or contact details into the body. "
        "create_letter starts one for an application; write_letter replaces "
        "its body (and subject); render_cv shows the page."
    ),
)


def _ws() -> Path:
    return studio.WORKSPACE


# How a client names itself at initialize, mapped onto the ids the app already
# uses for the clients it can configure. The app normally passes --client, since
# it wrote the config and therefore knows; this is the fallback for a config
# somebody wrote by hand, where the handshake is the only thing that knows.
CLIENT_NAMES = {
    "claude-ai": "claude", "claude-desktop": "claude", "claude-code": "claude",
    "claudecode": "claude", "claude": "claude",
    "codex": "openai", "codex-cli": "openai", "chatgpt": "openai",
    "openai": "openai", "openai-codex": "openai",
    "vibe": "mistral", "mistral": "mistral", "mistral-vibe": "mistral",
    "hermes": "hermes", "hermes-agent": "hermes", "nous": "hermes",
}


def _identify(ctx: Context) -> None:
    """Learn who we are serving, once, from the initialize handshake.

    --client already answers this when the app wrote the config, so this only
    fills the gap. The name is matched loosely because clients spell
    themselves differently across versions, and an unrecognised one still gets
    its full title recorded even though no mark is drawn for it.
    """
    if studio.CLIENT_ID and studio.CLIENT_AGENT:
        return
    try:
        info = ctx.session.client_params.client_info
    except (AttributeError, ValueError):
        return
    if info is None:
        return
    name = (getattr(info, "name", "") or "").strip()
    if not studio.CLIENT_AGENT:
        version = (getattr(info, "version", "") or "").strip()
        title = (getattr(info, "title", "") or "").strip()
        studio.CLIENT_AGENT = " ".join(x for x in (title or name, version) if x)
    if not studio.CLIENT_ID:
        key = name.lower().replace("_", "-").replace(" ", "-")
        studio.CLIENT_ID = CLIENT_NAMES.get(key) or next(
            (v for k, v in CLIENT_NAMES.items() if k in key), None)


# What to name in the activity log, in order of preference. A document tool
# identifies itself by path; an application tool has no path, so it says which
# company it touched instead. Without this every job tool would log a bare verb
# and "Recent activity" would stop being worth reading.
TARGET_KEYS = ("path", "name", "company", "job_id", "query")

# Tools that only read. Every other tool is watched for the documents it
# changes, so what it wrote can be shown back to the user as changes to keep
# or undo (review.py). Watching is a stat of each document and a read of its
# text, which is nothing next to the model's own turn.
# Tools a view calls for the user, hidden from the model ("app" visibility):
# what they change is the user's decision, so it is neither recorded as an AI
# client's change to review nor logged as its activity.
USER_ACTIONS = {"review_change", "page_view_data", "edit_on_page"}

READ_ONLY = {"list_cvs", "read_cv", "render_cv", "list_jobs", "read_job", "find_job",
             "job_alerts", "calendar", "get_interview_prep", "read_posting",
             "ats_check", "translation_status", "design_options", "workspace_info",
             "page_view_data"}

# What a client shows for each tool, and what it may assume of it. Claude
# Desktop and others use the hints to decide what to ask permission for:
# a read-only tool can be allowed once for good, a destructive one is the
# one worth a second look. Every tool not read-only is destructive unless it
# only ever adds (a new CV, a new application); idempotent ones give the same
# result called twice; open-world ones reach a website.
TITLES = {
    "list_cvs": "List CVs and letters", "read_cv": "Read a CV",
    "write_cv": "Replace a CV", "edit_cv_fields": "Edit fields of a CV",
    "create_cv": "Create a CV", "render_cv": "Render a page",
    "set_company_logo": "Set a company's logo", "list_jobs": "List applications",
    "read_job": "Read an application", "find_job": "Find an application",
    "job_alerts": "What needs attention", "calendar": "Interviews and follow-ups",
    "set_job_status": "Change an application's status",
    "update_job_tracking": "Update an application", "save_person": "Save a contact",
    "get_interview_prep": "Read interview prep", "save_interview_prep": "Save interview prep",
    "add_job": "Add an application", "read_posting": "Read a job posting",
    "ats_check": "Check how an ATS reads a CV", "create_letter": "Start a cover letter",
    "write_letter": "Write a cover letter", "add_language": "Translate a CV",
    "translation_status": "What a translation is missing",
    "mark_translation_current": "Mark a translation up to date",
    "design_options": "Themes, fonts and page sizes", "workspace_info": "About the workspace",
    "review_change": "Keep or undo a change",
    "page_view_data": "Page view data",
    "edit_on_page": "Edit on the page",
}
ADDITIVE = {"create_cv", "add_job", "create_letter", "add_language"}
IDEMPOTENT = {"review_change", "edit_on_page", "write_cv", "edit_cv_fields", "set_company_logo", "set_job_status",
              "save_interview_prep", "write_letter", "mark_translation_current"}
OPEN_WORLD = {"add_job", "read_posting", "set_company_logo"}


def _annotations(name: str) -> ToolAnnotations:
    ro = name in READ_ONLY
    return ToolAnnotations(
        title=TITLES.get(name),
        read_only_hint=ro,
        destructive_hint=None if ro else name not in ADDITIVE,
        idempotent_hint=True if ro else name in IDEMPOTENT,
        open_world_hint=name in OPEN_WORLD)


def _snapshot() -> dict:
    out = {}
    for f, _, _ in studio.document_files():
        try:
            out[studio.rel(f)] = (f.stat().st_mtime, f.read_text(encoding="utf-8"))
        except OSError:
            continue
    return out


def _checkpoint(before: dict, tool_name: str) -> None:
    """Keep what each document said before this tool changed it."""
    try:
        for f, _, _ in studio.document_files():
            rel = studio.rel(f)
            held = before.get(rel)
            try:
                if held and f.stat().st_mtime == held[0]:
                    continue
                if held and f.read_text(encoding="utf-8") == held[1]:
                    continue
            except OSError:
                continue
            review.checkpoint(studio.WORKSPACE, rel, held[1] if held else None,
                              by=studio.CLIENT_ID or "ai", agent=studio.CLIENT_AGENT,
                              tool=tool_name)
    except Exception:
        # Bookkeeping is never what fails a tool call.
        pass


# The client context of the call in progress, for a tool that answers
# differently to a client that can show views.
_CALL_CTX: contextvars.ContextVar = contextvars.ContextVar("cvs_call_ctx", default=None)


def _shows_views() -> bool:
    ctx = _CALL_CTX.get()
    try:
        return ctx is not None and client_supports_apps(ctx)
    except Exception:
        return False


def tool(fn=None, *, view: str | None = None, app_only: bool = False):
    """Register a tool, and leave a note in the workspace that it ran.

    `view` binds it to a ui:// view a client that supports MCP Apps shows
    with its result.

    The app is very likely open on the file being edited, in another process
    that cannot see this one. The note is how it finds out, so it can offer to
    reload rather than quietly save over what the model just wrote. Read-only
    tools are recorded too: "Claude is looking at this" is worth showing.

    The wrapper takes a Context purely to learn which client it is serving.
    MCPServer detects that by annotation and strips it from the published
    schema, so it costs the model nothing -- but it detects it on the function
    it is handed, which is this wrapper, so the signature has to advertise it
    rather than inherit the wrapped function's through functools.wraps.
    """
    if fn is None:
        return lambda f: tool(f, view=view, app_only=app_only)
    # Resolved, not the strings `from __future__ import annotations` leaves:
    # the SDK reads the return type to decide whether a tool has an output
    # schema, and a CallToolResult it cannot recognise gets one it then fails.
    signature = inspect.signature(fn, eval_str=True)

    @functools.wraps(fn)
    def wrapper(*args, cvs_ctx: Context = None, **kwargs):
        if cvs_ctx is not None:
            _identify(cvs_ctx)
        _CALL_CTX.set(cvs_ctx)
        try:
            bound = signature.bind(*args, **kwargs)
            target = next((bound.arguments[k] for k in TARGET_KEYS
                           if bound.arguments.get(k)), None)
        except TypeError:
            target = None
        # Record what happened, not merely that it was attempted: a refused
        # call logged like a successful one tells the user the model read a
        # file it was actually blocked from reading.
        if fn.__name__ in USER_ACTIONS:
            # The user's own decision, made in a view: not an AI client's
            # change to review, nor its activity to show.
            return fn(*args, **kwargs)
        watch = None
        if fn.__name__ not in READ_ONLY:
            try:
                watch = _snapshot()
            except Exception:
                watch = None
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            if watch is not None:
                _checkpoint(watch, fn.__name__)
            studio.note_mcp_activity(fn.__name__,
                                     str(target) if target else None,
                                     ok=False, error=str(exc)[:200])
            raise
        if watch is not None:
            _checkpoint(watch, fn.__name__)
        studio.note_mcp_activity(fn.__name__,
                                 str(target) if target else None)
        return result

    wrapper.__signature__ = signature.replace(parameters=[
        *signature.parameters.values(),
        inspect.Parameter("cvs_ctx", inspect.Parameter.KEYWORD_ONLY,
                          annotation=Context, default=None),
    ])
    wrapper.__annotations__ = {**getattr(fn, "__annotations__", {}),
                               "cvs_ctx": Context}
    return mcp.tool(title=TITLES.get(fn.__name__),
                    annotations=_annotations(fn.__name__),
                    meta={"ui": {"resourceUri": view, **({"visibility": ["app"]} if app_only else {})}}
                    if view else None)(wrapper)


@tool
def list_cvs() -> list[dict]:
    """List every CV in the workspace, with its path and where it lives."""
    return studio.list_documents()


@tool
def read_cv(path: str) -> str:
    """Read a CV's YAML source. `path` is relative to the workspace."""
    return studio.safe_path(path).read_text(encoding="utf-8")


def _doc_path(path: str):
    """A document this tool may write. Applications are files in tracker/
    too, but a model changes them through the tracking tools, which keep the
    history and cannot erase notes; writing the file would do both."""
    p = studio.safe_path(path)
    if studio.rel(p).split("/")[0] in (studio.jobstore.DIR, ".trash"):
        raise PermissionError(f"{path} is an application, not a CV. Change it with "
                              "update_job_tracking.")
    return p


@tool
def write_cv(path: str, content: str) -> str:
    """Overwrite a CV's YAML source with `content`.

    Prefer edit_cv_fields for small changes: this replaces the whole file and
    will drop any comments the user wrote that are not in `content`.
    """
    p = _doc_path(path)
    changed = studio.write_doc(p, content, "write_cv")["changed"]
    return (f"Wrote {len(content)} characters to {path}. "
            f"{len(changed)} field(s) changed.")


@tool
def edit_cv_fields(path: str, edits: list[dict]) -> str:
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
    return "\n".join(lines)


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
        else (studio.STARTER_LETTER if kind == "letter" else studio.STARTER_CV),
        encoding="utf-8",
    )
    # Remember what this was copied from. It is what makes "how does this
    # differ from the base CV" answerable later, for the app and for you.
    if copy_from:
        studio.note_lineage(dest, studio.rel(studio.safe_path(copy_from)))
        return (f"Created {folder}/{safe}.yaml, tailored from {copy_from}. "
                f"The app will mark every field that differs from it.")
    return f"Created {folder}/{safe}.yaml"


def _one_line(text, n: int = 80) -> str:
    """CV text as the view may put it in front of the model: one short line
    of plain text. A label is CV text, which can come from an imported PDF or
    a posting."""
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in str(text or "")).split())
    return text[:n - 1] + "…" if len(text) > n else text


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
    data = f.read_bytes()
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


def _page_view(path: str, p: Path, pngs: list[Path], page: int, pages: int,
               words, pdf, bands=None, box=None) -> dict:
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
    shots = [_keep_page(f) for f in pngs[:MAX_VIEW_PAGES]]
    images = shots
    last = _LAST_RENDER.get(str(p))
    changes = _changes(last["data"], data) if last and data and last.get("data") else None
    # What is waiting for the user to keep or undo outranks what differs
    # from the last render: it is what they can act on, and it survives a
    # restart of the client.
    pending = _review_units(p)
    if pending:
        units = pending["units"]
        changes = {"blocks": [u["block"] for u in units if u["block"]],
                   "removed": sum(1 for u in units if u.get("tag") == "del" or
                                  (u["kind"] == "section" and not u["after"])),
                   "design": any(u["id"].startswith("top:") for u in units),
                   "review": pending}
    if not changes and last and letter and last.get("data") and last.get("images") != images:
        changes = _changes(last["data"], data) or {"blocks": [], "removed": 0, "design": False, "page": True}
    _LAST_RENDER[str(p)] = {"data": data, "images": images}
    rid = _record_render(p, data, shots)
    first = min(max(page, 1), len(shots)) - 1 if shots else 0
    return {
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
        "map": [{**b, "label": labels.get(f"{b['k']}|{b.get('name')}|{b.get('i')}")}
                for b in bands or []],
        "box": box,
        "changes": changes,
        "before": last["images"] if changes and last and last.get("images") != images else None,
    }


@tool(view=PAGE_VIEW)
def render_cv(path: str, page: int = 1) -> CallToolResult:
    """Render a CV to PDF and return the page as an image to look at.

    Returns the page count, the word count an ATS would extract, the PDF's
    location, and an image of the requested page. Check the image before
    reporting success: page-break damage does not show up in the YAML.

    In a client that shows views, the user also sees every page in the
    conversation and can click a block to tell you what to change there:
    their message names the block and its place in the YAML. They can also
    keep or undo each change you made, from the page; you are told when
    they undo one, and should not make that change again unless asked.
    """
    return _render(path, page, _shows_views())


def _render(path: str, page: int, views: bool) -> CallToolResult:
    p = studio.safe_path(path)

    def failed(text: str) -> CallToolResult:
        return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)

    def image(f: Path) -> ImageContent:
        return ImageContent(type="image", data=base64.b64encode(f.read_bytes()).decode("ascii"),
                            mime_type="image/png")

    if studio.is_letter(p):
        r = studio.render_letter(p)
        if not r.get("ok"):
            return failed(f"RENDER FAILED\n\n{r.get('error')}")
        pngs = [studio.WORKSPACE / u.split("path=")[1].split("&")[0] for u in r["pngs"]]
        idx = max(1, min(page, r["pages"])) - 1
        summary = f"Rendered {path}\nPages: {r['pages']}\nWords: {r['words']}\nPDF: {r['pdf']}"
        view = None
        if views:
            try:
                mapped = studio.letter_map(p)
            except Exception:
                mapped = None
            view = _page_view(path, p, pngs, idx + 1, r["pages"], r["words"], r["pdf"],
                              (mapped or {}).get("bands"), (mapped or {}).get("box"))
        return CallToolResult(
            content=[TextContent(type="text", text=summary), image(pngs[idx])],
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
        content.append(image(pngs[idx]))
    view = None
    if views and pngs:
        mapped, _ = studio.block_map(result, p)
        view = _page_view(path, p, pngs, idx + 1, pages, result["ats_word_count"],
                          studio.rel(Path(result["pdf"])) if result.get("pdf") else None,
                          (mapped or {}).get("bands"), (mapped or {}).get("box"))
    return CallToolResult(content=content, structured_content=view)


@tool
def set_company_logo(company: str, website: str | None = None,
                     image_path: str | None = None) -> str:
    """Give a company a logo, and use it on every application to that company.

    Pass `website`, the company's own site (stripe.com, not the job board the
    posting was on), and its icon is fetched from there. That request goes to
    the company and nowhere else: there is no logo service in between that
    would learn where the user is applying.

    Or pass `image_path`, an image already on this machine (png, jpg, svg,
    webp, gif or ico). Either way it is copied into the workspace, so the app
    itself never fetches anything to draw it.

    If neither works, leave it: a company with no logo shows its initials,
    which is a deliberate look rather than a gap.
    """
    if image_path:
        saved = studio.save_logo(company, image_path)
    elif website:
        saved = studio.fetch_logo(company, website)
    else:
        raise ValueError("Pass the company's website, or an image_path.")
    n = studio.jobstore.set_company_logo(_ws(), company, saved["logo"])
    if not n:
        return (f"Saved {saved['logo']} ({saved['kb']} KB), but no application "
                f"lists {company!r} yet, so nothing uses it.")
    return (f"{company}: {saved['logo']} ({saved['kb']} KB), now shown on "
            f"{n} application{'' if n == 1 else 's'}.")


# --- Applications -----------------------------------------------------------
#
# The tracker used to be deliberately out of reach here, on the grounds that
# the documents were the model's job and the record of what was sent was the
# user's. That changed when the useful thing became keeping the record in step
# with a mailbox, which is work only something that can read the mail can do.
#
# What is still out of reach is structural rather than advisory. There is no
# delete tool, and no tool takes `company` or `notes`, so a model cannot
# destroy a record or paint over notes they typed. Those cannot be got wrong
# by a model misreading its instructions, because the parameters do not
# exist. Everything else, above all the status change itself, rests on the
# rules in the server instructions.
#
# `title` is the one exception, and it is checked rather than trusted: it can
# only be set to the title the posting itself gives, read from its link here.
# A model that had typed a wrong title (from a summary of the page) could
# otherwise not undo its own mistake, and the row kept the wrong name.
#
# Attaching a document is inside the line rather than outside it. It adds a
# reference and destroys nothing: the worst a wrong one does is show the wrong
# filename on a row, which the user can see and change in a click. Leaving it
# out was what made "tailor a CV for the Acme job" a request a model could do
# nine tenths of -- write the document, and then not be able to say what it
# was for. The path is checked before it is written, because the column is a
# bare string with no foreign key behind it.


def _document(path: str, field: str) -> str | None:
    """Check a document path before it is written onto an application.

    The link is a bare string in the database with no foreign key behind it, so
    nothing downstream will notice a path that points at nothing -- the row
    would simply show a filename that cannot be opened. The cheap check is
    here, at the only place a model can write one.

    An empty string clears the link, the same way it clears a date.
    """
    if path == "":
        return None
    target = studio.safe_path(path)          # raises outside the workspace
    if not target.exists():
        raise ValueError(f"There is no document at {path}.")
    if not studio.is_cv_yaml(target):
        raise ValueError(f"{path} is not a CV or cover letter.")
    rel = studio.rel(target)
    # Which of the two columns a document belongs in is decided by where it
    # lives, which is the same rule the app uses. Crossing them would show a
    # cover letter in the CV column and vice versa.
    letter = rel.startswith("letters/")
    if field == "cv_path" and letter:
        raise ValueError(f"{rel} is a cover letter. Pass it as letter_path.")
    if field == "letter_path" and not letter:
        raise ValueError(f"{rel} is a CV. Pass it as cv_path.")
    return rel


def _brief(job: dict) -> dict:
    """A job as the model needs to see it.

    `description` is the whole posting and `status_history` can be dozens of
    entries. Both are dead weight in a list of forty applications, so the list
    tools drop them and `find_job` returns enough to identify a row and no more.

    `cv_path` and `letter_path` are two short strings and they are the join
    between an application and the document that was sent for it. Stripping
    them meant "which CV went to Acme?" had no answer here, which is the one
    question an app organised around applications is for.
    """
    keep = ("id", "title", "company", "status", "location", "url", "source",
            "followup_date", "interview_at", "interview_tz", "contact_email",
            "last_contact_at", "cv_path", "letter_path", "updated_at")
    return {k: job.get(k) for k in keep if job.get(k) is not None}


@tool
def list_jobs(status: str | None = None, query: str | None = None) -> list[dict]:
    """The user's job applications. Read this before changing anything.

    `status` filters exactly. `query` matches the title, company or notes.
    Returns a trimmed view: call read_job for the full posting text.
    """
    return [_brief(j) for j in
            studio.jobstore.list_jobs(_ws(), status=status, q=query)]


@tool
def read_job(job_id: str) -> dict:
    """One application in full, including the posting text and its history.

    The posting stored in `description` is what to write against when the user
    asks you to tailor a CV for this job.
    """
    for job in studio.jobstore.list_jobs(_ws()):
        if job["id"] == job_id:
            return job
    raise ValueError("No such job.")


def _candidates(company: str, title: str | None = None,
                sender_email: str | None = None) -> list[dict]:
    """Applications that might be the one, best guess first.

    A plain function rather than a tool so add_job can reuse it for its
    duplicate check without logging a second activity entry, and without
    depending on what the decorator leaves attached to the tool.
    """
    needle = (company or "").strip().lower()
    if not needle:
        return []
    hits = []
    for job in studio.jobstore.list_jobs(_ws()):
        name = (job.get("company") or "").strip().lower()
        # Substring both ways: the record says "Acme" and the mail says
        # "Acme Corporation", or the reverse.
        if not (name and (needle in name or name in needle)):
            continue
        score = 2 if name == needle else 1
        if title and title.strip().lower() in (job.get("title") or "").lower():
            score += 2
        if sender_email and job.get("contact_email"):
            if sender_email.strip().lower() == job["contact_email"].strip().lower():
                score += 3
        hits.append((score, job))
    hits.sort(key=lambda pair: pair[0], reverse=True)
    return [job for _, job in hits]


@tool
def find_job(company: str, title: str | None = None,
             sender_email: str | None = None) -> dict:
    """Which application does this message or event belong to?

    Call this before every write. Matching is genuinely uncertain: people apply
    to the same company twice, and mail arrives from an applicant-tracking
    domain that resembles nothing in the record. So this reports what it found
    and refuses to choose.

    `confident` is true only for exactly one candidate. Anything else means ask
    the user which one, or whether to add it.
    """
    found = [_brief(j) for j in _candidates(company, title, sender_email)]
    return {
        "candidates": found,
        "confident": len(found) == 1,
        "note": ("No application matches that company. Ask the user whether to "
                 "add one rather than assuming." if not found else
                 "One match." if len(found) == 1 else
                 "Several matches. Ask the user which one before writing."),
    }


@tool
def job_alerts() -> dict:
    """What needs the user's attention, ready to read out.

    Interviews coming up, follow-ups due, interviews that have been and gone
    with no outcome recorded, and applications that have heard nothing back.
    The same answer the app shows in its own panel.
    """
    data = studio.jobstore.alerts(_ws())
    counts = data["counts"]
    parts = []
    if counts["interview_soon"]:
        parts.append(f"{counts['interview_soon']} interview(s) coming up")
    if counts["followup_due"]:
        parts.append(f"{counts['followup_due']} follow-up(s) due")
    if counts["interview_passed"]:
        parts.append(f"{counts['interview_passed']} interview(s) with no outcome recorded")
    if counts["silent"]:
        parts.append(f"{counts['silent']} application(s) with no reply")
    data["summary"] = ", ".join(parts) if parts else "Nothing needs attention."
    return data


@tool
def calendar(days_ahead: int = 14, ics: bool = False) -> dict:
    """The user's calendar in CV Studio: interviews and follow-ups ahead.

    Each interview has its wall-clock time as the invitation gave it and its
    zone (`interview_tz`, empty when it is the user's own), and `utc`, the
    moment itself: use that when creating an event in a calendar you are
    connected to, so it lands at the right hour wherever the user is.
    Follow-ups are dates, and `overdue` ones are listed too.

    With `ics=True` the answer also carries `ics`, the same events as an
    iCalendar file, for a client that can import one. This app writes to no
    calendar itself: putting these in the user's calendar is yours to do,
    with their say-so.
    """
    import datetime as dt
    days_ahead = max(1, min(int(days_ahead or 14), 90))
    now = dt.datetime.now().astimezone()
    horizon = now + dt.timedelta(days=days_ahead)
    today = now.date().isoformat()
    interviews, followups = [], []
    for job in studio.jobstore.list_jobs(_ws()):
        brief = {"job_id": job["id"], "company": job["company"], "title": job["title"],
                 "status": job["status"]}
        if job.get("interview_at"):
            local = studio.jobstore.interview_local(job["interview_at"], job.get("interview_tz"))
            try:
                at = dt.datetime.fromisoformat(str(local)[:19]).astimezone()
            except ValueError:
                at = None
            if at and now - dt.timedelta(hours=2) <= at <= horizon:
                interviews.append(brief | {
                    "interview_at": job["interview_at"], "interview_tz": job.get("interview_tz"),
                    "local": at.isoformat(timespec="minutes"),
                    "utc": at.astimezone(dt.timezone.utc).isoformat(timespec="minutes")})
        if job.get("followup_date") and job["status"] not in studio.jobstore.TERMINAL:
            day = str(job["followup_date"])[:10]
            if day <= horizon.date().isoformat():
                followups.append(brief | {"date": day, "overdue": day < today})
    interviews.sort(key=lambda e: e["utc"])
    followups.sort(key=lambda e: e["date"])
    out = {"days_ahead": days_ahead, "interviews": interviews, "followups": followups,
           "summary": (f"{len(interviews)} interview(s) and {len(followups)} follow-up(s) in the next "
                       f"{days_ahead} days" + (f", {sum(f['overdue'] for f in followups)} overdue"
                                               if any(f["overdue"] for f in followups) else ""))}
    if ics:
        out["ics"] = studio.jobstore.ics(_ws())
    return out


@tool
def set_job_status(job_id: str, status: str, append_note: str | None = None) -> dict:
    """Move one application to a new status. Confirm with the user first.

    This appends to a permanent history that the funnel is drawn from, and
    there is no undo, so show the user what you intend to change and wait.

    The vocabulary, and it is closed:
      pending                 not sent yet
      applied                 sent, no reply
      interviewing            at least one interview happening
      offer                   an offer is on the table
      accepted / refused      the user's decision on that offer
      rejected                turned down before any interview
      rejected_interviewing   turned down after interviewing
      ghosted                 no reply, before any interview
      ghosted_interviewing    no reply, after interviewing

    The two rejected and two ghosted values are separate on purpose: a
    rejection after interviews says something very different about a CV than
    one before, and the funnel keeps them apart.

    Never infer ghosted from silence. It means the user has given up on a
    thread, which is their call and not yours.

    `append_note` adds a dated line to the notes. Use it to record where the
    change came from, such as the subject line and date of the mail.
    """
    return studio.jobstore.update_job(
        _ws(), job_id, {"status": status, "append_note": append_note})


@tool
def update_job_tracking(job_id: str, interview_at: str | None = None,
                        interview_tz: str | None = None,
                        followup_date: str | None = None,
                        last_contact_at: str | None = None,
                        contact_email: str | None = None,
                        cv_path: str | None = None,
                        letter_path: str | None = None,
                        url: str | None = None,
                        description: str | None = None,
                        location: str | None = None,
                        source: str | None = None,
                        replace_posting: bool = False,
                        title: str | None = None,
                        append_note: str | None = None) -> dict:
    """Record dates, contacts and which documents were sent, without moving it.

    Deliberately separate from set_job_status: these are facts about the
    application, and the status is a judgement about it.

    `cv_path` attaches a document to this application -- the other half of
    tailoring one. Write a CV for a specific job by copying the base named in
    workspace_info: create_cv(name=..., copy_from=<base_cv>), edit the copy,
    then attach it here. The app then shows it in the application's row and
    marks every field that differs from the base.

    Attaching replaces whatever was attached before; it does not delete the
    document that was there. An empty string detaches without deleting
    anything. `letter_path` is the same for a cover letter, which is any
    document under letters/.

    `interview_at` is "YYYY-MM-DDTHH:MM:SS", the wall-clock time the
    invitation gives, not UTC. When the invitation gives it in the employer's
    time zone rather than the user's -- 10:00 in London for someone in Paris --
    write that time as it is and pass `interview_tz` as the IANA name
    ("Europe/London"). The app shows it in both zones and reminds at the right
    moment. Leave `interview_tz` out when the time is already the user's own;
    pass an empty string to clear a zone set before.

    Nothing outside this app knows about that time. No event is created in any
    calendar, so this record is the only thing that will remind the user. If an
    interview moves, update it here; if it is cancelled, pass an empty string
    to clear it, which also writes a note so the change is not silent.

    The app keeps the interviews as rounds (screen, technical, final...), and
    `interview_at` is always the next one not decided yet. Setting it moves
    that round; once every round has an outcome, a new time adds a round. The
    user records the outcome of each round in the app.

    `last_contact_at` is when they last got in touch. Set it for an
    acknowledgement that changes nothing else, so the application stops looking
    abandoned when it is not.

    The posting, once the application exists: `url` is the link to the advert,
    `description` its full text (Markdown: headings and lists come through),
    `location` and `source` (where it was found: "LinkedIn", "Referral"...).
    Save the text whenever you have it: adverts come down, and a tailored CV
    and a letter are written against it. A posting already saved is the
    user's, who may have edited it; replacing it takes `replace_posting=True`,
    and only when they asked.

    `title` corrects the application's title, and only to the title its
    posting gives: CV Studio reads the posting at the application's link and
    refuses any other. The user's own renames are theirs to make.
    """
    data: dict = {}
    if title is not None:
        link = url or read_job(job_id).get("url")
        if not link:
            raise ValueError("This application has no link, so its title cannot be "
                             "checked. Ask the user to rename it in the app.")
        read = studio.posting.read(link)
        if read["title"].casefold() != title.strip().casefold():
            raise ValueError(f"The posting's title is {read['title']!r}, not {title!r}. "
                             f"Only the posting's own title can be set here.")
        data["title"] = read["title"]
    for field, value in (("interview_at", interview_at), ("interview_tz", interview_tz),
                         ("followup_date", followup_date),
                         ("last_contact_at", last_contact_at),
                         ("contact_email", contact_email)):
        if value is not None:
            data[field] = value or None
    for field, value in (("url", url), ("location", location), ("source", source)):
        if value is not None:
            data[field] = value.strip() or None
    if url and not url.strip().lower().startswith(("http://", "https://")):
        raise ValueError("url must start with http:// or https://")
    if description is not None:
        current = read_job(job_id).get("description")
        if current and current.strip() != description.strip() and not replace_posting:
            raise ValueError("A posting is already saved for this application, and the "
                             "user may have edited it. Pass replace_posting=True only if "
                             "they asked for it to be replaced.")
        data["description"] = description.strip() or None
    for field, value in (("cv_path", cv_path), ("letter_path", letter_path)):
        if value is not None:
            data[field] = _document(value, field)
    if not data and not append_note:
        raise ValueError("Nothing to change.")
    if interview_at == "":
        append_note = (append_note or
                       "Interview time cleared, no matching calendar event.")
    data["append_note"] = append_note
    out = studio.jobstore.update_job(_ws(), job_id, data)
    if description is not None and description.strip() and len(description.split()) < THIN_POSTING:
        out["posting_note"] = _thin_note(len(description.split()))
    return out


@tool
def save_person(job_id: str, name: str | None = None, email: str | None = None,
                role: str | None = None, link: str | None = None) -> dict:
    """Record someone the user is talking to about an application: a
    recruiter, a hiring manager, an interviewer, whoever referred them.

    Use it when an email thread or an invitation names them. Someone already
    listed (same email, else same name) is completed, never duplicated; only
    the fields you pass change, and nobody is ever removed here. `role` reads
    best as one of Recruiter, Hiring manager, Interviewer or Referral, which
    the app shows in the user's language. `link` is a profile or page URL.
    """
    if not (name or email):
        raise ValueError("Give at least a name or an email.")
    people = [dict(p) for p in read_job(job_id).get("people") or []]
    match = next((p for p in people if email and (p.get("email") or "").lower() == email.lower()), None) \
        or next((p for p in people if name and (p.get("name") or "").strip().lower() == name.strip().lower()), None)
    fields = {"name": name, "email": email, "role": role, "link": link}
    if match is None:
        match = {"name": "", "email": "", "role": "", "link": "", "last": ""}
        people.append(match)
    for k, v in fields.items():
        if v is not None and v.strip():
            if k == "email" and (match.get("email") or "").lower() == v.strip().lower():
                continue                # the same address, written differently
            match[k] = v.strip()
    job = studio.jobstore.update_job(_ws(), job_id, {"people": people})
    return {"people": job.get("people"), "id": job_id}


@tool
def get_interview_prep(job_id: str) -> dict:
    """The interview prep for an application's next round, as the user sees
    it: the likely questions (each with `src` posting, cv, round or you, the
    posting line or CV line it comes from, the user's notes, and `state`
    work or got from rehearsing), the stories (`req` from the posting, `proof`
    from the CV, empty when nothing backs it) and the questions to ask them.

    `local` true means the app made it from the posting and the CV alone:
    read the posting (read_job) and the CV sent (read_cv on its cv_path),
    then write better ones with save_interview_prep.
    """
    return studio.interview_prep(job_id)


@tool
def save_interview_prep(job_id: str, questions: list[dict],
                        stories: list[dict] | None = None,
                        asks: list[str] | None = None) -> dict:
    """Write the interview prep for an application's next round.

    `questions`: 6 to 10 likely questions, each {"q", "src", "why", "cv"}:
    `src` is "posting" (a requirement turned into a question: put the posting
    line in `why`), "cv" (a claim on the CV they will test: put the CV line in
    `cv`) or "round" (what this kind of round asks). Write them in the
    application's language, as the interviewer would say them.
    `stories`: what the posting asks for, each {"req", "proof", "where"}:
    the CV line that shows it and where it is from, or "" in proof when
    nothing does, which the user sees as a gap to prepare.
    `asks`: 3 to 5 questions for the user to ask the person in this round.

    The user's notes and rehearsal marks stay on any question you ask again,
    and their own questions are kept. Read what is there first with
    get_interview_prep.
    """
    q = [{"q": x.get("q"), "src": x.get("src") or "round", "why": x.get("why") or "",
          "cv": x.get("cv") or "", "note": "", "state": ""} for x in questions if isinstance(x, dict)]
    data = {"questions": q}
    if stories is not None:
        data["stories"] = stories
    if asks is not None:
        data["asks"] = [{"q": a, "keep": True} for a in asks if isinstance(a, str)]
    return studio.save_interview_prep(job_id, data, by=studio.CLIENT_ID or "ai")


@tool
def add_job(company: str, title: str, status: str = "pending",
            url: str | None = None, location: str | None = None,
            source: str | None = None, description: str | None = None,
            contact_email: str | None = None,
            company_website: str | None = None,
            language: str | None = None,
            confirmed_new: bool = False) -> dict:
    """Add an application. Call find_job first.

    Pass `company_website` -- the company's own domain, such as stripe.com,
    never the job board's -- and its logo is fetched and shown on the row. If
    you do not know it, leave it out rather than guess; set_company_logo can
    add it later. A logo that cannot be found never stops the application
    being added: the result's `logo` field says what happened.

    Refuses if anything at that company already exists, and lists what it
    found. Pass confirmed_new=True only once the user has said it really is a
    separate application. There is no delete tool, so a duplicate created here
    is one the user has to clear up by hand.

    Put the full text of the posting in `description`. It costs nothing to
    store and it is what you will write against when they later ask you to
    tailor a CV for this job, by which time the page is usually gone.

    `language` is the language the posting is written in, as a code such as
    "fr". Left out, it is read from the description.

    With a `url`, CV Studio reads the posting itself from the job board
    (Lever, Greenhouse, Ashby, SmartRecruiters, or the job the page describes
    for search engines), and its title, full text and location win over what
    you pass: `title_note` and `posting_note` in the result say what it used.
    """
    if not confirmed_new:
        existing = _candidates(company, title)
        if existing:
            listed = "; ".join(f"{j['title']} ({j['status']})" for j in existing)
            raise ValueError(
                f"{company} already has: {listed}. If this is genuinely a "
                f"different application, ask the user, then call again with "
                f"confirmed_new=True.")
    notes: dict = {}
    if url:
        try:
            read = studio.posting.read(url)
        except Exception as exc:  # the application matters, the reading does not
            notes["posting_note"] = (f"Could not read the posting from its link ({exc}). "
                                     f"The title and text are the ones you passed.")
        else:
            if read["title"].casefold() != (title or "").strip().casefold():
                notes["title_note"] = (f"The posting's own title is {read['title']!r}; "
                                       f"used instead of {title!r}.")
            title = read["title"]
            if read.get("description"):
                description = read["description"]
                notes["posting_note"] = f"Posting text read from {read['via']}."
            location = location or read.get("location")
    job = studio.jobstore.add_job(_ws(), {
        "company": company, "title": title, "status": status, "url": url,
        "location": location, "source": source, "description": description,
        "contact_email": contact_email,
        "language": studio.languages.code_of(language) if language else None,
        # A company already has a logo if any earlier application to it did.
        "logo": studio.stored_logo(company),
    })
    if job.get("logo"):
        job["logo_note"] = f"Reused the logo already saved for {company}."
    elif company_website:
        try:
            saved = studio.fetch_logo(company, company_website)
            studio.jobstore.set_company_logo(_ws(), company, saved["logo"])
            job["logo"] = saved["logo"]
            job["logo_note"] = f"Fetched from {saved['from']}."
        except Exception as exc:  # the application matters, the logo does not
            job["logo_note"] = (f"No logo: {exc}. It shows the company's "
                                f"initials instead.")
    else:
        job["logo_note"] = ("No logo. Call set_company_logo with the company's "
                            "website to add one.")
    words = len((description or "").split())
    if description and words < THIN_POSTING and "posting_note" not in notes:
        notes["posting_note"] = _thin_note(words)
    job.update(notes)
    job["next"] = ("Unless the user asked only to track it: tailor a CV for it "
                   "(create_cv copying the base CV in the posting's language, "
                   "edit_cv_fields, render_cv, ats_check with this job_id, then "
                   "update_job_tracking with cv_path), then create_letter and "
                   "write_letter. Report what you wrote and any gap.")
    return job


# A posting runs to a few hundred words. Far fewer is usually a summary a web
# fetch made of the page, which is no base for a tailored CV.
THIN_POSTING = 150


def _thin_note(words: int) -> str:
    return (f"The posting saved is only {words} words, which looks like a summary rather "
            f"than the posting. Read the real one with read_posting on its link, or ask "
            f"the user to paste it, before tailoring anything to it.")


@tool
def read_posting(url: str) -> dict:
    """Read a job posting from its link, exactly as the company published it.

    CV Studio fetches it on this machine from the job board's own data
    (Lever, Greenhouse, Ashby, SmartRecruiters) or from the job the page
    describes for search engines. Returns {title, company, location,
    description (Markdown), via, url}. `company` is empty when the board does
    not say it.

    Use it instead of a web page fetch that summarises: titles and wording
    matter on a CV written against them. If it raises, ask the user to paste
    the posting; never fill the gap from memory.
    """
    return studio.posting.read(url)


@tool(view=PAGE_VIEW)
def ats_check(path: str, job_id: str | None = None) -> CallToolResult:
    """Read a CV's rendered PDF the way an applicant tracking system does.

    Renders first if the PDF is older than the YAML. Returns the parsing
    problems found (icons that extract as junk characters, a profile shown as
    a bare username, non-standard headings, entries missing or out of order in
    the text layer) and, against the posting of `job_id` or of the application
    this CV is attached to, which of the posting's keywords the CV uses.

    Use it after tailoring. A missing keyword is worth working in only where
    it is true of the user: never add a skill they have not claimed. The
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
    page = _render(path, 1, True) if views and studio.is_cv_yaml(p) else None
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
            blocks.append(({"k": "entry", "name": name, "i": i}, ats._norm(_flat(e, 20000))))
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
                    "Keep it under about 350 words and on one page."}


@tool
def write_letter(path: str, body: str, subject: str | None = None) -> str:
    """Replace a cover letter's body, and its subject line if given.

    `body` is the whole letter from greeting to closing, as Markdown, all of
    which prints: paragraphs separated by blank lines, # to ### headings,
    '- ' and '1. ' lists (nested by indenting), '> ' quotes, '---' rules,
    fenced code, pipe tables, **bold**, *italic*, ~~struck~~, `code` and
    [text](url). Most cover letters need only paragraphs; use the rest where
    it helps the reader. The name, contact details and signature are printed
    from the CV the letter looks like, so leave them out. The header is kept.
    """
    p = studio.safe_path(path)
    if not studio.is_letter(p):
        raise ValueError(f"{path} is not a cover letter. Letters are letters/*.md.")
    payload = {"body": body}
    if subject is not None:
        payload["meta"] = {"subject": subject}
    r = studio.save_letter(p, payload)
    return f"Wrote {path}: {r['words']} words. render_cv it to see the page."


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
            "yet. Ask rather than picking a CV for them."),
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
    }


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
    whether render `rid` of `path` is still its latest. what="fields": the
    text of `block` of `path`, to edit on the page."""
    if what == "fields":
        f = _block_fields(_doc_path(path), block or {})
        return CallToolResult(content=[TextContent(type="text", text="fields")], structured_content=f)
    if what == "page":
        if not _PAGE_ID.fullmatch(page):
            raise ValueError("not a page id")
        f = _views_dir() / "pages" / f"{page}.png"
        if not f.is_file():
            raise ValueError("That page is gone. Render the CV again.")
        return CallToolResult(content=[ImageContent(
            type="image", data=base64.b64encode(f.read_bytes()).decode("ascii"), mime_type="image/png")])
    if what == "pdf":
        return CallToolResult(content=[_pdf_resource(path)])
    if what == "status":
        st = _view_status(path, rid)
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(st))], structured_content=st)
    raise ValueError("what is page, pdf or status")



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


# The skills, offered as prompts too. A client that reads skills has them
# already (the app installs them); one that does not -- ChatGPT, or Claude
# Desktop before the plugin is added -- still lists these, in Claude Desktop
# under the + menu, and picking one hands the model the same procedure.
PROMPT_TITLES = {"cv-studio-apply": "Apply to a job",
                 "cv-studio-inbox": "Catch up from my inbox",
                 "cv-studio-interview-prep": "Prepare an interview"}


def _skill_prompt(skill: dict):
    def prompt(request: Annotated[str, Field(
            description="What you want, in your words: a job link, a company, "
                        "an interview (optional)")] = "") -> str:
        ask = request.strip()
        return skill["body"] + ("\n\n---\n\nWhat the user asked: " + ask if ask else "")
    return Prompt.from_function(
        prompt, name=skill["name"],
        title=PROMPT_TITLES.get(skill["name"])
        or skill["name"].replace("cv-studio-", "").replace("-", " ").capitalize(),
        description=skill["description"])


for _skill in studio.skill_texts():
    mcp.add_prompt(_skill_prompt(_skill))


def main(workspace: str | None = None, client: str | None = None) -> int:
    studio.WORKSPACE = Path(workspace).resolve() if workspace else studio.DEFAULT_WORKSPACE
    studio.IS_MCP = True
    # The app writes the client into the config it installs, so this is known
    # before any handshake. _identify() fills it from clientInfo otherwise.
    if client:
        studio.CLIENT_ID = client
        studio.CLIENT_AGENT = studio.AI_CLIENTS.get(client, {}).get("label")
    studio.bootstrap(studio.WORKSPACE)
    mcp.run(transport="stdio")
    return 0
