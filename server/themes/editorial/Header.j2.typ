// Editorial: the name set large in a book face, the headline in italics in
// the accent colour, the contact line under a hairline.
{% if cv.photo %}
#grid(columns: (1fr, auto), column-gutter: 0.6cm, align: horizon,
[
{% endif %}
{% if cv.name %}
= {{ cv.name }}
{% endif %}
{% if cv.headline %}
#text(style: "italic")[#headline([{{ cv.headline }}])]
{% endif %}
#v(0.08cm)
#line(length: 100%, stroke: 0.5pt + {{ design.colors.section_titles.as_rgb() }}.lighten(40%))
#v(0.05cm)
#connections(
{% for connection in cv._connections %}
  [{{ connection }}],
{% endfor %}
)
{% if cv.photo %}
],
[#box(clip: true, radius: 50%, image("{{ cv.photo.name }}", width: {{ design.header.photo_width }}))])
{% endif %}
