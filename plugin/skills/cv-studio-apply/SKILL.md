---
name: cv-studio-apply
description: Add job postings to CV Studio and prepare the application for each, a tailored CV and the motivation text it asks for, usually a short cover letter. Use when the user shares job links or postings, asks to add jobs to CV Studio, or to apply, tailor a CV or write a letter for a job.
---

# From a job link to an application ready to send

A job added to CV Studio is the start of an application, not the end of it.
Unless the user said to only track it, every job you add gets a CV tailored
to it and the motivation text its application asks for (usually a short
cover letter, attached; an answer for a form field goes in your report),
before you report back.

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
  bookmark. Do not tailor against a summary: recruiters can search CV text
  literally, so the posting's own words are what you tailor to.
- Call `find_job` first. One job at a time, but do not ask between jobs: the
  user gave you the list.
- **Other applications at the same company.** If `find_job` shows the user
  applied there before, `read_cv` the CV sent then. Some applicant tracking
  systems (Lever, for one) keep every application from one person on one
  record, so assume the recruiter can compare them: the headline, the
  summary and the choice and order of bullets may differ, the facts may not.

## 2. The CV

1. `workspace_info` names the base CV. `list_cvs` gives each CV's `lang`;
   `read_job` gives the application's `language`. Copy the base CV in the
   posting's language: `create_cv(name="<company>-<role>", copy_from=<base>)`.
   If there is none in that language, copy the base and translate it
   (`add_language`), or say you wrote it in the base's language.
