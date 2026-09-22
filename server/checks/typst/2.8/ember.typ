// Import the rendercv function and all the refactored components
#import "@preview/rendercv:0.3.0": *

// Apply the rendercv template with custom configuration
#show: rendercv.with(
  name: "Ada Lovelace",
  title: "Ada Lovelace - CV",
  footer: context { [#emph[Ada Lovelace -- #str(here().page())\/#str(counter(page).final().first())]] },
  top-note: [ #emph[Last updated in Sept 2026] ],
  locale-catalog-language: "en",
  text-direction: ltr,
  page-size: "us-letter",
  page-top-margin: 0.6in,
  page-bottom-margin: 0.6in,
  page-left-margin: 0.6in,
  page-right-margin: 0.6in,
  page-show-footer: true,
  page-show-top-note: true,
  colors-body: rgb(35, 31, 32),
  colors-name: rgb(155, 35, 25),
  colors-headline: rgb(90, 60, 55),
  colors-connections: rgb(100, 75, 68),
  colors-section-titles: rgb(155, 35, 25),
  colors-links: rgb(155, 35, 25),
  colors-footer: rgb(140, 125, 118),
  colors-top-note: rgb(140, 125, 118),
  typography-line-spacing: 0.6em,
  typography-alignment: "justified-with-no-hyphenation",
  typography-date-and-location-column-alignment: right,
  typography-font-family-body: "Ubuntu",
  typography-font-family-name: "Gentium Book Plus",
  typography-font-family-headline: "Gentium Book Plus",
  typography-font-family-connections: "Ubuntu",
  typography-font-family-section-titles: "Ubuntu",
  typography-font-size-body: 10pt,
  typography-font-size-name: 30pt,
  typography-font-size-headline: 10.5pt,
  typography-font-size-connections: 9pt,
  typography-font-size-section-titles: 1.25em,
  typography-small-caps-name: false,
  typography-small-caps-headline: true,
  typography-small-caps-connections: false,
  typography-small-caps-section-titles: true,
  typography-bold-name: true,
  typography-bold-headline: false,
  typography-bold-connections: false,
  typography-bold-section-titles: false,
  links-underline: true,
  links-show-external-link-icon: false,
  header-alignment: center,
  header-photo-width: 3.5cm,
  header-space-below-name: 0.5cm,
  header-space-below-headline: 0.4cm,
  header-space-below-connections: 0.6cm,
  header-connections-hyperlink: true,
  header-connections-show-icons: false,
  header-connections-display-urls-instead-of-usernames: false,
  header-connections-separator: "·",
  header-connections-space-between-connections: 0.5cm,
  section-titles-type: "centered_without_line",
  section-titles-line-thickness: 0.5pt,
  section-titles-space-above: 0.55cm,
  section-titles-space-below: 0.25cm,
  sections-allow-page-break: true,
  sections-space-between-text-based-entries: 0.3em,
  sections-space-between-regular-entries: 1.1em,
  entries-date-and-location-width: 4.15cm,
  entries-side-space: 0.1cm,
  entries-space-between-columns: 0.15cm,
  entries-allow-page-break: false,
  entries-short-second-row: false,
  entries-degree-width: 1cm,
  entries-summary-space-left: 0cm,
  entries-summary-space-above: 0.05cm,
  entries-highlights-bullet:  "◆" ,
  entries-highlights-nested-bullet:  "◦" ,
  entries-highlights-space-left: 0.15cm,
  entries-highlights-space-above: 0.05cm,
  entries-highlights-space-between-items: 0.04cm,
  entries-highlights-space-between-bullet-and-text: 0.5em,
  date: datetime(
    year: 2026,
    month: 9,
    day: 22,
  ),
)


= Ada Lovelace

#connections(
  [#link("mailto:ada@example.com", icon: false, if-underline: false, if-color: false)[ada\@example.com]],
  [London],
)


== Summary

One paragraph of prose, which RenderCV writes as a TextEntry.

== Experience

#regular-entry(
  [
    #strong[Analytical Engines] -- London

  ],
  [
    Jan 2020 – June 2022

  ],
  main-column-second-row: [
    #emph[Engineer]

    - Wrote the first algorithm

    - Published notes on the engine

  ],
)

#regular-entry(
  [
    #strong[Difference Co] -- London

  ],
  [
    Jan 2018 – Dec 2019

  ],
  main-column-second-row: [
    #emph[Advisor]

    - Advised on gearing

  ],
)

== Education

#education-entry(
  [
    #strong[Home tutoring]

  ],
  [
    Sept 2014 – June 2017

  ],
  main-column-second-row: [
    #emph[BSc in Mathematics]

    - Thesis on series

  ],
)

== Skills

#strong[Mathematics:] Series, calculus, notation

#strong[Engines:] Gearing, punch cards, loops

#strong[Writing:] Notes, correspondence, translation

#strong[Languages:] English, French, Italian

== Projects

  #regular-entry(
  [
    #strong[Note G]

  ],
  [
  ],
  main-column-second-row: [
    - The first published algorithm

  ],
)

== Certifications

- Fellow of the Royal Society, 1843

- Honorary member, 1844

== Publications

#regular-entry(
  [
    #strong[Sketch of the Analytical Engine]

  ],
  [
    1843

  ],
  main-column-second-row: [
    L. Menabrea, A. Lovelace

    (Scientific Memoirs)

  ],
)
