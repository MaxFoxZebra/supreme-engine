// Studio: section titles in the accent colour over a two-weight rule. Drawn
// inside the heading's own text, because a show rule here would hide the
// headings from the way RenderCV groups a section with its entries.
{% set acc = design.colors.section_titles.as_rgb() %}
#let cvstudio-title(t) = {
  set text(weight: 600)
  t
  v(3pt)
  grid(columns: (1.2cm, 1fr), align: horizon,
    box(width: 100%, height: 2.2pt, fill: {{ acc }}),
    box(width: 100%, height: 0.6pt, fill: {{ acc }}.lighten(75%)))
}
