"""Interactive job-search views (job_stats, show_application, today, job_view_data)."""

from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path

from mcp.types import CallToolResult, TextContent

import review
import studio
from .applications import _brief, _job, _logo_uri, _record_round
from .runtime import JOBS_VIEW, _one_line, _shows_views, _ws, tool


# --- The job search, as views (static/mcp-jobs.html) --------------------------
#
# The same numbers the app's funnel screen draws, for the chat: job_stats for
# how it is going, show_application for one application. Both return a short
# text for the model, which is what it reasons about, and the view's data in
# the structured content. The view switches period, opens an application and
# changes a status through job_view_data, hidden from the model; a status
# change made there is the user's own, confirmed on the card first.

STAGES = [
    ("applied", "Applied", set(studio.jobstore.NODE_STATUSES["applied_s"])),
    ("replied", "Replied", set(studio.jobstore.NODE_STATUSES["applied_s"]) - {"applied", "ghosted"}),
    ("interviewed", "Interviewed", set(studio.jobstore.NODE_STATUSES["interview_s"])),
    ("offer", "Offer", set(studio.jobstore.NODE_STATUSES["offer_s"])),
    ("accepted", "Accepted", {"accepted"}),
]
OUTCOMES = [
    ("applied", "Waiting for a reply"), ("rejected", "Rejected before an interview"),
    ("ghosted", "No reply, gave up"), ("interviewing", "Interviewing now"),
    ("rejected_interviewing", "Rejected after interviews"),
    ("ghosted_interviewing", "No reply after interviews"), ("offer", "Offer to decide on"),
    ("accepted", "Accepted"), ("refused", "Offer declined"),
]
# What can come next from each status: the moves a card offers. The table is
# the store's (jobs.LIFECYCLE), the same one the app's status menu reads.
NEXT = studio.jobstore.NEXT
STATUS_LABEL = {"pending": "Not sent yet", "applied": "Applied", "interviewing": "Interviewing",
                "offer": "Offer", "accepted": "Accepted", "refused": "Declined",
                "rejected": "Rejected", "ghosted": "No reply",
                "rejected_interviewing": "Rejected after interviews",
                "ghosted_interviewing": "No reply after interviews"}


def _applied_on(job: dict) -> str | None:
    at = studio.jobstore._applied_at(job.get("status_history") or [])
    if at:
        return str(at)[:10]
    return str(job.get("created_at") or "")[:10] or None if job.get("status") != "pending" else None


def _job_row(job: dict) -> dict:
    return {"id": job["id"], "company": _one_line(job.get("company"), 60),
            "title": _one_line(job.get("title"), 80), "status": job.get("status"),
            "applied": _applied_on(job)}


# Where an application stopped, between each stage and the next.
STOPPED_AT = [["applied", "ghosted"], ["rejected"],
              ["interviewing", "rejected_interviewing", "ghosted_interviewing"], ["offer", "refused"]]
DROP_LABEL = {"applied": "waiting", "ghosted": "no reply", "rejected": "rejected",
              "interviewing": "still interviewing", "rejected_interviewing": "rejected",
              "ghosted_interviewing": "no reply", "offer": "deciding", "refused": "declined"}


def _reached(status: str | None) -> int:
    """How far an application got: 0 sent, 1 replied, 2 interviewed, 3 offer, 4 accepted."""
    return max((i for i, (_, _, ss) in enumerate(STAGES) if status in ss), default=0)


