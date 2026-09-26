// Aurora: the band runs across the whole page, the accent turning into a
// neighbouring hue, with two faint circles for depth. Shapes, not text:
// an ATS reads only the name and contact line on it.
#pad(left: -{{ design.page.left_margin }}, right: -{{ design.page.right_margin }}, top: -{{ design.page.top_margin }})[
  #block(width: 100%, clip: true, fill: gradient.linear(cvstudio-acc, cvstudio-acc.rotate(-55deg, space: oklch), angle: 18deg), inset: (left: {{ design.page.left_margin }}, right: {{ design.page.right_margin }}, top: 1.05cm, bottom: 0.95cm), below: 0.5cm)[
    #place(top + right, dx: 2.2cm, dy: -2.6cm, circle(radius: 3.4cm, fill: white.transparentize(90%)))
    #place(top + right, dx: -1.6cm, dy: 1.4cm, circle(radius: 1.3cm, fill: white.transparentize(92%)))
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
    [#box(clip: true, radius: 50%, stroke: 2.5pt + white.transparentize(30%), image("{{ cv.photo|string }}", width: {{ design.header.photo_width }}))])
{% endif %}
  ]
]
