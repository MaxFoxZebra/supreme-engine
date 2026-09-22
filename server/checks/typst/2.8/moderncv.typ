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
  page-top-margin: 0.7in,
  page-bottom-margin: 0.7in,
  page-left-margin: 0.7in,
  page-right-margin: 0.7in,
  page-show-footer: true,
  page-show-top-note: true,
  colors-body: rgb(0, 0, 0),
  colors-name: rgb(0, 79, 144),
  colors-headline: rgb(0, 79, 144),
  colors-connections: rgb(0, 79, 144),
  colors-section-titles: rgb(0, 79, 144),
  colors-links: rgb(0, 79, 144),
  colors-footer: rgb(128, 128, 128),
  colors-top-note: rgb(128, 128, 128),
  typography-line-spacing: 0.6em,
  typography-alignment: "justified",
  typography-date-and-location-column-alignment: right,
  typography-font-family-body: "Fontin",
  typography-font-family-name: "Fontin",
  typography-font-family-headline: "Fontin",
  typography-font-family-connections: "Fontin",
  typography-font-family-section-titles: "Fontin",
  typography-font-size-body: 10pt,
  typography-font-size-name: 25pt,
  typography-font-size-headline: 10pt,
  typography-font-size-connections: 10pt,
  typography-font-size-section-titles: 1.4em,
  typography-small-caps-name: false,
  typography-small-caps-headline: false,
  typography-small-caps-connections: false,
  typography-small-caps-section-titles: false,
  typography-bold-name: false,
  typography-bold-headline: false,
  typography-bold-connections: false,
  typography-bold-section-titles: false,
  links-underline: true,
  links-show-external-link-icon: false,
  header-alignment: left,
  header-photo-width: 4.15cm,
  header-space-below-name: 0.7cm,
  header-space-below-headline: 0.7cm,
  header-space-below-connections: 0.7cm,
  header-connections-hyperlink: true,
  header-connections-show-icons: true,
  header-connections-display-urls-instead-of-usernames: false,
  header-connections-separator: "",
  header-connections-space-between-connections: 0.5cm,
  section-titles-type: "moderncv",
  section-titles-line-thickness: 0.15cm,
  section-titles-space-above: 0.55cm,
  section-titles-space-below: 0.3cm,
  sections-allow-page-break: true,
  sections-space-between-text-based-entries: 0.3em,
  sections-space-between-regular-entries: 1.2em,
  entries-date-and-location-width: 4.15cm,
  entries-side-space: 0cm,
  entries-space-between-columns: 0.3cm,
  entries-allow-page-break: false,
  entries-short-second-row: false,
  entries-degree-width: 1cm,
  entries-summary-space-left: 0cm,
  entries-summary-space-above: 0.1cm,
  entries-highlights-bullet:  "•" ,
  entries-highlights-nested-bullet:  "•" ,
  entries-highlights-space-left: 0cm,
  entries-highlights-space-above: 0.15cm,
  entries-highlights-space-between-items: 0.1cm,
  entries-highlights-space-between-bullet-and-text: 0.3em,
  date: datetime(
    year: 2026,
    month: 9,
    day: 22,
  ),
)


= Ada Lovelace

#connections(
  [#link("mailto:ada@example.com", icon: false, if-underline: false, if-color: false)[#connection-with-icon("envelope")[ada\@example.com]]],
  [#connection-with-icon("location-dot")[London]],
)


== Summary

One paragraph of prose, which RenderCV writes as a TextEntry.

== Experience

#regular-entry(
  [
    #strong[Engineer], Analytical Engines -- London

  ],
  [
    Jan 2020 – June 2022

  ],
  main-column-second-row: [
    - Wrote the first algorithm

    - Published notes on the engine

  ],
)

#regular-entry(
  [
    #strong[Advisor], Difference Co -- London

  ],
  [
    Jan 2018 – Dec 2019

  ],
  main-column-second-row: [
    - Advised on gearing

  ],
)

== Education

#education-entry(
  [
    #strong[Home tutoring], BSc in Mathematics

  ],
  [
    Sept 2014 – June 2017

  ],
  main-column-second-row: [
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
