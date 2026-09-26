// Sidebar: the name and contact line lead the main column; the sidebar is
// placed beside them, at the top of page one.
{% if cv.name %}
= {{ cv.name }}
{% endif %}
{% if cv.headline %}
#headline([{{ cv.headline }}])
{% endif %}
#connections(
{% for connection in cv._connections %}
  [{{ connection }}],
{% endfor %}
)
// Placed after the contact line, so a reader that follows the text meets the
// name first; it still shows at the top of the page.
#place(top + right, dx: cvstudio-side-width + cvstudio-side-gap, box(width: cvstudio-side-width, {
  set align(start)
  set par(justify: false, leading: 0.6em, spacing: 0.9em)
{% if cv.photo %}
  align(center, box(clip: true, radius: 50%, image("{{ cv.photo|string }}", width: {{ design.header.photo_width }})))
  v(0.3cm)
{% endif %}
  context { for c in cvstudio-side.final() { c } }
}))
