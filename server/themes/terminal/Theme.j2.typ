// Terminal: section titles as a comment, // in the accent and the title in
// lower case. A string, because // would start a comment in Typst markup.
#let cvx-term(t) = [#text(fill: {{ cvstudio_readable(design.colors.section_titles) }}, weight: 700, "// ")#text(weight: 700, lower(t))]
