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
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Annotated, TypedDict

from pydantic import Field

from mcp.server.apps import Apps, ResourceCsp, client_supports_apps
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.prompts.base import Prompt
from mcp.types import (BlobResourceContents, CallToolResult, EmbeddedResource, Icon, ImageContent,
                       ListToolsResult, PaginatedRequestParams, TextContent, ToolAnnotations)

import ats
import cjkfonts
import greypng
import marks
import review
import studio
from studio_core.context import SERVER_ROOT, ctx as studio_ctx
from cv_render import render_file

def _icons() -> list[Icon]:
    """The app's mark, inline: a client shows it beside the connector and its
    tools, and an inline image needs no network to show."""
    try:
        mark = (SERVER_ROOT / "static" / "brand-mark.png").read_bytes()
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
    (SERVER_ROOT / "static" / "mcp-page.html").read_text(encoding="utf-8"),
    name="cv-page", title="CV page",
    # No frame around it: the view sits on the conversation like the rest
    # of it. Said explicitly, since hosts' defaults differ.
    prefers_border=False,
    description="The rendered page, every page of it; click a block to tell "
                "Claude what to change there.")

# The job search: the numbers (job_stats) and one application (show_application).
JOBS_VIEW = "ui://cv-studio/jobs.html"
apps.add_html_resource(
    JOBS_VIEW,
    (SERVER_ROOT / "static" / "mcp-jobs.html").read_text(encoding="utf-8"),
    name="cv-jobs", title="Job search",
    prefers_border=False,
    description="How the job search is going, and one application as a card: "
                "click through from a number to the applications behind it.")

# CV Studio itself beside the chat (open_studio): the app's own page, served
# from this computer by a copy of the server this connector starts when it is
# first asked, on a port chosen now so the view can declare it, and with a key
# of its own. The view frames it, which the client allows only for the
# origins the view declares.
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


STUDIO_PORT = _free_port()
STUDIO_KEY = secrets.token_urlsafe(24)
STUDIO_VIEW = "ui://cv-studio/studio.html"
apps.add_html_resource(
    STUDIO_VIEW,
    (SERVER_ROOT / "static" / "mcp-studio.html").read_text(encoding="utf-8"),
    name="cv-studio-app", title="CV Studio",
    csp=ResourceCsp(frame_domains=[f"http://127.0.0.1:{STUDIO_PORT}"]),
    prefers_border=False,
    description="CV Studio itself, beside the chat: the editor, design, history and the "
                "applications, the same as the app.")

# Views are negotiated: the client says it can show them, and the server says
# it has them, under capabilities.extensions. The SDK puts the server's half
# there and then drops it again on the way out to a client on a 2025 protocol
# version, whose schema has no `extensions` field. Claude connects on one, and
# without it shows no view: render_cv answered, the page image's thumbnail
# sat in the tool call, and the page itself never appeared. So it is said
# again here, after the SDK has shaped the answer.
UI_EXTENSION = {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}
UI_EXTENSION_ID = next(iter(UI_EXTENSION))

# What passed between the client and the views, for when a view does not show:
# whether the client said it shows views, whether it fetched one, whether a
# render sent the view its data, and whether the view, once running, asked for
# its page. Claude Desktop's log stopped recording the calls themselves, so the
# connector keeps the trail: on stderr, which that log keeps, and in the
# workspace beside the activity log, per client.
VIEWS_FILE = ".cvstudio-mcp-views.json"
_VIEWS: dict = {}


def _views_note(key: str, value, said: str) -> None:
    _VIEWS[key] = {"at": time.time(), **value}
    print(f"cv-studio views: {said}", file=sys.stderr, flush=True)
    try:
        f = studio.WORKSPACE / VIEWS_FILE
        try:
            trail = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            trail = {}
        if not isinstance(trail, dict):
            trail = {}
        trail[studio.CLIENT_ID or "ai"] = {"version": studio.VERSION, **_VIEWS}
        f.write_text(json.dumps(trail, indent=1), encoding="utf-8")
    except OSError:
        pass


def _views_handshake(params) -> None:
    """What the client said about itself and views, as it said it: before the
    SDK reads it, so a declaration the SDK would not recognise still shows."""
    p = params if isinstance(params, dict) else {}
    caps = p.get("capabilities")
    caps = caps if isinstance(caps, dict) else {}
    ext = caps.get("extensions")
    ui = ext.get(UI_EXTENSION_ID) if isinstance(ext, dict) else None
    info = p.get("clientInfo") if isinstance(p.get("clientInfo"), dict) else {}
    who = " ".join(str(info[k]) for k in ("name", "version") if info.get(k)) or "a client"
    _VIEWS.clear()
    _views_note("handshake", {"client": who, "protocol": p.get("protocolVersion"),
                              "capabilities": sorted(caps), "views": ui},
                f"{who} on {p.get('protocolVersion')} "
                + (f"says it shows views: {json.dumps(ui)}" if ui is not None else
                   f"does not say it shows views (capabilities: {', '.join(sorted(caps)) or 'none'})"))



