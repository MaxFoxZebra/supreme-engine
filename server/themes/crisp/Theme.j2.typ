// Crisp, after Enhancv's single column: role, then company in the accent
// (the headline's colour), then dates and place with small pictures (not
// characters) beside them. Section titles and their rule take the section
// title colour.
#let cvx-acc = {{ cvstudio_readable(design.colors.headline) }}
#let cvx-rule = {{ cvstudio_readable(design.colors.section_titles) }}
#let cvx-role(b) = text(weight: 700, size: 1.1em, fill: rgb(17, 17, 17), cvx-trim(b))
#let cvx-org(b) = text(weight: 700, fill: cvx-acc, cvx-trim(b))
#let cvx-icon(name) = box(baseline: 0.13em, image(cvstudio-icons.at(name), format: "svg", height: 0.8em))
#let cvx-when(b) = { let t = cvx-trim(b); if t != none { text(size: 0.88em, fill: rgb(95, 95, 95))[#cvx-icon("calendar")#h(0.12cm)#t#h(0.45cm)] } }
#let cvx-where(b) = { let t = cvx-trim(b); if t != none { text(size: 0.88em, fill: rgb(95, 95, 95))[#cvx-icon("location-dot")#h(0.12cm)#t] } }
#let cvx-title(t) = block(width: 100%, inset: (bottom: 0.14cm), stroke: (bottom: 2pt + cvx-rule),
  text(tracking: 0.03em, upper(t)))