def _insight(st: dict, sent: list[dict]) -> list[str]:
    """One or two plain sentences on where the search is losing applications,
    worked out from the numbers rather than written by a model: the view
    opens with them, and Claude is there to say more."""
    t, g = st["totals"], st["stages"]
    n = t["applications"]
    if n < 3:
        return [f"Too early to read much: {n} application{'s' if n != 1 else ''} so far."]
    out = []
    # (rate, index) for each step, counting only what has an outcome there:
    # an application still waiting, or an interview still going, is not lost.
    still = {1: sum(1 for j in sent if j.get("status") == "applied"),
             2: 0, 3: sum(1 for j in sent if j.get("status") == "interviewing")}
    steps = []
    for i in range(1, 4):
        settled = g[i - 1]["count"] - still[i]
        if settled >= 3:
            steps.append((g[i]["count"] / settled, i))
    if steps:
        rate, i = min(steps)
        if i == 1:
            quiet = g[0]["count"] - g[1]["count"]
            out.append(f"Most of what is lost is lost early: {quiet} of {g[0]['count']} applications "
                       "have had no answer. Tailoring each one, and following up, is where to work.")
        elif i == 2:
            no = g[1]["count"] - g[2]["count"]
            out.append(f"Your applications get answers ({round(100 * g[1]['count'] / n)}% replied), but "
                       f"{no} of {g[1]['count']} replies were rejections before an interview: "
                       "the CV is read, and not yet convincing for these roles.")
        else:
            out.append(f"You reach interviews ({g[2]['count']} so far) but "
                       f"{'no offer yet' if not g[3]['count'] else 'few offers'}: interview "
                       "preparation is where to look.")
    good = [r for r in st["sources"] if r["count"] >= 2 and r["source"] != "Not recorded"]
    if len(good) >= 2:
        best = max(good, key=lambda r: (r["replied"] / r["count"], r["count"]))
        avg = sum(r["replied"] for r in good) / sum(r["count"] for r in good)
        if best["replied"] / best["count"] > avg + 0.1:
            out.append(f"{best['source']} works best: {best['replied']} of {best['count']} replied.")
    return out[:2]


def _stats(days: int | None) -> dict:
    import datetime as dt
    jobs = studio.jobstore.list_jobs(_ws())
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat() if days else None
    sent = [j for j in jobs if j.get("status") != "pending" and _applied_on(j)
            and (not since or _applied_on(j) >= since)]
    stages = []
    prev = None
    for key, label, statuses in STAGES:
        members = [j for j in sent if j.get("status") in statuses]
        stages.append({"key": key, "label": label, "count": len(members),
                       "of_previous": round(100 * len(members) / prev) if prev else None,
                       "jobs": [_job_row(j) for j in members][:80]})
        prev = len(members)
    outcomes = []
    for status, label in OUTCOMES:
        members = [j for j in sent if j.get("status") == status]
        if members:
            outcomes.append({"key": status, "label": label, "count": len(members),
                             "jobs": [_job_row(j) for j in members][:80]})
    replies = [d for d in (studio.jobstore._reply_days(j.get("status_history") or []) for j in sent)
               if d is not None]
    # Applications per week, Monday to Sunday, or per month over a year.
    by_month = not days or days > 180
    buckets: dict[str, int] = {}
    for j in sent:
        d = dt.date.fromisoformat(_applied_on(j))
        key = d.strftime("%Y-%m") if by_month else (d - dt.timedelta(days=d.weekday())).isoformat()
        buckets[key] = buckets.get(key, 0) + 1
    series = []
    if sent:
        # From the first application, not the start of the period: a year
        # of empty months before the search began says nothing.
        start = min(dt.date.fromisoformat(_applied_on(j)) for j in sent)
        end = dt.date.today()
        if by_month:
            y, m = start.year, start.month
            while (y, m) <= (end.year, end.month):
                k = f"{y:04d}-{m:02d}"
                series.append({"key": k, "count": buckets.get(k, 0)})
                y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        else:
            d = start - dt.timedelta(days=start.weekday())
            while d <= end:
                series.append({"key": d.isoformat(), "count": buckets.get(d.isoformat(), 0)})
                d += dt.timedelta(days=7)
    sources: dict[str, list] = {}
    for j in sent:
        sources.setdefault(_one_line(j.get("source"), 40) or "Not recorded", []).append(j)
    replied = STAGES[1][2]
    source_rows = sorted(({"source": k, "count": len(v),
                           "replied": sum(1 for j in v if j.get("status") in replied)}
                          for k, v in sources.items()), key=lambda r: -r["count"])[:8]
    n = len(sent)
    rate = lambda a: round(100 * a / n) if n else None  # noqa: E731
    # Who stopped between one stage and the next, and how.
    drops = []
    for i in range(len(STAGES) - 1):
        parts = [{"key": k, "label": DROP_LABEL[k], "count": sum(1 for j in sent if j.get("status") == k),
                  "jobs": [_job_row(j) for j in sent if j.get("status") == k][:80]}
                 for k in STOPPED_AT[i]]
        drops.append([p for p in parts if p["count"]])
    dots = sorted(({**_job_row(j), "reached": _reached(j.get("status"))} for j in sent),
                  key=lambda d: d["applied"] or "")[-400:]
    stats = {
        "stages": stages, "totals": {"applications": n, "replied": stages[1]["count"],
                                     "interviewed": stages[2]["count"], "offers": stages[3]["count"]},
        "sources": source_rows,
    }
    return {
        "view": "job-stats", "days": days, "since": since,
        "insight": _insight(stats, sent), "drops": drops, "dots": dots,
        "totals": {"applications": n, "replied": stages[1]["count"], "reply_rate": rate(stages[1]["count"]),
                   "interviewed": stages[2]["count"], "interview_rate": rate(stages[2]["count"]),
                   "offers": stages[3]["count"], "median_reply_days": studio.jobstore._median(replies),
                   "drafts": sum(1 for j in jobs if j.get("status") == "pending")},
        "stages": stages, "outcomes": outcomes,
        "series": {"by": "month" if by_month else "week", "points": series},
        "sources": source_rows,
    }


