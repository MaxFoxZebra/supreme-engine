// Editorial: a slim accent stripe down the left edge of every page.
#show: doc => { set page(background: place(left + top, rect(width: 0.55cm, height: 100%, fill: rgb(122, 31, 43)))); doc }
// Import the rendercv function and all the refactored components
#import "@preview/rendercv:0.3.0": *

// Apply the rendercv template with custom configuration
#show: rendercv.with(
  name: "Ada Lovelace",
  title: "Ada Lovelace - CV",
  footer: context { [#emph[Ada Lovelace -- #str(here().page())\/#str(counter(page).final().first())]] },
  top-note: [ #emph[Last updated in Oct 2026] ],
  locale-catalog-language: "en",
  text-direction: ltr,
  page-size: "us-letter",
  page-top-margin: 1.5cm,
  page-bottom-margin: 1.4cm,
  page-left-margin: 2.1cm,
  page-right-margin: 1.7cm,
  page-show-footer: false,
  page-show-top-note: false,
  colors-body: rgb(30, 30, 30),
  colors-name: rgb(24, 24, 27),
  colors-headline: rgb(122, 31, 43),
  colors-connections: rgb(70, 70, 70),
  colors-section-titles: rgb(122, 31, 43),
  colors-links: rgb(122, 31, 43),
  colors-footer: rgb(128, 128, 128),
  colors-top-note: rgb(128, 128, 128),
  typography-line-spacing: 0.55em,
  typography-alignment: "left",
  typography-date-and-location-column-alignment: right,
  typography-font-family-body: "XCharter",
  typography-font-family-name: "EB Garamond",
  typography-font-family-headline: "EB Garamond",
  typography-font-family-connections: "XCharter",
  typography-font-family-section-titles: "EB Garamond",
  typography-font-size-body: 9.8pt,
  typography-font-size-name: 36pt,
  typography-font-size-headline: 13pt,
  typography-font-size-connections: 10pt,
  typography-font-size-section-titles: 1.25em,
  typography-small-caps-name: false,
  typography-small-caps-headline: false,
  typography-small-caps-connections: false,
  typography-small-caps-section-titles: true,
  typography-bold-name: false,
  typography-bold-headline: false,
  typography-bold-connections: false,
  typography-bold-section-titles: false,
  links-underline: false,
  links-show-external-link-icon: false,
  header-alignment: left,
  header-photo-width: 3.5cm,
  header-space-below-name: 0.7cm,
  header-space-below-headline: 0.7cm,
  header-space-below-connections: 0.7cm,
  header-connections-hyperlink: true,
  header-connections-show-icons: false,
  header-connections-display-urls-instead-of-usernames: true,
  header-connections-separator: "",
  header-connections-space-between-connections: 0.5cm,
  section-titles-type: "without_line",
  section-titles-line-thickness: 0.5pt,
  section-titles-space-above: 0.5cm,
  section-titles-space-below: 0.2cm,
  sections-allow-page-break: true,
  sections-space-between-text-based-entries: 0.3em,
  sections-space-between-regular-entries: 1.2em,
  entries-date-and-location-width: 4.15cm,
  entries-side-space: 0.2cm,
  entries-space-between-columns: 0.1cm,
  entries-allow-page-break: false,
  entries-short-second-row: true,
  entries-degree-width: 1cm,
  entries-summary-space-left: 0cm,
  entries-summary-space-above: 0cm,
  entries-highlights-bullet:  "•" ,
  entries-highlights-nested-bullet:  "•" ,
  entries-highlights-space-left: 0.15cm,
  entries-highlights-space-above: 0cm,
  entries-highlights-space-between-items: 0cm,
  entries-highlights-space-between-bullet-and-text: 0.5em,
  date: datetime(
    year: 2026,
    month: 10,
    day: 14,
  ),
)

// CV Studio: contact icons drawn as pictures, not font characters, so an
// ATS reads only the text beside them.
#let cvstudio-icons = (
  "envelope": bytes("<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 24 24\"><path fill=\"#464646\" d=\"M3 5h18a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1zm.8 2.1V17h16.4V7.1L12 12.6zM5.3 7 12 11.3 18.7 7z\"/></svg>"),
  "phone": bytes("<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 24 24\"><path fill=\"#464646\" d=\"M17.472 14.382c-.297-.149-1.758-.867-2.03-.967c-.273-.099-.471-.148-.67.15c-.197.297-.767.966-.94 1.164c-.173.199-.347.223-.644.075c-.297-.15-1.255-.463-2.39-1.475c-.883-.788-1.48-1.761-1.653-2.059c-.173-.297-.018-.458.13-.606c.134-.133.298-.347.446-.52s.198-.298.298-.497c.099-.198.05-.371-.025-.52s-.669-1.612-.916-2.207c-.242-.579-.487-.5-.669-.51a13 13 0 0 0-.57-.01c-.198 0-.52.074-.792.372c-.272.297-1.04 1.016-1.04 2.479c0 1.462 1.065 2.875 1.213 3.074s2.096 3.2 5.077 4.487c.709.306 1.262.489 1.694.625c.712.227 1.36.195 1.871.118c.571-.085 1.758-.719 2.006-1.413s.248-1.289.173-1.413c-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 0 1-5.031-1.378l-.361-.214l-3.741.982l.998-3.648l-.235-.374a9.86 9.86 0 0 1-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884c2.64 0 5.122 1.03 6.988 2.898a9.82 9.82 0 0 1 2.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.82 11.82 0 0 0 12.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.9 11.9 0 0 0 5.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.82 11.82 0 0 0-3.48-8.413\"/></svg>"),
  "location-dot": bytes("<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 24 24\"><path fill=\"#464646\" d=\"M12 1.5a7.5 7.5 0 0 1 7.5 7.5c0 5.2-6.3 12.3-6.9 13a.8.8 0 0 1-1.2 0C10.8 21.3 4.5 14.2 4.5 9A7.5 7.5 0 0 1 12 1.5zm0 4.5a3 3 0 1 0 0 6 3 3 0 0 0 0-6z\"/></svg>"),
  "link": bytes("<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 24 24\"><path fill=\"#464646\" d=\"M12 1.5a10.5 10.5 0 1 1 0 21 10.5 10.5 0 0 1 0-21zm-2.1 2.4a8.6 8.6 0 0 0-6.1 7.2h3.9c.1-2.6.9-5.1 2.2-7.2zm4.2 0c1.3 2.1 2.1 4.6 2.2 7.2h3.9a8.6 8.6 0 0 0-6.1-7.2zM12 4.2c-1.4 1.9-2.2 4.3-2.4 6.9h4.8c-.2-2.6-1-5-2.4-6.9zM3.8 12.9a8.6 8.6 0 0 0 6.1 7.2 14.8 14.8 0 0 1-2.2-7.2zm5.8 0c.2 2.6 1 5 2.4 6.9 1.4-1.9 2.2-4.3 2.4-6.9zm6.7 0c-.1 2.6-.9 5.1-2.2 7.2a8.6 8.6 0 0 0 6.1-7.2z\"/></svg>"),
  "linkedin": bytes("<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 24 24\"><path fill=\"#464646\" d=\"M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433c-1.144 0-2.063-.926-2.063-2.065 0-1.138.92-2.063 2.063-2.063 1.14 0 2.064.925 2.064 2.063 0 1.139-.925 2.065-2.064 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z\"/></svg>"),
  "github": bytes("<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 24 24\"><path fill=\"#464646\" d=\"M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.809 1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 2.28-1.552 3.285-1.23 3.285-1.23.645 1.653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36.81 1.096.81 2.22 0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12\"/></svg>"),
  "x-twitter": bytes("<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 24 24\"><path fill=\"#464646\" d=\"M18.901 1.153h3.68l-8.04 9.19L24 22.846h-7.406l-5.8-7.584-6.638 7.584H.474l8.6-9.83L0 1.154h7.594l5.243 6.932ZM17.61 20.644h2.039L6.486 3.24H4.298Z\"/></svg>"),
  "calendar": bytes("<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 24 24\"><path fill=\"#464646\" d=\"M7 2h2v2h6V2h2v2h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2zM5 10v10h14V10zm0-2h14V6H5z\"/></svg>"),
)
#let connection-with-icon(icon-name, body) = [
  #box(baseline: 0.13em, image(cvstudio-icons.at(icon-name, default: cvstudio-icons.at("link")), format: "svg", height: 0.85em))#h(0.12cm)#box[#body]
]
// A field passed as #f[ FIELD ] (spaced, so RenderCV can drop a missing
// field without eating the call): without its spaces, or none if empty.
#let cvx-trim(b) = {
  if b == [ ] or b == [] { return none }
  if not b.has("children") { return b }
  let c = b.children
  while c.len() > 0 and c.first() == [ ] { c = c.slice(1) }
  while c.len() > 0 and c.last() == [ ] { c = c.slice(0, -1) }
  if c.len() == 0 { none } else { c.join() }
}
// Editorial: section titles in small capitals over a hairline.
#let cvstudio-title(t) = {
  t
  v(1pt)
  box(width: 100%, height: 0.5pt, fill: rgb(122, 31, 43).lighten(40%))
}