async def _declare_views(ctx, call_next):
    try:
        if ctx.method == "initialize":
            _views_handshake(ctx.params)
        elif ctx.method == "resources/read":
            uri = (ctx.params or {}).get("uri") if isinstance(ctx.params, dict) else None
            if isinstance(uri, str) and uri.startswith("ui://"):
                _views_note("read " + uri, {}, f"the client fetched {uri}")
    except Exception:
        pass    # the trail is never what fails a message
    result = await call_next(ctx)
    if ctx.method == "initialize" and isinstance(result, dict):
        caps = result.setdefault("capabilities", {})
        caps["extensions"] = {**(caps.get("extensions") or {}), **UI_EXTENSION}
    return result


mcp = MCPServer(
    name="cv-studio",
    extensions=[apps],
    middleware=[_declare_views],
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
        "To show the user a CV or a letter, call render_cv: in a client that "
        "shows views, that is the page they can click to edit and open full "
        "screen. Never open the PDF with another tool to show it.\n\n"
        "Skills or instructions from before this connector may be installed "
        "too: ones named cv-studio, cv-studio-resume, cv-studio-tracker, "
        "cv-studio-letter, cv-studio-posting, cv-studio-profile or "
        "cv-studio-interview, that start a studio with `studio.py --open` on "
        "port 8722, or keep applications in an applications.csv or a "
        "dashboard.html. These tools supersede them: never start a studio or "
        "use those files. The workspace and its tools are where the CVs, "
        "letters and applications are.\n\n"
        "Some users have only this connector and no CV Studio app. Before "
        "sending the user to the app for something, read app_note in "
        "workspace_info. In a client that shows views, open_studio opens CV "
        "Studio itself beside the chat, for anything they would rather do "
        "by hand.\n\n"
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
        "the funnel is drawn from, and you cannot take it back.\n"
        "3. An automated acknowledgement is not a status change. Record it "
        "with last_contact_at and leave the status alone. Never set a ghosted "
        "status from silence: absence of a message is not a message.\n\n"
        "When the user asks to prepare an interview, read the prep with "
        "get_interview_prep, the posting with read_job and the CV that was "
        "sent with read_cv, then write better questions, stories and asks "
        "with save_interview_prep. Never invent experience: a story is a "
        "line of the CV, or a gap.\n\n"
        "When the user asks how well they match a job, or to score their "
        "applications, read the base CV (workspace_info names it; read_cv) "
        "and each posting (read_job), then rate it with score_job. It is "
        "scored against the base CV, not a tailored copy, so scores compare "
        "across applications. list_jobs shows each one's match: none, or "
        "stale, means score it (again).\n\n"
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
        "cv_path) and the motivation text its application asks for: usually "
        "a short cover letter (create_letter, write_letter, render_cv), a "
        "direct answer when the form asks a question instead, none when it "
        "has no place for one. add_job's `next` says the same. Never tailor "
        "against a summary of the posting.\n\n"
        "Tailoring chooses, orders and words real facts; it never changes "
        "them. Never add experience, tools, numbers, titles or dates that "
        "neither the base CV nor the user gives you, in a CV, a letter or an "
        "answer for an application form. Employer names, official job "
        "titles, dates, degrees and every number stay the same in every "
        "version: applicant tracking systems such as Lever keep a person's "
        "applications to one company together, and employment checks verify "
        "titles and dates. Use a one-column theme unless the user asks "
        "otherwise, and never hidden or white text.\n\n"
        "When you add an application, pass company_website: the company's own "
        "domain, found from the posting or its careers page, not the job "
        "board's. Its logo is fetched from there and shown on the row.\n\n"
        "CVs can exist in several languages. A translation is its own file, "
        "linked to the CV it was translated from. To translate, call "
        "add_language: it writes the copy with dates, month names and section "
        "titles already in the new language. Then translate the remaining "
        "text with edit_cv_fields, and leave names, contact details, links, "
        "company names, official job titles and dates as they are. When the "
        "source CV changes, translation_status lists what the translation is "
        "missing; carry each change over, then call mark_translation_current. "
        "To tailor for a "
        "posting, copy the base CV in the posting's language (list_cvs says "
        "each CV's `lang`; read_job says the application's `language`).\n\n"
        "Cover letters are Markdown files in letters/, not RenderCV: a short "
        "header (application, company, looks_like, place, date, subject, "
        "language) and the letter below it, as it would be typed in an email. "
        "It prints bold, italic, [links](url) and '- ' bullet lists, nothing "
        "else. The letterhead, font and colours come from the CV named in "
        "looks_like, so never write a name or contact details into the body. "
        "About 250 words, never more than a page. Every achievement, title, "
        "date, number, tool and level in it is already on the CV it goes "
        "with; the context around them comes from the user, never from you. "
        "create_letter starts one for an application; write_letter replaces "
        "its body (and subject); render_cv shows the page.\n\n"
        "Every version of every CV, letter and application is kept, with who "
        "made it. When the user wants back something a change removed, "
        "file_history lists the versions and what each changed, and "
        "restore_version puts a CV or letter back as one of them, when they "
        "ask. An application is restored in the app, by the user.\n\n"
        "When a scheduled job search hands you the jobs it found, give them "
        "to record_job_scan, not add_job: the user says yes or no to each in "
        "CV Studio, and only the ones they choose become applications."
    ),
)