def _stats_text(st: dict) -> str:
    """The numbers for the model, without the lists: what it reasons about."""
    t = st["totals"]
    lines = [f"Job search, {('last ' + str(st['days']) + ' days') if st['days'] else 'all time'}: "
             f"{t['applications']} applications sent, {t['drafts']} drafts not sent.",
             "Funnel: " + " → ".join(f"{s['label']} {s['count']}" + (f" ({s['of_previous']}%)" if s["of_previous"] is not None else "")
                                    for s in st["stages"]) + ".",
             f"Median days to a reply: {t['median_reply_days'] if t['median_reply_days'] is not None else 'no replies yet'}.",
             "Where they stand: " + ", ".join(f"{o['label'].lower()} {o['count']}" for o in st["outcomes"]) + ".",
             "By source (sent, replied): " + ", ".join(f"{r['source']} {r['count']}/{r['replied']}" for r in st["sources"]) + "."]
    if st.get("insight"):
        lines.append("The view opens with: " + " ".join(st["insight"]))
    return "\n".join(lines)


def _next_step(job: dict) -> dict | None:
    """The one thing to do about this application now, if any."""
    import datetime as dt
    today = dt.date.today().isoformat()
    st = job.get("status")
    if job.get("followup_date") and job["followup_date"] <= today and st not in studio.jobstore.TERMINAL:
        return {"kind": "followup", "text": f"Follow-up due since {job['followup_date']}"}
    at = studio.jobstore.interview_local(job.get("interview_at"), job.get("interview_tz"))
    age = studio.jobstore._days_since(at)
    if age is not None and -studio.jobstore.INTERVIEW_SOON_DAYS <= age <= 0:
        rnd = next((r for r in job.get("rounds") or [] if not r.get("outcome") and r.get("at")), {})
        try:
            when = dt.datetime.fromisoformat(str(job["interview_at"])[:16])
            said = f"{when.strftime('%a')} {when.day} {when.strftime('%b')} at {when.strftime('%H:%M')}"
        except ValueError:
            said = str(job["interview_at"])[:16].replace("T", " at ")
        what = _one_line(rnd.get("kind"), 40) or "Interview"
        who = _one_line(rnd.get("with"), 60)
        # The parts too, so a view can say the date in the chat's language.
        return {"kind": "interview", "text": f"{what} on {said}" + (f", with {who}" if who else ""),
                "in_days": -age, "what": what, "who": who or None, "at": str(job["interview_at"])[:16]}
    if age is not None and age > 0 and st == "interviewing" and studio.jobstore.waiting_round(job):
        return {"kind": "outcome", "text": "The interview has passed: how did it go?"}
    if st == "applied":
        last = max(filter(None, [job.get("last_contact_at"), _applied_on(job)]), default=None)
        quiet = studio.jobstore._days_since(last)
        if quiet is not None and quiet > studio.jobstore.SILENT_DAYS:
            return {"kind": "silent", "text": f"No reply in {quiet} days: worth a follow-up"}
        return {"kind": "wait", "text": "Waiting for a reply" + (f" ({quiet} days so far)" if quiet else "")}
    if st == "offer":
        return {"kind": "decide", "text": "An offer to decide on"}
    if st == "pending":
        return {"kind": "send", "text": "Not sent yet"}
    return None




