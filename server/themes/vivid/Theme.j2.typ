// Vivid, after Awesome-CV: grey and near-black type, one accent.
#let cvx-acc = {{ cvstudio_readable(design.colors.section_titles) }}
#let cvx-grey = rgb(93, 93, 93)
#let cvx-org(b) = text(weight: 700, size: 1.06em, fill: rgb(65, 65, 65), cvx-trim(b))
#let cvx-role(b) = text(size: 0.8em, tracking: 0.05em, fill: cvx-grey, upper(cvx-trim(b)))
#let cvx-place(b) = text(size: 0.95em, style: "italic", fill: cvx-acc, cvx-trim(b))
#let cvx-when(b) = text(size: 0.85em, style: "italic", fill: cvx-grey, cvx-trim(b))
// Skills and the like: the label bold and right-aligned in a column of its
// own, the details grey beside it (OneLineEntry indents their next lines).
#let cvx-label(b) = [#box(width: 2.7cm, align(end, text(weight: 700, fill: rgb(65, 65, 65), cvx-trim(b))))#h(0.4cm)]
// The first three letters in the accent, the rest dark, then a grey rule to
// the margin: a shape, so the title reads as one word.
#let cvx-title(first, rest) = [#first#text(fill: rgb(51, 51, 51))[#rest]#h(0.25cm)#box(width: 1fr, baseline: -0.32em, line(length: 100%, stroke: 0.9pt + cvx-grey))]
