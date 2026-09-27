{#- RenderCV's own header, with the first name light grey and the last bold. -#}
{% set cvx_head %}{% include "typst/Header.j2.typ" %}{% endset %}
{% set cvx_parts = (cv.name or "").rsplit(" ", 1) %}
{% if cvx_parts|length == 2 %}
{{ cvx_head|replace("= " ~ cv.name, "= #text(weight: 300, fill: rgb(93, 93, 93))[" ~ cvx_parts[0] ~ "] " ~ cvx_parts[1]) }}
{% else %}
{{ cvx_head }}
{% endif %}