def _card(job: dict) -> dict:
    jobs = studio.jobstore.list_jobs(_ws())
    days = studio.jobstore._reply_days(job.get("status_history") or [])
    others = [d for d in (studio.jobstore._reply_days(j.get("status_history") or []) for j in jobs)
              if d is not None]
    faster = None
    if days is not None and len(others) >= 4:
        faster = round(100 * sum(1 for d in others if d > days) / len(others))
    logo = _logo_uri(job)
    return {
        "view": "job-card", "id": job["id"],
        "company": _one_line(job.get("company"), 80), "title": _one_line(job.get("title"), 120),
        "status": job.get("status"), "status_label": STATUS_LABEL.get(job.get("status"), job.get("status")),
        "location": _one_line(job.get("location"), 80) or None, "url": job.get("url") or None,
        "source": _one_line(job.get("source"), 40) or None, "logo": logo,
        "applied": _applied_on(job), "last_contact": str(job.get("last_contact_at") or "")[:10] or None,
        "followup": job.get("followup_date") or None,
        "reply_days": days, "faster_than": faster,
        "next": _next_step(job),
        "track": _track(job),
        "moves": [{"status": s, "label": STATUS_LABEL[s]} for s in NEXT.get(job.get("status"), [])],
        "history": [{"status": h.get("status"), "label": STATUS_LABEL.get(h.get("status"), h.get("status")),
                     "at": str(h.get("at") or "")[:10]} for h in job.get("status_history") or []][-12:],
        "people": [{"name": _one_line(p.get("name"), 60), "role": _one_line(p.get("role"), 60),
                    "email": _one_line(p.get("email"), 120)} for p in job.get("people") or []][:6],
        "rounds": [{"kind": _one_line(r.get("kind"), 40), "at": r.get("at") or "", "with": _one_line(r.get("with"), 80),
                    "outcome": r.get("outcome") or ""} for r in job.get("rounds") or []][:8],
        "cv": job.get("cv_path") or None, "letter": job.get("letter_path") or None,
        "has_posting": bool(job.get("description")),
    }


def _track(job: dict) -> dict:
    """The application's way through the stages, with the day it reached
    each, and where it stopped if it did."""
    hist = job.get("status_history") or []
    first = {}
    applied = None
    for h in hist:
        st, at = h.get("status"), str(h.get("at") or "")[:10]
        if st == "applied" and not applied:
            applied = at
        r = _reached(st)
        if st in ("ghosted", "ghosted_interviewing"):
            continue
        for k in range(1, r + 1):
            first.setdefault(k, at)
    first.setdefault(0, applied or _applied_on(job))
    status = job.get("status")
    reached = _reached(status) if status != "pending" else -1
    stopped = {"rejected": "Rejected", "ghosted": "No reply", "rejected_interviewing": "Rejected",
               "ghosted_interviewing": "No reply", "refused": "Declined"}.get(status)
    return {"steps": [{"label": label, "date": first.get(i)} for i, (_, label, _) in enumerate(STAGES)],
            "reached": reached, "stopped": stopped}


def _card_text(c: dict) -> str:
    bits = [f"{c['company']}, {c['title']} ({c['id']}): {c['status_label']}"]
    if c["applied"]:
        bits.append(f"applied {c['applied']}")
    if c["reply_days"] is not None:
        bits.append(f"replied in {c['reply_days']} days")
    if c["next"]:
        bits.append("next: " + c["next"]["text"])
    return "; ".join(bits) + "."



@tool(view=JOBS_VIEW)
def job_stats(days: int | None = 90) -> CallToolResult:
    """How the job search is going: applications sent, the funnel (applied,
    replied, interviewed, offer, accepted), median days to a reply, where
    each application stands, applications per week and by source.

    `days` is the period, by when each was applied to; None for all time.
    Use it when the user asks how their search is going, then say what the
    numbers mean: where applications stop, what gets replies. In a client
    that shows views the user sees it as a chart and can open the
    applications behind any number.
    """
    st = _stats(days if days and days > 0 else None)
    return CallToolResult(content=[TextContent(type="text", text=_stats_text(st))],
                          structured_content=st if _shows_views() else None)


