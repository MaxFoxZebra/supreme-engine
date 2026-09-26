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
import functools
import inspect
from pathlib import Path

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ImageContent

import studio
from cv_render import render_file

mcp = MCPServer(
    name="cv-studio",
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


def tool(fn):
    """Register a tool, and leave a note in the workspace that it ran.

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
    signature = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args, cvs_ctx: Context = None, **kwargs):
        if cvs_ctx is not None:
            _identify(cvs_ctx)
        try:
            bound = signature.bind(*args, **kwargs)
            target = next((bound.arguments[k] for k in TARGET_KEYS
                           if bound.arguments.get(k)), None)
        except TypeError:
            target = None
        # Record what happened, not merely that it was attempted: a refused
        # call logged like a successful one tells the user the model read a
        # file it was actually blocked from reading.
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            studio.note_mcp_activity(fn.__name__,
                                     str(target) if target else None,
                                     ok=False, error=str(exc)[:200])
            raise
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
    return mcp.tool()(wrapper)


@tool
def list_cvs() -> list[dict]:
    """List every CV in the workspace, with its path and where it lives."""
    return studio.list_documents()


@tool
def read_cv(path: str) -> str:
    """Read a CV's YAML source. `path` is relative to the workspace."""
    return studio.safe_path(path).read_text(encoding="utf-8")


@tool
def write_cv(path: str, content: str) -> str:
    """Overwrite a CV's YAML source with `content`.

    Prefer edit_cv_fields for small changes: this replaces the whole file and
    will drop any comments the user wrote that are not in `content`.
    """
    p = studio.safe_path(path)
    changed = studio.write_doc(p, content, "write_cv")["changed"]
    return (f"Wrote {len(content)} characters to {path}. "
            f"{len(changed)} field(s) changed.")


@tool
def edit_cv_fields(path: str, edits: list[dict]) -> str:
    """Change individual fields, preserving the rest of the file and its comments.

    Each edit is {"path": ["cv", "headline"], "value": "Solutions Engineer"}.
    List positions are integers: ["cv","sections","experience",0,"company"].
    """
    p = studio.safe_path(path)
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


@tool
def render_cv(path: str, page: int = 1) -> list:
    """Render a CV to PDF and return the page as an image to look at.

    Returns the page count, the word count an ATS would extract, the PDF's
    location, and an image of the requested page. Check the image before
    reporting success: page-break damage does not show up in the YAML.
    """
    p = studio.safe_path(path)
    if studio.is_letter(p):
        r = studio.render_letter(p)
        if not r.get("ok"):
            return [f"RENDER FAILED\n\n{r.get('error')}"]
        png = studio.WORKSPACE / r["pngs"][max(1, min(page, r["pages"])) - 1].split("path=")[1].split("&")[0]
        return [f"Rendered {path}\nPages: {r['pages']}\nWords: {r['words']}\nPDF: {r['pdf']}",
                ImageContent(type="image", data=base64.b64encode(png.read_bytes()).decode("ascii"),
                             mime_type="image/png")]
    result = render_file(p, studio.output_dir(p))

    if not result.get("ok"):
        log = (result.get("log") or "render failed")[-2500:]
        hint = studio.friendly(log)
        return [f"RENDER FAILED\n\n{('Likely cause: ' + hint) if hint else ''}\n\n{log}"]

    pages = result["pages"]
    summary = (
        f"Rendered {path}\n"
        f"Pages: {pages}\n"
        f"Words an ATS reads: {result['ats_word_count']}\n"
        f"PDF: {result['pdf']}"
    )
    out_blocks: list = [summary]

    idx = max(1, min(page, pages)) - 1
    if result.get("png_pages"):
        data = Path(result["png_pages"][idx]).read_bytes()
        out_blocks.append(ImageContent(
            type="image",
            data=base64.b64encode(data).decode("ascii"),
            mime_type="image/png",
        ))
    return out_blocks


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
# delete tool, and no tool takes `company`, `title` or `notes`, so a model
# cannot destroy a record, rename the row the user finds things by, or paint
# over notes they typed. Those cannot be got wrong by a model misreading its
# instructions, because the parameters do not exist. Everything else, above
# all the status change itself, rests on the rules in the server instructions.
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
    """
    data: dict = {}
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
    return studio.jobstore.update_job(_ws(), job_id, data)


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
    """
    if not confirmed_new:
        existing = _candidates(company, title)
        if existing:
            listed = "; ".join(f"{j['title']} ({j['status']})" for j in existing)
            raise ValueError(
                f"{company} already has: {listed}. If this is genuinely a "
                f"different application, ask the user, then call again with "
                f"confirmed_new=True.")
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
    return job


@tool
def ats_check(path: str, job_id: str | None = None) -> dict:
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
    """
    r = studio.ats_report(studio.safe_path(path), job_id)
    if not r.get("ok"):
        raise ValueError(r.get("error") or "The check could not run.")
    kw = r.get("keywords")
    return {
        "pages": r["pages"], "words": r["words"],
        "problems": [{"title": c["title"], "detail": c["detail"]}
                     for c in r["checks"] if c["level"] != "ok"],
        "against": r.get("against"),
        "keywords": None if not kw else {
            "used": f"{len(kw['found'])} of {kw['total']}",
            "found": [t["term"] for t in kw["found"]],
            "missing": [t["term"] for t in kw["missing"]]},
    }


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

    `body` is the whole letter from greeting to closing, as Markdown:
    paragraphs separated by blank lines, **bold**, *italic*, [text](url) and
    '- ' bullets. The name, contact details and signature are printed from the
    CV the letter looks like, so leave them out. The header is kept.
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
                   "rows in applications.db beside them, which export to JSON "
                   "and CSV so nothing is locked in.",
    }


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
