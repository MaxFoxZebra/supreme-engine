// Bold: each section title a bar of the accent, the words in whichever of
// white or near-black reads on it, in spaced capitals.
#let cvx-bar(t) = block(width: 100%, fill: {{ design.colors.section_titles.as_rgb() }},
  inset: (x: 0.24cm, y: 0.15cm), radius: 1.5pt,
  text(fill: {{ cvstudio_band_ink(design.colors.section_titles) }}, size: 0.9em, weight: 700,
    tracking: 0.07em, upper(t)))
