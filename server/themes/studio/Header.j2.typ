// Studio: the name, headline and contact line on a band of the accent
// colour, across the full width of the page.
#pad(left: -{{ design.page.left_margin }}, right: -{{ design.page.right_margin }}, top: -{{ design.page.top_margin }})[
  #block(width: 100%, fill: {{ design.colors.section_titles.as_rgb() }}, inset: (left: {{ design.page.left_margin }}, right: {{ design.page.right_margin }}, top: 0.95cm, bottom: 0.8cm), below: 0.55cm)[
{% if cv.photo %}
    #grid(columns: (1fr, auto), column-gutter: 0.6cm, align: horizon,
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
    ],
    [#box(clip: true, radius: 50%, image("{{ cv.photo|string }}", width: {{ design.header.photo_width }}))])
{% endif %}
  ]
]