@tool(view=JOBS_VIEW)
def show_application(job_id: str) -> CallToolResult:
    """Show one application to the user as a card: its status and next step,
    history, contacts, interview rounds and documents. Use it when the user
    wants to see an application; read_job is for reading one yourself.

    From the card the user can move it to its next status themselves, after
    confirming there; you are told when they do.
    """
    c = _card(_job(job_id))
    return CallToolResult(content=[TextContent(type="text", text=_card_text(c))],
                          structured_content=c if _shows_views() else None)


def _today() -> dict:
    """What needs the user now, most pressing first: changes an AI client
    made to what is about to be sent, interviews coming, interviews with no
    outcome, offers to decide on, follow-ups due, applications gone quiet,
    drafts left sitting, and jobs a search found. The tracker's own alert
    rules (jobs.alerts) and the app's Next actions, each with the action that
    fits it."""
    import datetime as dt
    al = studio.jobstore.alerts(_ws())
    jobs = {j["id"]: j for j in studio.jobstore.list_jobs(_ws())}
    items = []

    def add(kind, jid, text, when=None, action=None, order=0, **parts):
        j = jobs.get(jid)
        if not j or any(i["id"] == jid and i["kind"] == kind for i in items):
            return
        items.append({**_job_row(j), "kind": kind, "text": text, "when": when, "action": action,
                      "logo": _logo_uri(j), "order": order, **parts})

    # What an AI client changed in a CV or letter about to be sent, and the
    # user has not looked at: first, as in the app's Next actions.
    waiting = review.pending(_ws())
    for j in jobs.values():
        for path, what in ((j.get("cv_path"), "the CV"), (j.get("letter_path"), "the cover letter")):
            r = waiting.get(path) if path else None
            if r:
                who = r.get("agent") or studio.AI_CLIENTS.get(r.get("by") or "", {}).get("label") or "Claude"
                # After an interview in the next three days, before the rest,
                # as the app ranks it.
                add("review", j["id"], f"{who} changed {what}: look before you send it", None,
                    "Show me the changes", 2.5)
    for a in al["interview_soon"]:
        nx = _next_step(jobs.get(a["id"]) or {}) or {}
        d = nx.get("in_days")
        add("interview", a["id"], nx.get("text") or "Interview coming up",
            "Today" if d == 0 else "Tomorrow" if d == 1 else f"In {d} days" if d is not None else None,
            "Prepare me for it", d if d is not None else 9,
            **{k: nx[k] for k in ("what", "who", "at") if nx.get(k)})
    for a in al["interview_passed"]:
        add("outcome", a["id"], "The interview has passed: how did it go?", None, None, 20)
    for j in jobs.values():
        if j.get("status") == "offer":
            add("offer", j["id"], "An offer to decide on", None, "Help me weigh it", 30)
    for a in al["followup_due"]:
        try:
            late = (dt.date.today() - dt.date.fromisoformat(str(a.get("followup_date"))[:10])).days
        except ValueError:
            late = None
        add("followup", a["id"], (f"A follow-up was due {late} day{'s' if late > 1 else ''} ago" if late
                                  else "A follow-up is due today"),
            "Due", "Draft a follow-up", 40)
    for a in al["silent"]:
        add("silent", a["id"], f"No reply in {a['silent_days']} days: worth a follow-up",
            f"{a['silent_days']} days", "Draft a follow-up", 50 + min(a["silent_days"], 99) / 100)
    # A draft left more than three days; after a month it is not a plan any
    # more (the app's rule).
    for j in jobs.values():
        if j.get("status") != "pending":
            continue
        try:
            days = (dt.date.today() - dt.date.fromisoformat(str(j.get("created_at") or "")[:10])).days
        except ValueError:
            continue
        if 3 < days <= 30:
            add("draft", j["id"], (f"A draft for {days} days: the CV is ready" if j.get("cv_path")
                                   else f"A draft for {days} days, no tailored CV yet"),
                f"{days} days", "Show me what I'm sending" if j.get("cv_path") else
                "Tailor my CV to it" if j.get("description") else None, 60 + days / 100)
    items.sort(key=lambda i: i["order"])
    drafts = sum(1 for j in jobs.values() if j.get("status") == "pending")
    try:
        scans_new = studio.scans_state()["counts"]["new"]
    except Exception:
        scans_new = 0
    counts = {k: sum(1 for i in items if i["kind"] == k)
              for k in ("review", "interview", "outcome", "offer", "followup", "silent", "draft")}
    bits = []
    if counts["interview"]:
        bits.append(f"{counts['interview']} interview{'s' if counts['interview'] > 1 else ''} coming up")
    if counts["outcome"] or counts["offer"]:
        n = counts["outcome"] + counts["offer"]
        bits.append(f"{n} decision{'s' if n > 1 else ''} to record")
    if counts["followup"] or counts["silent"]:
        n = counts["followup"] + counts["silent"]
        bits.append(f"{n} to follow up")
    if counts["review"]:
        bits.append(f"{counts['review']} to review")
    if counts["draft"]:
        bits.append(f"{counts['draft']} draft{'s' if counts['draft'] > 1 else ''} to send")
    head = (", ".join(bits) + ".")[:1].upper() + (", ".join(bits) + ".")[1:] if bits else "Nothing needs you today."
    return {"view": "job-today", "date": dt.date.today().isoformat(), "headline": head,
            "items": items, "drafts": drafts, "scans": scans_new, "counts": counts}


