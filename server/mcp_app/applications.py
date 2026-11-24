"""Application tracker, calendar, people, interview prep, scoring, and scan MCP tools."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Annotated, TypedDict

from pydantic import Field

import studio
from .runtime import _doc_path, _ws, tool


class Criterion(TypedDict):
    key: Annotated[str, Field(description="requirements, experience, skills, domain or practical")]
    rating: Annotated[int, Field(ge=0, le=5)]
    why: Annotated[str, Field(description="One sentence naming the evidence")]


class ScanMatch(TypedDict, total=False):
    criteria: list[Criterion]
    summary: str
    strengths: list[str]
    gaps: list[str]


class ScanJob(TypedDict, total=False):
    company: str
    role: str
    link: Annotated[str, Field(description="The posting's own permanent link")]
    location: str
    why: Annotated[str, Field(description="One line on why it fits the user")]
    posted: Annotated[str, Field(description="YYYY-MM-DD, or as the posting says it")]
    scan: Annotated[str, Field(description="The search that found it, when not the same for all")]
    notes: str
    description: Annotated[str, Field(description="The posting's text, when the link may not last")]
    details: Annotated[dict[str, str | bool], Field(
        description='Anything else for the card, a column each: {"Sector": "Cloud & data", "Top pick": true}')]
    match: Annotated[ScanMatch, Field(
        description="How the base CV matches it, rated as for score_job")]


@tool
def record_job_scan(jobs: list[ScanJob], scan: str | None = None, found: str | None = None) -> str:
    """Hand CV Studio the jobs a scheduled job search found. The user goes
    through them in the app, one card at a time: right to add one to their
    applications, left to pass. Or they tell you, and decide_job_scan records
    it.

    Pass every job the search found, each with its permanent posting link,
    company, role and one line on why it fits. `scan` names the search
    ("Media, AI & cloud"), `found` the day it ran (YYYY-MM-DD; today when left
    out). A job already handed over, already decided or already an
    application is left out, so handing over yesterday's again is harmless.

    Score each against the base CV in `match`, exactly as score_job asks
    (read the base CV first): the user sees the score out of 10 on its card,
    it comes along when the job becomes an application, and the result lists
    each score with the job's key.

    Do not add these jobs yourself: which become applications is the user's
    choice, made in the app or told to you (decide_job_scan). The one
    exception is the user's own standing instruction, such as their
    scheduled task saying to add the jobs scoring 8 or more: then call
    decide_job_scan "yes" for exactly the keys listed at or above it.
    """
    if not jobs:
        raise ValueError("No jobs given.")
    r = studio.scan_record([dict(j) for j in jobs], scan, found, by=studio.CLIENT_ID or "ai")
    out = (f"{r['added']} new job(s) waiting for the user in CV Studio; {r['already']} were "
           f"already there and {r['tracked']} are already applications. Saved in {r['file']}.")
    if r["scored"]:
        out += " Scores: " + "; ".join(
            f"{x['company']}, {x['role']}: {x['score']:.1f} (key {x['key']})"
            for x in sorted(r["scored"], key=lambda x: -x["score"])) + "."
    if not r["written"]:
        out += (" The spreadsheet is open in another program, so they are queued and written "
                "as soon as it is closed; the app shows them already.")
    return out


@tool
def job_scans(limit: int = 50) -> str:
    """What the user's scheduled job searches found that they have not said
    yes or no to yet, newest first, and how many they added or passed on.

    record_job_scan already leaves out what was handed over before; read this
    when the user asks what is waiting, or to tell them how a search is
    doing.
    """
    st = studio.scans_state()
    c = st["counts"]
    lines = [f"Scans folder: {st['folder']}. Waiting: {c['new']}; added to applications: "
             f"{c['added']}; passed on: {c['skipped']}."]
    lines += [f"Problem: {x}" for x in st["problems"]]
    for r in st["new"][:max(1, min(limit, 500))]:
        bits = [r["found"], r["scan"], f"{r['company']}, {r['role']}", r["location"], r["link"]]
        lines.append("- " + " · ".join(b for b in bits if b) + (f". Why: {r['why']}" if r["why"] else "")
                     + f" (key {r['key']})")
    if not st["new"]:
        lines.append("Nothing is waiting.")
    return "\n".join(lines)


@tool
def decide_job_scan(key: str, decision: str) -> str:
    """Yes or no, for the user, to a job their scheduled search found: the
    same as their swipe in the app. "yes" makes it an application, with the
    posting's own title and text read from its link and the search's reason
    in its notes; "no" passes on it. `key` is the one job_scans lists.

    Only when the user said which, about that job, or their own standing
    instruction says which (such as "add the ones scoring 8 or more" in their
    scheduled task): which jobs they apply to is their choice, not yours. A
    yes is the start of applying, as add_job is: its `next` says what
    follows. A score the scan gave the job comes along to the application.
    """
    if decision == "yes":
        r = studio.scan_add(key)
        job = r.get("job") or {}
        if r.get("already"):
            return f"Already an application: {job.get('company')}, {job.get('title')} ({job.get('id')})."
        said = {"read": "with the posting read from its link",
                "unread": "but its link could not be read: ask the user to paste the posting",
                "none": "with no link to read the posting from"}[r.get("posting") or "none"]
        return (f"Added as an application, {said}: {job.get('company')}, {job.get('title')} "
                f"({job.get('id')}). Next, unless the user only wanted it tracked: a CV tailored "
                f"to it and the letter it asks for, as after add_job.")
    if decision == "no":
        studio.scan_decide([key], studio.scans.SKIPPED)
        return "Passed on. It stays in the scans folder, marked skipped."
    raise ValueError('decision is "yes" or "no".')




def _logo_uri(job: dict) -> str | None:
    """A company's stored logo, inline, when it is small enough to carry."""
    if not job.get("logo"):
        return None
    f = studio.logo_dir() / Path(str(job["logo"])).name
    if f.is_file() and f.stat().st_size < 80_000 and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif"):
        mime = {".svg": "image/svg+xml", ".jpg": "image/jpeg"}.get(f.suffix.lower(), "image/" + f.suffix.lower()[1:])
        return f"data:{mime};base64," + base64.b64encode(f.read_bytes()).decode("ascii")
    return None



