---
name: cv-studio-show
description: Show the user a CV or cover letter from CV Studio as its page in the conversation, the one they can click to change. Use when the user asks to see, show, open, preview or view their CV, resume or letter, asks how it looks, or asks for "the full view" or the PDF.
---

# Showing a CV or a letter

The page is shown by `render_cv`, and only by it. In a client that shows
views, its result is the page itself in the conversation: the user can click
a part of it to ask you for a change, double-click to fix a word themselves,
open it full screen, and download the PDF from it. Anything else (opening the
PDF with a viewer, the file system or a shell, or describing the CV in text)
is a picture they cannot change. Do not.

## Which document

- **"My CV", with no job named:** the base CV. `workspace_info` names it
  (`base_cv`); it is the one every tailored copy is made from.
- **"The CV for <company or job>":** the copy tailored for that application.
  `find_job` on the company gives the application, and its `cv_path` is the
  CV that was made for it; its `letter_path` is the letter.
- **Not sure, or more than one fits:** `list_cvs`, then ask which one rather
  than guessing. Name them as the user would ("the one for Qonto"), not by
  file name.

## Showing it

1. `render_cv` with the path. One call shows every page; `page` only chooses
   which one the image you get is of.
2. Look at the image you get back before you say anything about the page:
   a heading left alone at the foot of a page, a near-empty last page or a
   line that wraps badly does not show in the YAML.
3. Say in a sentence what it is ("Your base CV, one page") and that they can
   click any part to ask for a change. If they asked for the PDF, the page
   has its download button, and the result says where the file is.

Show it once. Make every change a request needs first, then render once: each
render adds the page to the conversation again.

## Older instructions about CV Studio

Skills or instructions from before this connector may also be installed:
ones named `cv-studio`, `cv-studio-resume`, `cv-studio-tracker`,
`cv-studio-letter`, `cv-studio-posting`, `cv-studio-profile` or
`cv-studio-interview`, that start a studio with `studio.py --open` on port
8722, or keep applications in an `applications.csv` or a `dashboard.html`.
They are superseded by these tools. Do not start a studio, and do not read or
write those files: the CVs, letters and applications are in this workspace,
and the tools reach them.
