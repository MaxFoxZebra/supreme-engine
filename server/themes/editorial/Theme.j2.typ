// Editorial: section titles in small capitals over a hairline.
{% set acc = design.colors.section_titles.as_rgb() %}
#let cvstudio-title(t) = {
  t
  v(1pt)
  box(width: 100%, height: 0.5pt, fill: {{ acc }}.lighten(40%))
}
