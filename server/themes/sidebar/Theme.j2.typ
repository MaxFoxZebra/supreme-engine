// Two columns: a column on the right carries the short sections (skills,
// languages, certifications...). They are collected as the page is built
// and placed beside the name, so nothing in your CV is reordered. What does
// not fit beside page one continues in the main column, in order.
{% set duo = design.theme == "duo" %}
{% set acc = design.colors.section_titles.as_rgb() %}
{% if duo %}
{% include "crisp/Theme.j2.typ" %}

{% endif %}
#let cvstudio-side-width = {{ "6.3cm" if duo else "5.4cm" }}
#let cvstudio-side-gap = {{ "0.7cm" if duo else "0.8cm" }}  // as the preamble reserves
#let cvstudio-side = state("cvstudio-side", ())
#let cvstudio-side-full = state("cvstudio-side-full", false)
{% if duo %}
#let cvstudio-title(t) = cvx-title(t)
{% else %}
#let cvstudio-title(t) = {
  set text(weight: 700)
  t
  v(2pt)
  box(width: 100%, height: 0.8pt, fill: {{ acc }}.lighten(55%))
}
{% endif %}
#let cvstudio-side-head(title) = block(sticky: true, above: 0.55cm, below: 0.25cm, {
  set text(font: "{{ design.typography.font_family.section_titles }}", size: {{ design.typography.font_size.section_titles }}, fill: {{ cvstudio_readable(design.colors.section_titles) }}, weight: 700)
  cvstudio-title(title)
})
// A sidebar section is not a heading of the page, so RenderCV does not group
// it: its title and spacing are drawn here, with a narrow date column that
// is put back afterwards.
#let cvstudio-side-item(title, body) = context {
  let was = rendercv-config.get()
  rendercv-config.update(c => { c.insert("entries-date-and-location-width", 1.7cm); c.insert("entries-space-between-columns", 0.2cm); c.insert("entries-side-space", 0cm); c })
  cvstudio-side-head(title)
  content-area(body)
  rendercv-config.update(was)
}
// The height beside page one: the page less its margins and the photo.
#let cvstudio-side-room = page-height => page-height - {{ design.page.top_margin }} - {{ design.page.bottom_margin }}{% if cv.photo %} - {{ design.header.photo_width }} - 0.3cm{% endif %} - 0.4cm
#let cvstudio-side-add(title, body) = context {
  let item = cvstudio-side-item(title, body)
  let tall = measure(block(width: cvstudio-side-width, {
    set par(justify: false, leading: 0.6em, spacing: 0.9em)
    for c in cvstudio-side.get() { c }
    item
  })).height
  if not cvstudio-side-full.get() and tall <= cvstudio-side-room(page.height) {
    cvstudio-side.update(x => x + (item,))
  } else {
    // Once one section does not fit, it and the rest stay in the main
    // column, so their order is kept.
    cvstudio-side-full.update(true)
    cvstudio-side-head(title)
    let kids = if body.has("children") { body.children } else { (body,) }
    if kids.any(c => c.func() == metadata and c.value == "skip-content-area") {
      body
    } else {
      // One block per entry, so a long list can run over the page.
      for c in kids.filter(c => c.func() != parbreak and c != [ ]) { content-area(c) }
    }
  }
}
