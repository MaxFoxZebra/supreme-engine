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
#let cvstudio-stop(body) = block(width: 100%, above: 0pt, below: 0pt, breakable: true,
  inset: (left: 15pt, bottom: 9pt),
  stroke: (left: 1.2pt + cvstudio-acc.lighten(65%)), {
    place(top + left, dx: -15pt - 4.6pt, dy: 1.5pt, circle(radius: 4pt, fill: cvstudio-acc, stroke: 1.8pt + white))
    body
  })
