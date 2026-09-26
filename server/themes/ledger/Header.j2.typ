// Ledger: the name set large, the headline in the accent colour, a strong
// rule under the contact line.
{% if cv.photo %}
#grid(columns: {% if design.header.photo_position == "left" %}(auto, 1fr){% else %}(1fr, auto){% endif %}, column-gutter: 0.6cm, align: horizon,
{% if design.header.photo_position == "left" %}
[#box(clip: true, radius: 50%, image("{{ cv.photo.name }}", width: {{ design.header.photo_width }}))],
{% endif %}
[
{% endif %}
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
{% if cv.photo %}
]{% if design.header.photo_position != "left" %},
[#box(clip: true, radius: 50%, image("{{ cv.photo.name }}", width: {{ design.header.photo_width }}))]{% endif %})
{% endif %}
#v(0.15cm)
#line(length: 100%, stroke: 1.4pt + {{ design.colors.name.as_rgb() }})
#v(0.1cm)
