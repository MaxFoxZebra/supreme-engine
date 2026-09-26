// Ledger: a left rail carries the section titles and every entry's dates
// and place; the content runs in the column beside it. RenderCV's moderncv
// layout gives the rail. The title is drawn into the rail in the gap above
// its section, so it costs no line of its own, and it is sized from the
// design settings rather than measured, so the PDF's outline keeps its text.
{% set acc_text = cvstudio_readable(design.colors.section_titles) %}
{% set rail = "(" ~ design.entries.date_and_location_width ~ " + " ~ design.entries.side_space ~ ")" %}
{% set gap = design.entries.space_between_columns %}
#let cvstudio-title(t) = place(top + start, dx: {{ "" if locale.is_rtl else "-" }}({{ rail }} + {{ gap }}), dy: -1.15em,
  box(width: {{ rail }}, {
    set par(leading: 0.4em, justify: false)
    set align(end)
    set text(weight: 700, fill: {{ acc_text }}, size: {{ design.typography.font_size.section_titles }})
    upper(t)
  }))
