// Swiss, after Google Docs: the dates and place in small capitals under
// each role, in the one column.
#let cvx-it(b) = emph(cvx-trim(b))
#let cvx-when(b) = text(size: 0.8em, tracking: 0.07em, fill: rgb(110, 110, 110), upper(cvx-trim(b)))
#let cvx-where(b) = { let t = cvx-trim(b); if t != none { text(size: 0.8em, tracking: 0.07em, fill: rgb(110, 110, 110))[#h(0.1em)·#h(0.35em)#upper(t)] } }
