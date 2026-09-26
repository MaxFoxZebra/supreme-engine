// Aurora: a gradient band behind the name, section titles as tinted labels.
{% set acc = design.colors.section_titles.as_rgb() %}
#let cvstudio-acc = {{ acc }}
#let cvstudio-title(t) = box(fill: cvstudio-acc.lighten(88%), radius: 3pt, inset: (x: 7pt, y: 3.5pt), {
  set text(weight: 600, fill: {{ cvstudio_readable(design.colors.section_titles) }})
  t
})
