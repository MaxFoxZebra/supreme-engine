---
name: cv-studio-interview-prep
description: Prepare the user for an interview booked in CV Studio. Use when the user asks to prepare an interview, get ready for a round, or practise questions for an application ("prepare my Monzo interview").
---

# Preparing an interview

CV Studio shows the prep on the application and lets the user rehearse it,
one question at a time, out loud. Until you write it, what they see was made
by the app from the posting's words alone: generic, and sometimes loosely
matched. Your job is to replace it with the prep a good coach would write.

## The loop

1. **`find_job`**, then **`get_interview_prep`**. It gives the round coming
   up (its kind, when, and who it is with) and what is there now. `local:
   true` means the app made it; keep any question that has notes: the user
   wrote them.
2. **Read what they will be asked from.** `read_job` for the posting and the
   people. `read_cv` on the application's `cv_path`: that is the CV the
   company has, not the base CV. If you can browse, read the company's own
   site and recent news, briefly.
3. **Write the questions**, 6 to 10, as the interviewer in this round would
   say them, in the application's language:
   - From the posting (`src: "posting"`): its asks turned into questions,
     with the posting's line in `why`.
   - From the CV (`src: "cv"`): the claims they will test, a number above
     all, with the CV line in `cv`.
   - From the round (`src: "round"`): what this kind of round asks. A system
     design round gets a design problem shaped like the company's product; a
     recruiter screen gets motivation, notice and salary.
4. **Write the stories**: every ask in the posting, with the CV line that
   proves it and where it comes from, or `proof: ""` when nothing does. Be
   strict: a skill listed is weaker than something done, and a gap told is
   worth more than a stretch. The user sees gaps in red, to prepare.
5. **Write the asks**: 3 to 5 questions for the user to ask the person in
   this round, specific to what you read, never generic.
6. **`save_interview_prep`**, then tell the user in two lines what you
   changed and which gaps to prepare, and that they can rehearse from the
   application.

## Rules

- Never invent experience. A story is a line of the CV, or a gap.
- Never overwrite the user's notes: the tool keeps them on any question you
  ask again, so ask the ones they already worked on again if they still fit.
- One round at a time: prep is for the next round with no outcome.