def _job(job_id: str) -> dict:
    for job in studio.jobstore.list_jobs(_ws()):
        if job["id"] == job_id:
            return job
    raise ValueError("No such job.")




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
    if not (studio.is_cv_yaml(target) or studio.is_letter(target)):
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
    out = {k: job.get(k) for k in keep if job.get(k) is not None}
    # The score alone, and whether it is out of date: enough to tell which
    # ones to score (again) without the reasons for forty of them.
    m = job.get("match")
    out["match"] = {"score": m["score"], "stale": bool(m.get("stale"))} if m else None
    return out


def _with_stale(jobs: list[dict]) -> list[dict]:
    """Each match marked with why it is out of date, as the app shows it."""
    base = studio._base_sig() if any(j.get("match") for j in jobs) else (None, None)
    for j in jobs:
        if j.get("match"):
            j["match"]["stale"] = studio.match_stale(j["match"], j, base)
    return jobs


@tool
def list_jobs(status: str | None = None, query: str | None = None) -> list[dict]:
    """The user's job applications. Read this before changing anything.

    `status` filters exactly. `query` matches the title, company or notes.
    Returns a trimmed view: call read_job for the full posting text.
    `match` is the score score_job gave it out of 10, null when it has none,
    with `stale` true when the base CV or the posting changed since.
    """
    return [_brief(j) for j in
            _with_stale(studio.jobstore.list_jobs(_ws(), status=status, q=query))]


