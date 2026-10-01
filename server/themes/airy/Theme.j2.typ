// Airy: section titles small, spaced out and grey, with nothing under them.
#let cvx-label(t) = text(size: 0.8em, weight: 600, tracking: 0.2em,
  fill: {{ design.colors.section_titles.as_rgb() }}, upper(t))
