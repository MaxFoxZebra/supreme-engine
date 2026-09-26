// Timeline: jobs and schooling hang from a line down the left, a dot for
// each; section titles led by a small accent square.
{% set acc = design.colors.section_titles.as_rgb() %}
#let cvstudio-acc = {{ acc }}
#let cvstudio-title(t) = {
  box(width: 7pt, height: 7pt, radius: 1.5pt, fill: cvstudio-acc, baseline: -0.05em)
  h(7pt)
  set text(weight: 700)
  t
  h(8pt)
  box(width: 1fr, height: 0.6pt, fill: cvstudio-acc.lighten(70%), baseline: -0.3em)
}
{% set cvs_side = "right" if locale.is_rtl else "left" %}
// The line runs down the side a line starts on: the left, or the right in
// a right-to-left language.
#let cvstudio-stop(body) = block(width: 100%, above: 0pt, below: 0pt, breakable: true,
  inset: ({{ cvs_side }}: 15pt, bottom: 9pt),
  stroke: ({{ cvs_side }}: 1.2pt + cvstudio-acc.lighten(65%)), {
    place(top + {{ cvs_side }}, dx: {{ "" if locale.is_rtl else "-" }}(15pt + 4.6pt), dy: 1.5pt, circle(radius: 4pt, fill: cvstudio-acc, stroke: 1.8pt + white))
    body
  })
