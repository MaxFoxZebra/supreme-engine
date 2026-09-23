# Third-party notices

CV Studio is MIT licensed. The installers bundle the components below, whose
licences are listed with the versions actually shipped. All are compatible with
distributing this application under the MIT licence.

## Runtime, bundled into the installers

| Component | Version | Licence |
|---|---|---|
| [RenderCV](https://github.com/rendercv/rendercv) | 2.8 | MIT |
| [rendercv-fonts](https://pypi.org/project/rendercv-fonts/) | 0.5.1 | MIT (individual families: SIL Open Font License or Apache-2.0) |
| [Typst](https://github.com/typst/typst) (via the `typst` package) | 0.15.0 | Apache-2.0 |
| [ruamel.yaml](https://sourceforge.net/projects/ruamel-yaml/) | 0.19.1 | MIT |
| [pypdf](https://github.com/py-pdf/pypdf) | 6.19.0 | BSD-3-Clause |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | 2.1.1 | MIT |
| [pydantic](https://github.com/pydantic/pydantic) / pydantic-core | 2.13.5 | MIT |
| [typer](https://github.com/fastapi/typer), [rich](https://github.com/Textualize/rich), [markdown-it-py](https://github.com/executablebooks/markdown-it-py), annotated-types | n/a | MIT |
| [click](https://github.com/pallets/click) | 8.5.0 | BSD-3-Clause |
| [Jinja2](https://github.com/pallets/jinja) | 3.1.6 | BSD-3-Clause |
| [packaging](https://github.com/pypa/packaging) | 26.3 | Apache-2.0 OR BSD-2-Clause |
| typing-extensions | 4.16.0 | PSF-2.0 |
| [CPython](https://www.python.org/) | 3.13 | PSF-2.0 |
| [Tauri](https://github.com/tauri-apps/tauri) | 2.x | MIT OR Apache-2.0 |

## Bundled in the interface

Served locally so the app works offline. Licence texts ship alongside them in
`server/static/D3-LICENSES.txt` and `server/static/fonts/OFL.txt`.

| Component | Licence |
|---|---|
| [d3-sankey](https://github.com/d3/d3-sankey) | BSD-3-Clause |
| [d3-array](https://github.com/d3/d3-array) | BSD-3-Clause |
| [d3-shape](https://github.com/d3/d3-shape) | ISC |
| [d3-path](https://github.com/d3/d3-path) | ISC |
| [IBM Plex Sans / IBM Plex Mono](https://github.com/IBM/plex) | SIL Open Font License 1.1 |
| Job board marks from [Simple Icons](https://github.com/simple-icons/simple-icons) | CC0 1.0 |
| LinkedIn mark from [Font Awesome Free](https://github.com/FortAwesome/Font-Awesome) 6.7.2 | CC BY 4.0 |

The Plex files are the latin and latin-ext WOFF2 subsets published by Google
Fonts. They are the interface typefaces; the typefaces a CV is *rendered* in
come from rendercv-fonts above.

## Trademarks

**Claude** and the Claude mark are trademarks of Anthropic. CV Studio uses the
mark, as published by Anthropic, in one place only: to identify the Claude
Desktop integration in the interface. It is not the application's own icon, and
nothing here is built, endorsed or supported by Anthropic.

**OpenAI**, **ChatGPT** and **Codex** are trademarks of OpenAI. CV Studio uses
the OpenAI mark in one place only: to identify that integration in the
interface. It is not the application's own icon, and nothing here is built,
endorsed or supported by OpenAI.

**Hermes Agent** and **Nous Research** are trademarks of Nous Research. CV
Studio uses the Nous Research mark in one place only: to identify that
integration in the interface. It is not the application's own icon, and nothing
here is built, endorsed or supported by Nous Research. Nous Research publishes
no separate mark for Hermes Agent itself — the only artwork in its repository is
a wordmark banner — so the company's mark identifies the row, exactly as the
OpenAI mark identifies the row covering ChatGPT and Codex.

**Mistral** and **Vibe** are trademarks of Mistral AI. CV Studio uses the
Mistral mark in one place only: to identify that integration in the interface.
It is not the application's own icon, and nothing here is built, endorsed or
supported by Mistral AI.

Each mark is drawn exactly as its owner publishes it, rather than redrawn or
simplified, and all four are inlined in `server/studio.py` — three as SVG, the
Mistral one as a base64 PNG, which is the form it is published in. The Claude
mark is as published by Anthropic in their VS Code extension; the OpenAI and
Nous Research ones are taken from
[@lobehub/icons-static-svg](https://github.com/lobehub/lobe-icons) (MIT), which
redistributes brand marks for exactly this purpose. The MIT licence covers the
packaging of the artwork, not the trademarks themselves, which remain their
owners'.

**Job boards.** LinkedIn, Indeed, Glassdoor, Greenhouse, Wellfound, Welcome to
the Jungle, XING, Monster and Y Combinator are trademarks of their respective
owners. CV Studio shows each mark in one place only: beside an application, to
say which board the posting was found on. Nothing here is built, endorsed or
supported by any of them. The marks are inlined in `server/studio.py` as SVG,
unaltered, from Simple Icons (CC0), except LinkedIn's, which LinkedIn asked
Simple Icons to remove; that one is Font Awesome Free's (CC BY 4.0, by
Fonticons, Inc.). Boards that publish no mark for this kind of use -- Lever,
Workday, Ashby, SmartRecruiters, France Travail, Apec, HelloWork, JobTeaser --
are shown by their initials on a neutral tile rather than by a drawing of their
logo.

## Build tooling

[PyInstaller](https://github.com/pyinstaller/pyinstaller) 6.22.2 is licensed
under **GPL 2.0 with the Bootloader Exception**. That exception grants
"unlimited permission to link or embed compiled bootloader and related files
into combinations with other programs, and to distribute those combinations
without any restriction coming from the use of those files."

The GPL therefore does not extend to CV Studio or to the applications it
produces. PyInstaller is a build-time tool; only its exception-covered
bootloader is present in the shipped binary.

## Apache-2.0 attribution

Typst and packaging are Apache-2.0, which requires that this notice accompany
redistribution. Their copyright notices remain intact in the bundled files.