2. `read_job` for the posting, `read_cv` for the copy. Pick out the
   posting's must-haves (stated as required, or repeated) from its
   nice-to-haves. Then tailor with `edit_cv_fields`. Tailoring is choosing,
   ordering and wording real facts, nothing else:
   - **Headline and summary** carry the match, because the first look at a
     CV is short and goes to the top of page one and to the job titles. The
     headline names the role the posting hires for when the user's
     experience is that role, otherwise the closest honest variant ("Data
     engineer, analytics platforms" for an "Analytics Engineer" posting).
     It is a target, not a title held: never a seniority or speciality the
     CV does not show ("Senior", "Lead", "Head of"). The summary is two or
     three lines on why this person fits this posting, from facts already
     in the CV.
   - **Experience** is where most of the work goes. Under each role, the
     bullet that proves the posting's main must-have comes first; bullets
     that prove nothing it asks for are cut before anything is added. Every
     must-have the user really meets shows in a bullet, in the posting's own
     term (with the common variant once where it reads naturally:
     "presales (pre-sales)"): some screening models (Workday HiredScore
     since August 2026) check each requirement against the experience, not
     only the skills list.
   - **Skills**: lead with the ones the posting names that the CV already
     backs. Keywords go inside achievement sentences, never in a block:
     keyword stuffing marked the worst CVs in a recruiter eye-tracking
     study.
   - **Older roles** shrink before recent ones do.
3. **What never changes between versions:** employer names, official job
   titles, start and end dates, degrees, certifications, language levels and
   every number. Employment checks verify titles and dates, and versions can
   be compared. A short descriptor after a title is allowed only if it
   describes most of the actual work, adds no seniority or scope the title
   lacks, and is the same in every version ("Account Manager (enterprise
   accounts, DACH)"). A job that has ended shows its end date.
4. **Personal details:** no date of birth, marital status, children or
   photo unless the user asks for them; city and country are enough for an
   address. If the user is moving for the job, a header line with the move
   as they stated it ("Lyon, moving to Berlin in March"). Work authorisation
   ("EU citizen") only as the user has stated it, never inferred from a
   name, a language or an address. Language levels on the CEFR scale (A1 to
   C2) or "native", one per language, as the user gives it. If the CV says a
   word instead ("fluent", "professional"), keep the word and ask for the
   level in your report; never convert it yourself, and never "fluent"
   below C1.
5. `render_cv` and look at the page. One page by default; two when the user
   has about ten years or more of relevant experience and page two holds
   things the posting asks for; never more than two. Page one carries the
   match on its own. Nothing cut off, no heading stranded at a page break.
6. `ats_check(path, job_id)` and work in a missing keyword only where it is
   true. Use a one-column theme: columns are hard for parsers, tables and
   text boxes are commonly reported to break parsing, and cluttered
   multi-column layouts ranked lowest with human readers too. Use a
   two-column theme (Duo, Sidebar) only when the user asks for one, for a
   CV handed straight to a person; if the base CV uses one, switch the copy
   to a one-column theme and say so in your report.
7. Spelling and grammar in the CV's language, accents and agreement
   included: a few errors measurably cut interview chances. For a language
   the user writes less well, suggest a native speaker reads it.
8. `update_job_tracking(job_id, cv_path=<the copy>)`.

## 3. The motivation text

Write what the application asks for, and say which in your report:

- **The form requires or offers a letter, or you cannot see the form:** a
  cover letter, below.
- **The form asks a question instead** ("Why us?", "What makes you a fit?"):
  answer that question in three to five sentences, in the report, for the
  user to paste. Do not paste a letter into it.
- **The form has nowhere for one:** no letter. If the user has a real
  contact at the company, offer a short note to that person instead.

The letter:

1. `create_letter(job_id)`: it takes the letterhead from the CV.
2. `write_letter`, in the posting's language: about 250 words (half a page),
   never more than one page. Three short paragraphs:
   - why this company and this role, with something that could not be sent
     to another company: a problem the posting or the product implies, tied
     to something the user really did. What you say about the company comes
     from the posting, its own site or a page you read in this session,
     never from memory; an inferred problem is the user's reading ("your
     posting suggests..."), not a fact about them;
   - one or two achievements from the CV that answer the posting's main
     asks, with the context the CV had no room for (what was hard, what the
     constraint was);
   - a plain close: a move or a start date if it matters, as the user
     stated it, and what they would like to happen next.
   Add a fourth only to explain a real gap, briefly and without apology.
   No name or contact details in the body. Every achievement, title, date,
   number, tool and level in the letter is already on the CV it goes with;
   the context around them comes from the user, never from you, and if you
   do not have it, leave it out. Polish is cheap now that anyone can
   generate it; what still tells a reader something is the specific,
   checkable detail only this applicant could write.
3. `render_cv` on the letter and look at it. `create_letter` attaches it.

## 4. Report

A short entry per job: company and exact title, the CV and motivation text
written (with any form answer, ready to paste), and anything to check: a gap
the posting asks for, a posting you could not read, a CV written in another
language, a missing number or language level to ask for, and any screening
questions you could see on the form (location, work permit, languages,
years of experience). When you could not see the form, remind the user that
such questions are usual and that the answers must match the CV. Suggest
applying soon (recruiters often read in order of arrival) and naming a
referral if the user has a real contact. Leave the status as it is:
applying is the user's step.

## The honesty line

Tailoring may change what is shown, in what order and in whose words. It
never changes what is true.

**Fair, use it freely:** the strongest true verb ("designed" rather than
"worked on" when the user designed it, "co-led" when they co-led it); the
posting's term for the same fact; the most favourable of several real
numbers, labelled for what it measures; a judgement the facts beside it bear
out ("large-scale" next to the volume); the user's own part in a team
result, stated as theirs; leaving out what is irrelevant, never what would
change how a reader sees a fact you do show (that it was a prototype, a team
effort, or has ended); enthusiasm in the letter.

**Check each line:**

1. **Source.** It comes from the base CV, another of the user's CVs, or
   something the user told you in this conversation. If not, leave it out
   and put the question in your report; never fill it in yourself.
2. **Impression.** A reader would not come away believing something untrue,
   even if each word can be defended: a team result read as theirs alone, a
   prototype read as production, Docker read as Kubernetes, a title they
   never held.
3. **Decisive facts.** Where a line touches a must-have of the posting (a
   degree, years, a named tool, a language level, a certification), it
   states the user's real value: their real degree, their real level, the
   tool they used under its own name. Years count only the roles that were
   that work. If the user falls short, the line shows what they have and
   the gap goes in your report. In France and the Netherlands, for two, a
   deliberate falsehood on such a fact lets an employer dismiss or annul
   the contract once it comes out.
4. **Numbers.** Only numbers the user gave, or plain arithmetic on them
   (from 40 to 10 minutes is "75% faster"). Say what each measures and
   whose it was ("in the pilot", "team figure"). An estimate says so
   ("~30%, team estimate"). "~N" is the nearest round figure, "over N" only
   when the real value is above N; never round into the next bracket or
   add precision a measurement did not have. With no number, use a scope
   the CV gives (customers, integrations, countries, team size) or say what
   changed, and list the missing number as a question in your report.
5. **References.** The user's former manager, LinkedIn and contract would
   agree on the checkable facts: title, dates, scope, and whether the user
   led or contributed. The wording may be stronger than theirs; the facts
   may not differ.

If the user asks for a line that fails these checks, say why in one
sentence, offer the strongest true version, and ask for the fact that would
make the stronger one true. Do not write the false version.

## Rules

- The posting's title and text are the company's; never paraphrase them into
  the record.
- Never write hidden, white or tiny text, or text meant only for software:
  some screening firms and tools now detect it, and a hidden skill list is
  an invented claim too.
- If the company publishes a policy on using AI in applications, follow it
  and tell the user what it says (Anthropic's, for one, asks candidates to
  write their own first draft and use AI only to refine it). If a form asks
  whether AI helped, the answer you draft is the truth.
- Nothing is sent: CV Studio has no way to apply for the user.
