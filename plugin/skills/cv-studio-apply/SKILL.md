---
name: cv-studio-apply
description: Add job postings to CV Studio and prepare the application for each, a tailored CV and a cover letter. Use when the user shares job links or postings, asks to add jobs to CV Studio, or to apply, tailor a CV or write a letter for a job.
---

# From a job link to an application ready to send

A job added to CV Studio is the start of an application, not the end of it.
Unless the user said to only track it, every job you add gets a CV tailored
to it and the motivation text its application asks for (usually a short
cover letter), attached to the application, before you report back.

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
  bookmark. Do not tailor against a summary: recruiters search CV text for
  the posting's exact words, so the exact words are what you tailor to.
- Call `find_job` first. One job at a time, but do not ask between jobs: the
  user gave you the list.
- **Other applications at the same company.** If `find_job` shows the user
  applied there before, `read_cv` the CV sent then. An applicant tracking
  system keeps every application from one person on one record, so the
  recruiter can see both: the headline, the summary and the choice and order
  of bullets may differ, the facts may not.

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
     headline names the role the posting hires for, or the closest honest
     variant of it; the summary is two or three lines on why this person
     fits this posting, built from facts already in the CV.
   - **Experience** is where most of the work goes. Under each role, the
     bullet that proves the posting's main must-have comes first; bullets
     that prove nothing it asks for go before anything is added. A screening
     model checks each requirement against the experience, not only the
     skills list, so every must-have the user really meets should be visible
     in a bullet, in the posting's own term (with the common variant once
     where it reads naturally: "presales (pre-sales)").
   - **Skills**: lead with the ones the posting names that the CV already
     backs. Keywords go inside achievement sentences, never in a block of
     keywords: keyword stuffing marked the worst CVs in recruiter studies.
   - **Older roles** shrink before recent ones do.
3. **What never changes between versions:** employer names, official job
   titles, start and end dates, degrees, certifications, language levels and
   every number. These are what reference and background checks verify. A
   short descriptor after a title is allowed only if it is accurate and the
   same in every version ("Solutions Architect (pre-sales, integrations)");
   the title itself stays as held. A job that has ended shows its end date.
4. **Personal details:** no date of birth, marital status, children or
   photo unless the user asks for them; city and country are enough for an
   address. If the user lives elsewhere than the job, a header line with the
   move as they stated it ("Lyon, moving to Berlin in March") and,
   where it helps, "EU citizen" answer the question before it is asked.
   Language levels on the CEFR scale (A1 to C2) or "native"; never "fluent"
   below C1.
5. `render_cv` and look at the page. One page by default; two only when page
   two holds things the posting asks for, and page one must carry the match
   on its own. Nothing cut off, no heading stranded at a page break.
6. `ats_check(path, job_id)`. Work a missing keyword in only where it is
   true, and into the bullet that proves it, not only the skills list. Use a
   one-column theme: columns, tables and text boxes are what parsers get
   wrong, and recruiters rank cluttered multi-column layouts lowest too.
   Offer a two-column theme (Duo, Sidebar) only when the user asks for one,
   for a CV handed straight to a person.
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
     to something the user really did;
   - one or two achievements from the CV that answer the posting's main
     asks, with the context the CV had no room for (what was hard, what the
     constraint was);
   - a plain close: a move or availability if it matters, as the user stated
     it, and what they would like to happen next.
   Add a fourth only to explain a real gap, briefly and without apology.
   No name or contact details in the body. Every fact in the letter is
   already on the CV it goes with. Polish is cheap now that anyone can
   generate it; what still tells a reader something is the specific,
   checkable detail only this applicant could write.
3. `render_cv` on the letter and look at it. `create_letter` attaches it.

## 4. Report

One line per job: company and exact title, the CV and motivation text
written, and anything to check: a gap the posting asks for, a posting you
could not read, a CV written in another language, the screening questions
the form asks (location, work permit, languages, years of experience) so the
user answers them truthfully and the same way the CV does. Suggest applying
soon (recruiters often read in order of arrival) and naming a referral if
the user has a real contact. Leave the status as it is: applying is the
user's step.

## The honesty line

Tailoring may change what is shown, in what order and in whose words. It
never changes what is true. Before keeping a line of the CV or a sentence of
the letter, check:

1. **Source.** It comes from the base CV, the user's profile, or something
   the user told you. If not, ask; do not write it.
2. **Impression.** A reader would not come away believing something untrue,
   even if each word can be defended: a team result read as theirs alone, a
   prototype read as production, Docker read as Kubernetes, a title they
   never held.
3. **Decisive facts.** A must-have of the posting (a degree, years, a named
   tool, a language level, a certification) is stated exactly. In France
   and the Netherlands, a deliberate falsehood about such a fact can justify
   dismissal or annul the contract once discovered.
4. **Numbers.** Only numbers the user gave. An estimate says so ("~30%,
   team estimate"); never round up past the real value or add precision a
   measurement did not have. With no number, use a scope the user can count
   (customers, integrations, countries, team size) or say what changed.
5. **References.** The user's former manager, LinkedIn and contract would
   describe it the same way.

The strongest true wording is fair: "designed" rather than "worked on" when
the user designed it, "co-led" when they co-led it, the posting's term for
the same fact. A gap stays a gap; say it in your report instead.

## Rules

- The posting's title and text are the company's; never paraphrase them into
  the record.
- Never invent experience, tools, numbers, titles or dates, in the CV, the
  letter or an answer for the application form.
- Never put hidden, white or tiny text in a document, or text meant only for
  software: screening tools detect it, and it is an invented claim too.
- If the company publishes a policy on using AI in applications, follow it
  and tell the user what it says (Anthropic's, for one, asks candidates to
  write their own first draft and use AI only to refine it).
- Nothing is sent: CV Studio has no way to apply for the user.
