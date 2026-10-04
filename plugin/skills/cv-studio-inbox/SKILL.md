---
name: cv-studio-inbox
description: Bring a CV Studio job tracker up to date from recent email and calendar events. Use when the user asks to catch up, sync, or check their job applications, or asks what needs attention.
---

# Catching the tracker up

CV Studio holds the applications. Your mail and calendar connectors hold what
happened to them. This joins the two.

Everything flows one way. Mail and calendar are **read-only sources**. Never
create an event, never RSVP, never send, draft, label or archive anything. All
you learn goes into CV Studio through its tools.

## The loop

1. **`job_alerts`** first. It tells you what the app already knows is wrong, and
   it is the cheapest way to see whether there is anything to do.
2. **Read mail, scoped.** `newer_than:14d`, and bias toward the companies
   already in the tracker. Do not classify the whole inbox.
3. **Read the calendar, scoped.** Events from the last 7 days and the next 30.
   Two things matter there: interview-shaped events the tracker has never heard
   of, and every application that already has a future `interview_at`, which
   you re-check in step 6.
4. **`find_job`** for each candidate message or event. Never write without it.
5. **Propose, then wait.** List every change as "Company, role: from X to Y,
   because <the subject line>". Write nothing until the user agrees.
6. **Reconcile interviews you already knew about.** For each application with a
   future `interview_at`, find its event. Moved, so update it. Gone, so clear
   it with an empty string and say so. This app is the only thing that will
   remind them, so a stale time is worse than none.
7. **`job_alerts`** again at the end and read out what is left.

## Matching

Company name is the only real key, and it is weak. The mail says "Greenhouse on
behalf of Acme", the record says "Acme Robotics", and a recruiter writes from a
personal address. So:

- Company name in the subject, body or signature, matched loosely.
- Sender domain against `contact_email` when the record has one. Once you learn
  a real address, write it with `update_job_tracking` so the next sweep is easy.
- Role title, when the mail names one.

Two applications to one company is normal, not an error. If `find_job` returns
more than one, ask. If it returns none, ask whether to add rather than assuming
they forgot.

## Reading a message

| What arrived | What to do |
|---|---|
| Automated "we received your application" | `last_contact_at` only. Not a status change. |
| A human reply, no decision yet | `last_contact_at`, and a note saying what they said. |
| Interview invitation | `interviewing`, and `interview_at` if a time is given. |
| Rejection before any interview | `rejected` |
| Rejection after interviewing | `rejected_interviewing`. The distinction is the point of having both. |
| An offer | `offer`. Never `accepted` or `refused`: that is the user's decision, not the employer's. |
| Nothing at all | Nothing. Silence is not a message, and `ghosted` is the user giving up, which is theirs to declare. |

Times are the user's own local time, `YYYY-MM-DDTHH:MM:SS`, never UTC. Convert
before writing.

## Adding one

`add_job` refuses if that company is already there, which is deliberate: there
is no delete tool, so a duplicate is the user's to clear up by hand. Confirm
with them, then pass `confirmed_new=True`.

Put the whole posting in `description` when you have it. It costs nothing and it
is what you will write against when they later ask you to tailor a CV for that
job, by which time the page is usually gone.

## What you cannot do

No deleting, no renaming a company or a role, and no overwriting notes. Those
are not rules you are being asked to keep, they are parameters that do not
exist. `append_note` adds a dated line and leaves everything else alone; use it
to record where a change came from, so the user can check your reasoning later.

The one thing resting on you rather than on the tools is the confirm step. A
status change appends to a permanent history the funnel is drawn from, and the
app has no undo. Show your work and wait.