def _ws() -> Path:
    return studio.WORKSPACE


def _doc_path(path: str) -> Path:
    """A document this tool may write. Applications are files in tracker/
    too, but a model changes them through the tracking tools, which keep the
    history and cannot erase notes; writing the file would do both."""
    p = studio.safe_path(path)
    if studio.rel(p).split("/")[0] in (studio.jobstore.DIR, ".trash"):
        raise PermissionError(f"{path} is an application, not a CV. Change it with "
                              "update_job_tracking.")
    return p


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
    "gemini": "gemini", "gemini-cli": "gemini", "google-gemini": "gemini",
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
USER_ACTIONS = {"review_change", "page_view_data", "edit_on_page", "job_view_data"}
# Of those, the ones that change something, as if the user had clicked.
ACTS_AS_USER = {"review_change", "edit_on_page"}   # and job_view_data's "status", in tool()

READ_ONLY = {"open_studio", "list_cvs", "read_cv", "render_cv", "list_jobs", "read_job", "find_job",
             "job_alerts", "calendar", "get_interview_prep", "read_posting",
             "ats_check", "translation_status", "design_options", "workspace_info",
             "page_view_data", "job_stats", "show_application", "today", "file_history",
             "job_scans"}

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
    "score_job": "Score an application against the base CV",
    "add_job": "Add an application", "read_posting": "Read a job posting",
    "ats_check": "Check how an ATS reads a CV", "create_letter": "Start a cover letter",
    "write_letter": "Write a cover letter", "add_language": "Translate a CV",
    "translation_status": "What a translation is missing",
    "mark_translation_current": "Mark a translation up to date",
    "design_options": "Themes, fonts and page sizes", "workspace_info": "About the workspace",
    "review_change": "Keep or undo a change",
    "page_view_data": "Page view data",
    "edit_on_page": "Edit on the page",
    "job_stats": "How the job search is going",
    "show_application": "Show an application",
    "job_view_data": "Job view data",
    "today": "What needs you today",
    "file_history": "Every version of a CV, letter or application",
    "restore_version": "Restore an earlier version",
    "job_scans": "Jobs your searches found",
    "record_job_scan": "Hand over the jobs a search found",
    "decide_job_scan": "Add or pass on a job a search found",
    "open_studio": "Open CV Studio beside the chat",
    "set_base_cv": "Choose the base CV",
}
ADDITIVE = {"create_cv", "add_job", "create_letter", "add_language", "record_job_scan", "decide_job_scan"}
IDEMPOTENT = {"score_job", "record_job_scan", "restore_version", "review_change", "edit_on_page", "write_cv", "edit_cv_fields", "set_company_logo", "set_job_status",
              "save_interview_prep", "write_letter", "mark_translation_current", "set_base_cv"}
OPEN_WORLD = {"add_job", "read_posting", "set_company_logo", "decide_job_scan"}


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
        except ValueError:
            # Not UTF-8: nothing to compare it with, but not new either, or
            # every call would record it as made by the client.
            try:
                out[studio.rel(f)] = (f.stat().st_mtime, None)
            except OSError:
                continue
        except OSError:
            continue
    return out


