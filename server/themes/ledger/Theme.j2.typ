// Ledger: a left rail carries the section titles and every entry's dates
// and place; the content runs in the column beside it. RenderCV's moderncv
// layout gives the rail; the title is moved into it on a line of its own,
// so it never meets a date. The heading stays a heading, so RenderCV still
// groups each section with its entries.
{% set acc = design.colors.section_titles.as_rgb() %}
#let cvstudio-title(t) = context {
  let c = rendercv-config.get()
  let rail = c.at("entries-date-and-location-width") + c.at("entries-side-space")
  move(dx: -rail - c.at("entries-space-between-columns"),
    box(width: rail, {
      set par(leading: 0.4em, justify: false)
      set align(end)
      set text(weight: 700, fill: {{ acc }}, size: {{ design.typography.font_size.section_titles }})
      upper(t)
    }))
}
