---
name: cv-studio-apply
description: Add job postings to CV Studio and prepare the application for each, a tailored CV and a cover letter. Use when the user shares job links or postings, asks to add jobs to CV Studio, or to apply, tailor a CV or write a letter for a job.
---

# From a job link to an application ready to send

A job added to CV Studio is the start of an application, not the end of it.
Unless the user said to only track it, every job you add gets a CV tailored
to it and a cover letter, both attached to the application, before you report
back.

## 1. The posting, exactly

- **`add_job` with the `url`.** CV Studio reads the posting from the job
  board itself (Lever, Greenhouse, Ashby, SmartRecruiters, or the job the page
  describes for search engines): the title, the full text and the location,
  exactly as published. Pass `company` and `company_website` (the company's
  own domain, for the logo). Do not pass a title or a description of your
  own when you have the link.
- **Never save a summary.** A web fetch often returns a summary of the page,
  not the page. If `add_job` says it could not read the posting
  (`posting_note`), try `read_posting` on the same link; if that fails too,
  ask the user to paste the posting or save it with the Save to CV Studio
  bookmark. Do not tailor against a summary.
- Call `find_job` first. One job at a time, but do not ask between jobs: the
  user gave you the list.

## 2. The CV

1. `workspace_info` names the base CV. `list_cvs` gives each CV's `lang`;
   `read_job` gives the application's `language`. Copy the base CV in the
   posting's language: `create_cv(name="<company>-<role>", copy_from=<base>)`.
   If there is none in that language, copy the base and translate it
   (`add_language`), or say you wrote it in the base's language.
2. `read_job` for the posting, `read_cv` for the copy. Tailor with
   `edit_cv_fields`:
   - the headline and summary say this role, in the posting's words where
     they are true;
   - reorder and rephrase bullets so the ones that prove the posting's asks
     come first; cut what does not serve it before adding length;
   - skills: lead with the ones the posting names that the CV already backs.
   Never add experience, tools, numbers or dates the base CV does not have.
   A gap stays a gap; say it in your report instead.
3. `render_cv` and look at the page: one or two pages, nothing cut off.
   `ats_check(path, job_id)` and work in a missing keyword only where it is
   true.
4. `update_job_tracking(job_id, cv_path=<the copy>)`.

## 3. The letter

1. `create_letter(job_id)`: it takes the letterhead from the CV.
2. `write_letter`: in the posting's language, under 250 words, three short
   paragraphs: why this company and role (something specific from the
   posting or the company), the two or three things from the CV that answer
   its main asks, and a plain close. No name or contact details in the body,
   no clichés, nothing the CV does not back.
3. `render_cv` on the letter and look at it. `create_letter` attaches it.

## 4. Report

One line per job: company and exact title, the CV and letter written, and
anything to check (a gap the posting asks for, a posting you could not read,
a CV written in another language). Leave the status as it is: applying is
the user's step.

## Rules

- The posting's title and text are the company's; never paraphrase them into
  the record.
- Never invent experience.
- Nothing is sent: CV Studio has no way to apply for the user.