// Editorial: the name set large in a book face, the headline in italics in
// the accent colour, the contact line under a hairline.
= Ada Lovelace
#v(0.08cm)
#line(length: 100%, stroke: 0.5pt + rgb(122, 31, 43).lighten(40%))
#v(0.05cm)
#connections(
  [#link("mailto:ada@example.com", icon: false, if-underline: false, if-color: false)[ada\@example.com]],
  [London],
)


== #cvstudio-title[Summary]

One paragraph of prose, which RenderCV writes as a TextEntry.

== #cvstudio-title[Experience]

#regular-entry(
  [
    #strong[Analytical Engines], Engineer

    - Wrote the first algorithm

    - Published notes on the engine

  ],
  [
    London

    Jan 2020 – June 2022

  ],
)

#regular-entry(
  [
    #strong[Difference Co], Advisor

    - Advised on gearing

  ],
  [
    London

    Jan 2018 – Dec 2019

  ],
)

== #cvstudio-title[Education]

#education-entry(
  [
    #strong[Home tutoring], Mathematics

    - Thesis on series

  ],
  [
    Sept 2014 – June 2017

  ],
  degree-column: [
    #strong[BSc]
  ],
)

== #cvstudio-title[Skills]

#strong[Mathematics:] Series, calculus, notation

#strong[Engines:] Gearing, punch cards, loops

#strong[Writing:] Notes, correspondence, translation

#strong[Languages:] English, French, Italian

== #cvstudio-title[Projects]

#regular-entry(
  [
    #strong[Note G]

    - The first published algorithm

  ],
  [
  ],
)

== #cvstudio-title[Certifications]

- Fellow of the Royal Society, 1843

- Honorary member, 1844

== #cvstudio-title[Publications]

#regular-entry(
  [
    #strong[Sketch of the Analytical Engine]

    L. Menabrea, A. Lovelace

    (Scientific Memoirs)

  ],
  [
    1843

  ],
)
