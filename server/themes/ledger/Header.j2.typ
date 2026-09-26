// Ledger: the name set large, the headline in the accent colour, a strong
// rule under the contact line.
{% if cv.name %}
= {{ cv.name }}
{% endif %}
{% if cv.headline %}
#text(fill: {{ design.colors.section_titles.as_rgb() }})[#headline([{{ cv.headline }}])]
{% endif %}
#connections(
{% for connection in cv._connections %}
  [{{ connection }}],
{% endfor %}
)
#v(0.15cm)
#line(length: 100%, stroke: 1.4pt + {{ design.colors.name.as_rgb() }})
#v(0.1cm)