def _today_text(t: dict) -> str:
    lines = ["Today: " + t["headline"]]
    for i in t["items"]:
        lines.append(f"- {i['company']}, {i['title']} ({i['id']}): {i['text']}")
    if t["drafts"]:
        lines.append(f"{t['drafts']} draft application(s) not sent yet.")
    if t.get("scans"):
        lines.append(f"{t['scans']} new job(s) from the user's searches are waiting: job_scans lists them, "
                     "and decide_job_scan records the user's yes or no.")
    return "\n".join(lines)


@tool(view=JOBS_VIEW)
def today() -> CallToolResult:
    """What needs the user today, most pressing first: changes you or another
    client made to a CV or letter about to be sent, interviews coming up (with
    a countdown), interviews with no outcome recorded, offers to decide on,
    follow-ups due, applications that have gone quiet, drafts left sitting,
    and new jobs from their searches. Use it when
    the user asks what to do today, or opens the conversation with their job
    search; then offer to help with the first item. In a client that shows
    views it is a list they can act on: each opens its application."""
    t = _today()
    return CallToolResult(content=[TextContent(type="text", text=_today_text(t))],
                          structured_content=t if _shows_views() else None)


@tool(view=JOBS_VIEW, app_only=True)
def job_view_data(what: str, job_id: str = "", status: str = "", days: int = 0) -> CallToolResult:
    """For the job views only, hidden from the model. what="stats": the
    numbers for `days` (0 for all time). what="card": one application.
    what="status": the user moves `job_id` to `status`, confirmed on the card.
    what="round": how the round waiting went, `status` "passed" or "failed",
    said by the user on the card. what="today": what needs the user today."""
    if what == "stats":
        st = _stats(days or None)
        return CallToolResult(content=[TextContent(type="text", text=_stats_text(st))], structured_content=st)
    if what == "today":
        t = _today()
        return CallToolResult(content=[TextContent(type="text", text=_today_text(t))], structured_content=t)
    if what == "card":
        c = _card(_job(job_id))
        return CallToolResult(content=[TextContent(type="text", text=_card_text(c))], structured_content=c)
    if what == "status":
        if not _shows_views():
            raise ValueError("job_view_data is for CV Studio's views in the conversation, "
                             "and this client does not show them.")
        job = _job(job_id)
        if status not in NEXT.get(job.get("status"), []):
            raise ValueError("That is not a next step for this application.")
        studio.jobstore.update_job(_ws(), job_id, {
            "status": status, "append_note": f"Moved to {STATUS_LABEL[status].lower()} on the card in the chat."})
        c = _card(_job(job_id))
        return CallToolResult(content=[TextContent(type="text", text=_card_text(c))], structured_content=c)
    if what == "round":
        if not _shows_views():
            raise ValueError("job_view_data is for CV Studio's views in the conversation, "
                             "and this client does not show them.")
        _record_round(job_id, status, " (on the card in the chat)")
        c = _card(_job(job_id))
        return CallToolResult(content=[TextContent(type="text", text=_card_text(c))], structured_content=c)
    raise ValueError("what is stats, card, status, round or today")



# The views' own tools (app_only) are hidden from the model by a client that
# shows views. A client that does not would list them to its model like any
# other, and they act as the user: an edit saved as theirs, a status moved as
# if they had clicked it. So such a client is not told about them at all, and
# a call from it to one that changes something is refused (in tool()).
