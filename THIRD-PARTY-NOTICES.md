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
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | 2.1.1 | MIT |
| [pydantic](https://github.com/pydantic/pydantic) / pydantic-core | 2.13.5 | MIT |
| [typer](https://github.com/fastapi/typer), [rich](https://github.com/Textualize/rich), [markdown-it-py](https://github.com/executablebooks/markdown-it-py), annotated-types | — | MIT |
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

The Plex files are the latin and latin-ext WOFF2 subsets published by Google
Fonts. They are the interface typefaces; the typefaces a CV is *rendered* in
come from rendercv-fonts above.

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
