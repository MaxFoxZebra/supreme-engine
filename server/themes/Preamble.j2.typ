{% set cvs_ours = design.theme in ["studio", "ledger", "sidebar", "aurora", "editorial", "timeline"] %}
{% set cvs_band = design.theme in ["studio", "aurora"] %}
{% set cvs_accent_head = design.theme in ["editorial", "timeline", "sidebar", "ledger"] %}
{% set cvs_pre %}{% include "typst/Preamble.j2.typ" %}{% endset %}
{% if cvs_ours %}
{#- Text in the accent colour darkened until it reads on white; on a band,
    the name and contact line in whichever of white or black reads on it;
    the headline following the accent. Set in RenderCV's own parameters,
    which its headings read. -#}
{% set cvs_pre = cvstudio_swap(cvs_pre, "colors-section-titles", cvstudio_readable(design.colors.section_titles)) %}
{% set cvs_pre = cvstudio_swap(cvs_pre, "colors-links", cvstudio_readable(design.colors.links)) %}
{% if cvs_band %}
{% set cvs_ink = cvstudio_band_ink(design.colors.section_titles) %}
{% set cvs_pre = cvstudio_swap(cvs_pre, "colors-name", cvs_ink) %}
{% set cvs_pre = cvstudio_swap(cvs_pre, "colors-headline", cvs_ink) %}
{% set cvs_pre = cvstudio_swap(cvs_pre, "colors-connections", cvs_ink) %}
{% elif cvs_accent_head %}
{% set cvs_pre = cvstudio_swap(cvs_pre, "colors-headline", cvstudio_readable(design.colors.section_titles)) %}
{% endif %}
{% endif %}
{% if design.theme == "sidebar" %}
// Sidebar: the tint behind the right-hand column, on every page, set before
// RenderCV's own page setup, which then makes room for it.
#show: doc => { set page(background: place(right + top, rect(width: {{ design.page.right_margin }} + 5.8cm, height: 100%, fill: {{ design.colors.section_titles.as_rgb() }}.lighten(91%)))); doc }
{% set cvs_pre = cvstudio_swap(cvs_pre, "page-right-margin", design.page.right_margin ~ " + 6.2cm") %}
{% elif design.theme == "editorial" %}
// Editorial: a slim accent stripe down the left edge of every page.
#show: doc => { set page(background: place({{ "right" if locale.is_rtl else "left" }} + top, rect(width: 0.55cm, height: 100%, fill: {{ design.colors.section_titles.as_rgb() }}))); doc }
{% endif %}
{{ cvs_pre }}

// CV Studio: contact icons drawn as pictures, not font characters, so an
// ATS reads only the text beside them.
{% set cvs_ic = (cvstudio_band_ink(design.colors.section_titles) if cvs_band else design.colors.connections.as_hex()) %}
#let cvstudio-icons = (
{% for name in ["envelope", "phone", "location-dot", "link", "linkedin", "github", "x-twitter"] %}
  "{{ name }}": bytes("{{ cvstudio_icon(name, cvs_ic)|replace('"', '\\"') }}"),
{% endfor %}
)
#let connection-with-icon(icon-name, body) = [
  #box(baseline: 0.13em, image(cvstudio-icons.at(icon-name, default: cvstudio-icons.at("link")), format: "svg", height: 0.85em))#h(0.12cm)#box[#body]
]
{% if design.theme in ["studio", "ledger", "sidebar", "aurora", "editorial", "timeline"] %}
{% include design.theme ~ "/Theme.j2.typ" %}
{% endif %}
