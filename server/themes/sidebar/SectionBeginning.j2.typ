{% set side = snake_case_section_title in design.sidebar_sections %}
{% set _ = cvstudio_sides.append(side) %}
{% if side %}
#cvstudio-side-add([{{ section_title }}], [
{% else %}
== #cvstudio-title[{{ section_title }}]
{% endif %}
{% if entry_type in ["ReversedNumberedEntry"] %}

#reversed-numbered-entries(
  [
{% endif %}