def _checkpoint(before: dict, tool_name: str) -> None:
    """Keep what each document said before this tool changed it."""
    try:
        for f, _, _ in studio.document_files():
            rel = studio.rel(f)
            held = before.get(rel)
            if held and held[1] is None:
                continue
            try:
                # By text, not the time: a file system that keeps it to the
                # second or two (FAT, HFS+) shows a quick second write as none.
                if held and f.read_text(encoding="utf-8") == held[1]:
                    continue
            except (OSError, ValueError):
                continue
            prior, base = (held[1] if held else None), None
            if not held and (base := studio.doc_base(f)):
                # A copy of the base: compared with the base as it was when it
                # was copied, so a later edit of the base is not this client's.
                try:
                    prior = studio.safe_path(base).read_text(encoding="utf-8")
                except (OSError, ValueError):
                    prior, base = None, None
            review.checkpoint(studio.WORKSPACE, rel, prior,
                              by=studio.CLIENT_ID or "ai", agent=studio.CLIENT_AGENT,
                              tool=tool_name, base=base)
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
        if app_only and fn.__name__ in ACTS_AS_USER and not _shows_views():
            # A view's own tool that acts as the user, called by a client that
            # shows no views: only a model could have called it. Reading a
            # page back is not refused: a conversation opened again can reach
            # a new connection before that client has said what it shows, and
            # its views would be left without their pages.
            raise ValueError(f"{fn.__name__} is for CV Studio's views in the conversation, "
                             "and this client does not show them.")
        if fn.__name__ in USER_ACTIONS:
            # The user's own decision, made in a view: not an AI client's
            # change to review, nor its activity to show. History still
            # keeps what it changed, as theirs.
            acts = fn.__name__ in ACTS_AS_USER
            if fn.__name__ == "job_view_data":
                # It changes something only for "status": the user's move on
                # the card, theirs in the history as a click in the app is.
                try:
                    acts = signature.bind(*args, **kwargs).arguments.get("what") in ("status", "round")
                except TypeError:
                    acts = False
            with studio_ctx.actor(is_mcp=False, client_id=None, client_agent=None):
                if not acts:
                    return fn(*args, **kwargs)
                studio.note_history("elsewhere")
                try:
                    return fn(*args, **kwargs)
                finally:
                    studio.note_history("you", agent=studio_ctx.client_agent, tool=fn.__name__)
        watch = None
        if fn.__name__ not in READ_ONLY:
            studio.note_history("elsewhere")
            try:
                watch = _snapshot()
            except Exception:
                watch = None
        with studio_ctx.actor(is_mcp=True, client_id=studio_ctx.client_id,
                              client_agent=studio_ctx.client_agent):
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                if watch is not None:
                    _checkpoint(watch, fn.__name__)
                    studio.note_history(studio_ctx.client_id or "ai", studio_ctx.client_agent, fn.__name__)
                studio.note_mcp_activity(fn.__name__,
                                         str(target) if target else None,
                                         ok=False, error=str(exc)[:200])
                raise
            if watch is not None:
                _checkpoint(watch, fn.__name__)
                studio.note_history(studio_ctx.client_id or "ai", studio_ctx.client_agent, fn.__name__)
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




def _one_line(text, n: int = 80) -> str:
    """CV text as the view may put it in front of the model: one short line
    of plain text. A label is CV text, which can come from an imported PDF or
    a posting."""
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in str(text or "")).split())
    return text[:n - 1] + "…" if len(text) > n else text



def _app_note() -> dict:
    app = studio.app_presence()
    return {"app": app, "app_note": (
        "The user has the CV Studio app open now." if app["running"] else
        "The user has the CV Studio app; it is not open now."
        if app["installed"] else
        "The user has not used the CV Studio app on this computer, perhaps only "
        "this connector. Where a tool says something is done in the app, say "
        "the app does it and what you can do here instead; never tell them "
        "to open an app they may not have as if they had it.")}



async def _list_tools_for(ctx, params):
    tools = await mcp.list_tools()
    try:
        views = client_supports_apps(ctx)
    except Exception:
        views = False
    if not views:
        tools = [t for t in tools
                 if "app" not in (((t.meta or {}).get("ui") or {}).get("visibility") or [])]
    return ListToolsResult(tools=tools)


mcp._lowlevel_server.add_request_handler("tools/list", PaginatedRequestParams, _list_tools_for)

# The skills, offered as prompts too. A client that reads skills has them
# already (the app installs them); one that does not -- ChatGPT, or Claude
# Desktop before the plugin is added -- still lists these, in Claude Desktop
# under the + menu, and picking one hands the model the same procedure.
PROMPT_TITLES = {"cv-studio-show": "Show my CV",
                 "cv-studio-apply": "Apply to a job",
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