@tool
def read_job(job_id: str) -> dict:
    """One application in full, including the posting text and its history.

    The posting stored in `description` is what to write against when the user
    asks you to tailor a CV for this job. `match` is the last score_job, with
    `stale` saying what changed since ("cv", "posting").
    """
    for job in studio.jobstore.list_jobs(_ws()):
        if job["id"] == job_id:
            return _with_stale([job])[0]
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

    The result has `next_statuses`, the usual moves from where it now is,
    and a `note` when the move made was not one of them: read it.
    """
    if status not in studio.jobstore.STATUSES:
        # The store takes an empty one as no status, which is Draft.
        raise ValueError(f"{status!r} is not a status. One of: {', '.join(studio.jobstore.STATUSES)}.")
    found = studio.jobstore._find(_ws(), job_id)
    was = found[1].get("status") if found else None
    row = studio.jobstore.update_job(
        _ws(), job_id, {"status": status, "append_note": append_note})
    out = dict(row, next_statuses=list(studio.jobstore.NEXT.get(row.get("status"), [])))
    note = studio.jobstore.unexpected_move(was, status)
    if note:
        out["note"] = note
    return out


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
                        append_note: str | None = None,
                        round_outcome: str | None = None) -> dict:
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
    that round; once every round has an outcome, a new time adds a round.
    `round_outcome` is how that round went, "passed" or "failed", once the
    user tells you (never from silence). Recorded first, so a time given with
    it is the next round's.

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
    if round_outcome is not None:
        decided = _record_round(job_id, round_outcome, "")
        if not data and not append_note:
            return decided
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


def _record_round(job_id: str, outcome: str, where: str) -> dict:
    """How the round now waiting went: the first one with a time and no
    outcome, else the first with no outcome."""
    if outcome not in ("passed", "failed"):
        raise ValueError('A round is "passed" or "failed".')
    job = _job(job_id)
    rounds = [dict(r) for r in job.get("rounds") or []]
    rnd = (next((r for r in rounds if not r.get("outcome") and r.get("at")), None)
           or next((r for r in rounds if not r.get("outcome")), None))
    if rnd is None:
        raise ValueError("No interview round of this application is waiting for an outcome.")
    rnd["outcome"] = outcome
    name = rnd.get("kind") or "The interview"
    return studio.jobstore.update_job(_ws(), job_id, {
        "rounds": rounds,
        "append_note": f"{name}: {'passed' if outcome == 'passed' else 'did not pass'}{where}."})


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
def score_job(job_id: str, criteria: list[Criterion], summary: str,
              strengths: list[str] | None = None, gaps: list[str] | None = None) -> dict:
    """Score how well the user's base CV matches an application's posting.

    Read both first: the base CV (workspace_info names it, read_cv reads it)
    and the posting (read_job). Score the base CV, never a tailored copy, so
    the scores compare across applications.

    `criteria`: all five, each {"key", "rating", "why"}, rating a whole
    number 0 to 5 and `why` one sentence naming the evidence:
      requirements (35%): the core responsibilities and must-haves; credit
        transferable and equivalent work on the base CV.
      experience (25%): overall seniority and total years of relevant or
        adjacent work (within a year or two of the ask, or equivalent scope
        across related roles, is 4 or 5).
      skills (20%): the core methods, craft and tools; credit transferable
        skills and never penalise here for industry-specific tools that
        belong under domain.
      domain (10%): the industry, product type and kind of customer;
        adjacent B2B, SaaS or technical sectors are 3 or 4, not 1 or 2.
      practical (10%): location or remote and languages; 5 whenever
        compatible (do not deduct for unstated contract type or work
        authorisation).
    5 is a strong fit (meets the core asks, even if minor nice-to-haves are
    not written verbatim on the base CV), 4 solid fit with minor tailorable
    gaps, 3 partial fit, 2 stretch, 1 barely, 0 not at all. Never
    double-count the same gap across two criteria, and judge like a
    pragmatic hiring manager reading a general base CV before tailoring.

    CV Studio works out the score out of 10 (one decimal) from the ratings
    and their weights: top from 9, strong from 7, good from 5, fair from 3. `summary`: one or two plain sentences, in the posting's
    language, on the fit. `strengths` and `gaps`: up to six short lines
    each, the gaps what the user could address in a tailored CV or a letter.

    Scoring again replaces the last score. The user sees it on the
    application list and can sort and filter by it.
    """
    data = {"criteria": [dict(c) for c in criteria], "summary": summary,
            "strengths": strengths or [], "gaps": gaps or []}
    m = studio.save_match(job_id, data, by=studio.CLIENT_ID or "ai")
    return {**m, "band": studio.jobstore.match_band(m["score"])}


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
    job["next"] = ("Score it against the base CV with score_job once you have "
                   "read both. Unless the user asked only to track it: tailor a CV for it "
                   "(create_cv copying the base CV in the posting's language, "
                   "edit_cv_fields, render_cv, ats_check with this job_id, then "
                   "update_job_tracking with cv_path), then the motivation text "
                   "its form asks for: create_letter and write_letter, a short "
                   "answer when the form asks a question instead, or none when "
                   "it has no place for one. Report what you wrote, any gap, "
                   "and any screening questions you could see on the form.")
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


