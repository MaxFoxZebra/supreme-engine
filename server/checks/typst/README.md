# Typst fixtures

Real output from `rendercv render`, one file per theme, for `checks/maptest.py`
to read. They are here because cv_map parses somebody else's generated code:
the only way to know it still works is to keep a copy of what that code looks
like, from every theme and every RenderCV whose shape differs.

`2.8/` is what the app ships (`rendercv[full]==2.8`, Python 3.12+). `2.3/` is
the last shape before RenderCV unified its themes onto one template, which a
developer gets from an unpinned `uv pip install "rendercv[full]"` on Python
3.11.

To regenerate, render this CV with each theme and copy the `.typ` out of the
output folder:

```yaml
cv:
  name: Ada Lovelace
  email: ada@example.com
  location: London
  sections:
    summary:
      - One paragraph of prose, which RenderCV writes as a TextEntry.
    experience:
      - company: Analytical Engines
        position: Engineer
        location: London
        start_date: 2020-01
        end_date: 2022-06
        highlights:
          - Wrote the first algorithm
          - Published notes on the engine
      - company: Difference Co
        position: Advisor
        location: London
        start_date: 2018-01
        end_date: 2019-12
        highlights:
          - Advised on gearing
    education:
      - institution: Home tutoring
        area: Mathematics
        degree: BSc
        start_date: 2014-09
        end_date: 2017-06
        highlights:
          - Thesis on series
    skills:
      - label: Mathematics
        details: Series, calculus, notation
      - label: Engines
        details: Gearing, punch cards, loops
      - label: Writing
        details: Notes, correspondence, translation
      - label: Languages
        details: English, French, Italian
    projects:
      - name: Note G
        highlights:
          - The first published algorithm
    certifications:
      - bullet: Fellow of the Royal Society, 1843
      - bullet: Honorary member, 1844
    publications:
      - title: Sketch of the Analytical Engine
        authors:
          - L. Menabrea
          - A. Lovelace
        journal: Scientific Memoirs
        date: 1843
design:
  theme: classic
```

Projects has no dates on purpose: that is the entry RenderCV 2.8 indents, and
the one that used to take the whole click map down with it.
