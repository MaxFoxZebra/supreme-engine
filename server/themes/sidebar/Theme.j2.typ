// Sidebar: a tinted column on the right carries the short sections (skills,
// languages, certifications...). They are collected as the page is built
// and placed there, so nothing in your CV is reordered.
{% set acc = design.colors.section_titles.as_rgb() %}
#let cvstudio-side-width = 5.4cm
#let cvstudio-side-gap = 0.8cm  // 6.2cm in all, as the preamble reserves
#let cvstudio-side = state("cvstudio-side", ())
#let cvstudio-title(t) = {
  set text(weight: 700)
  t
  v(2pt)
  box(width: 100%, height: 0.8pt, fill: {{ acc }}.lighten(55%))
}
// A sidebar section is not a heading of the page, so RenderCV does not group
// it: its title and spacing are drawn here, with a narrow date column that
// is put back afterwards.
#let cvstudio-side-add(title, body) = cvstudio-side.update(x => x + (context {
  let was = rendercv-config.get()
  rendercv-config.update(c => { c.insert("entries-date-and-location-width", 1.7cm); c.insert("entries-space-between-columns", 0.2cm); c.insert("entries-side-space", 0cm); c })
  block(above: 0.55cm, below: 0.25cm, {
    set text(font: "{{ design.typography.font_family.section_titles }}", size: {{ design.typography.font_size.section_titles }}, fill: {{ acc }})
    cvstudio-title(title)
  })
  content-area(body)
  rendercv-config.update(was)
},))
