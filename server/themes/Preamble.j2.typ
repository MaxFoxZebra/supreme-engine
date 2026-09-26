{% if design.theme == "sidebar" %}
// Sidebar: the tint behind the right-hand column, on every page, set before
// RenderCV's own page setup, which then makes room for it.
#show: doc => { set page(background: place(right + top, rect(width: {{ design.page.right_margin }} + 5.8cm, height: 100%, fill: {{ design.colors.section_titles.as_rgb() }}.lighten(91%)))); doc }
{% set cvs_pre %}{% include "typst/Preamble.j2.typ" %}{% endset %}
{{ cvs_pre|replace("page-right-margin: " ~ design.page.right_margin ~ ",", "page-right-margin: " ~ design.page.right_margin ~ " + 6.2cm,") }}
{% else %}
{% include "typst/Preamble.j2.typ" %}
{% endif %}

// CV Studio: contact icons drawn as pictures, not font characters, so an
// ATS reads only the text beside them.
{% set cvs_ic = design.colors.connections.as_hex() %}
#let cvstudio-icons = (
{% for name in ["envelope", "phone", "location-dot", "link", "linkedin", "github", "x-twitter"] %}
  "{{ name }}": bytes("{{ cvstudio_icon(name, cvs_ic)|replace('"', '\\"') }}"),
{% endfor %}
)
#let connection-with-icon(icon-name, body) = [
  #box(baseline: 0.13em, image(cvstudio-icons.at(icon-name, default: cvstudio-icons.at("link")), format: "svg", height: 0.85em))#h(0.12cm)#box[#body]
]
{% if design.theme in ["studio", "ledger", "sidebar"] %}
{% include design.theme ~ "/Theme.j2.typ" %}
{% endif %}
